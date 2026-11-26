"""通知适配器（prd-04 §9.3）：V1.5 接飞书时**只改这一个文件**。

适配器只负责"把已渲染好的消息投递出去"，不允许做业务判断
（该不该发、发给谁、文案是什么，都在 render/dispatcher 里决定）。
这条边界就是"零改动扩展"的全部含金量：飞书适配器不需要知道批注是什么。
"""
from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database import AsyncSessionLocal
from app.models.enums import BizType, NotificationChannel, NotificationType
from app.models.notification import Notification
from app.models.system import DomainEvent
from app.notify.render import merge_content, merge_deeplink, merge_title, sample_title

log = logging.getLogger(__name__)


@dataclass
class NotifyTarget:
    """一条待投递消息（prd-04 §9.3）。"""

    user_id: uuid.UUID
    title: str
    content: str
    type: NotificationType
    biz_type: BizType
    biz_id: uuid.UUID | None
    deeplink: str | None
    prefs: dict | None = None
    project_id: uuid.UUID | None = None
    mergeable: bool = False


@dataclass
class AdapterResult:
    success: bool
    delivered: int = 0
    skipped: int = 0
    error: str | None = None

    def merge(self, other: "AdapterResult") -> "AdapterResult":
        return AdapterResult(
            success=self.success and other.success,
            delivered=self.delivered + other.delivered,
            skipped=self.skipped + other.skipped,
            error=self.error or other.error,
        )


class NotificationAdapter(ABC):
    name: str = "base"
    enabled: bool = False

    @abstractmethod
    async def send(self, event: DomainEvent, targets: list[NotifyTarget]) -> AdapterResult: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InAppAdapter(NotificationAdapter):
    """批量写 `notifications` 表，按 `(user_id, event_key)` 唯一约束去重。

    on_conflict_do_nothing 而不是"先查再插"：并发/重试时先查再插有 TOCTOU 窗口，
    唯一约束才是唯一可靠的去重手段 —— 查只是为了少写一次日志。
    """

    name = "inapp"
    enabled = True

    async def send(self, event: DomainEvent, targets: list[NotifyTarget]) -> AdapterResult:
        if not targets:
            return AdapterResult(success=True)

        delivered = skipped = 0
        async with AsyncSessionLocal() as db:
            for target in targets:
                parent = None
                if target.mergeable:
                    parent = await self._find_merge_parent(db, target)

                inserted = await self._insert(
                    db, event, target, merged_into=parent.id if parent else None
                )
                if not inserted:
                    # 同一 (user_id, event_key) 已存在：消费者重试的正常路径
                    skipped += 1
                    continue

                delivered += 1
                if parent is not None:
                    await self._bump_parent(db, parent, target)
            await db.commit()

        return AdapterResult(success=True, delivered=delivered, skipped=skipped)

    # ---------------- 幂等落库 ----------------
    async def _insert(
        self,
        db: AsyncSession,
        event: DomainEvent,
        target: NotifyTarget,
        *,
        merged_into: uuid.UUID | None,
    ) -> bool:
        values = {
            "user_id": target.user_id,
            "type": target.type,
            "channel": NotificationChannel.INAPP,
            "event_key": event.event_key,
            "title": target.title[:200],
            "content": target.content[:1000],
            "biz_type": target.biz_type,
            "biz_id": target.biz_id,
            "project_id": target.project_id,
            "deeplink": target.deeplink,
            "is_read": False,
            "is_deleted": False,
            "merged_into_id": merged_into,
            "merge_count": 1,
        }
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(Notification).values(**values)
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as lite_insert

            stmt = lite_insert(Notification).values(**values)
        else:  # pragma: no cover - 只有 SQLite / PG 两种部署形态
            db.add(Notification(**values))
            return True

        result = await db.execute(
            stmt.on_conflict_do_nothing(index_elements=["user_id", "event_key"])
        )
        return (result.rowcount or 0) > 0

    # ---------------- 批量合并 ----------------
    async def _find_merge_parent(
        self, db: AsyncSession, target: NotifyTarget
    ) -> Notification | None:
        """60 秒窗口内、同接收人、同 type 的"未合并"消息作为父行。"""
        window_start = _now() - timedelta(seconds=settings.NOTIFICATION_MERGE_WINDOW)
        return (
            await db.execute(
                select(Notification)
                .where(
                    Notification.user_id == target.user_id,
                    Notification.type == target.type,
                    Notification.channel == NotificationChannel.INAPP,
                    Notification.merged_into_id.is_(None),
                    Notification.is_deleted.is_(False),
                    Notification.created_at >= window_start,
                )
                .order_by(Notification.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _bump_parent(
        self, db: AsyncSession, parent: Notification, target: NotifyTarget
    ) -> None:
        """把新消息合并进父行：条数 +1、标题改"N 条…"、摘要列前两条、重新置未读。

        为什么合并后要清 is_read：合并意味着"又来了新东西"，
        已读状态下静默吞掉新批注就等于漏通知。
        """
        count = (parent.merge_count or 1) + 1
        first_child = (
            await db.execute(
                select(Notification)
                .where(Notification.merged_into_id == parent.id)
                .order_by(Notification.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

        samples = [sample_title(parent.type, parent.title)]
        if first_child is not None:
            samples.append(sample_title(first_child.type, first_child.title))

        parent.merge_count = count
        parent.title = merge_title(parent.type, count)
        parent.content = merge_content(parent.type, samples, count)[:1000]
        parent.deeplink = merge_deeplink(parent.type)
        parent.is_read = False
        parent.read_at = None


class FeishuAdapter(NotificationAdapter):
    """V1.5 占位（prd-04 §9.3）：类存在、默认 `enabled=False`、`send` 抛 NotImplementedError。

    `enabled` 读配置而不是硬编码 False：一旦有人真把 `NOTIFICATION_ADAPTERS` 配上 feishu，
    dispatch 就会真的调到这里并炸出 NotImplementedError —— 配置错误要响，不要静默跳过。
    """

    name = "feishu"

    @property
    def enabled(self) -> bool:  # type: ignore[override]
        return "feishu" in settings.adapter_names

    async def send(self, event: DomainEvent, targets: list[NotifyTarget]) -> AdapterResult:
        raise NotImplementedError("飞书渠道将在 V1.5 开放，请先使用站内信")


_ADAPTER_REGISTRY: dict[str, type[NotificationAdapter]] = {
    "inapp": InAppAdapter,
    "feishu": FeishuAdapter,
}


def build_adapters() -> list[NotificationAdapter]:
    """按 `settings.NOTIFICATION_ADAPTERS` 组装（默认只开站内信）。"""
    adapters: list[NotificationAdapter] = []
    for name in settings.adapter_names:
        cls = _ADAPTER_REGISTRY.get(name)
        if cls is None:
            log.warning("未知的通知适配器：%s（已跳过）", name)
            continue
        adapters.append(cls())
    return adapters or [InAppAdapter()]
