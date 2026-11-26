"""通知分发：事件 → 收件人 → 适配器（prd-04 §9.1）。

这里集中三条业务规则，任何一条写错都会变成"用户被打扰"或"流程被挂死"：

1. **不通知自己**：自己下发给自己的批注不发（payload 的 actor == 接收人即剔除）；
2. **例外**：`HITL_PENDING` / `REPORT_READY` 即使操作人是自己也要发 ——
   这两条是"流程正卡在你这里"，跳过等于把流程挂死；
3. **批量合并**：60s 窗口内同 type 同接收人合并为 1 条（`HITL_PENDING` 不参与，
   合并会丢 interrupt_id，点进去定位不到具体中断）。

订阅偏好在这里过滤（`notification_preferences.event_types` 为空数组 = 订阅全部）。
V1.0 只读偏好页不影响这一层——过滤逻辑先就位，V1.5 打开开关即可生效。
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.enums import NotificationChannel, NotificationType, ProjectRole
from app.models.notification import NotificationPreference
from app.models.project import ProjectMember
from app.models.system import DomainEvent
from app.notify.adapters import (
    AdapterResult,
    NotificationAdapter,
    NotifyTarget,
    build_adapters,
)
from app.notify.render import SELF_NOTIFY_ALLOWED, render_event, safe_deeplink

log = logging.getLogger(__name__)

# HITL 类事件的"审图角色"兜底：项目 OWNER / REVIEWER（prd-04 §4 接收人说明）
REVIEWER_ROLES = (ProjectRole.OWNER, ProjectRole.REVIEWER)


class NotificationService:
    def __init__(self, adapters: list[NotificationAdapter] | None = None) -> None:
        self._adapters = adapters if adapters is not None else build_adapters()

    @property
    def adapters(self) -> list[NotificationAdapter]:
        return self._adapters

    async def dispatch(self, event: DomainEvent) -> AdapterResult:
        fallback = await self._fallback_recipients(event)
        rendered = render_event(event, fallback)
        if rendered is None:
            # 未知事件类型不该堵住队列：消费成功但零投递
            log.warning("未知事件类型，跳过：%s", event.event_type)
            return AdapterResult(success=True)

        recipients = self._filter_self(event, rendered.recipients, rendered.type)
        if not recipients:
            return AdapterResult(success=True)

        prefs = await self._load_prefs(recipients)
        targets: list[NotifyTarget] = []
        for uid in recipients:
            pref = prefs.get(uid)
            if pref is not None:
                if not pref.get("enabled", True):
                    continue
                subscribed = pref.get("event_types") or []
                if subscribed and event.event_type not in subscribed:
                    continue
            targets.append(
                NotifyTarget(
                    user_id=uid,
                    title=rendered.title,
                    content=rendered.content,
                    type=rendered.type,
                    biz_type=rendered.biz_type,
                    biz_id=rendered.biz_id,
                    deeplink=safe_deeplink(rendered.deeplink),
                    prefs=pref,
                    project_id=rendered.project_id,
                    mergeable=rendered.mergeable,
                )
            )

        if not targets:
            return AdapterResult(success=True, skipped=len(recipients))

        result = AdapterResult(success=True)
        for adapter in self._adapters:
            if not adapter.enabled:
                continue
            try:
                result = result.merge(await adapter.send(event, targets))
            except asyncio.CancelledError:
                raise
            except Exception:
                # 任一渠道失败都让事件重试：静默丢掉一个渠道比"重复投递"更糟
                log.exception("适配器 %s 投递失败 key=%s", adapter.name, event.event_key)
                raise
        return result

    # ---------------- 规则实现 ----------------
    @staticmethod
    def _filter_self(
        event: DomainEvent, recipients: list[uuid.UUID], etype: NotificationType | None
    ) -> list[uuid.UUID]:
        if event.actor_id is None:
            return recipients
        if etype in SELF_NOTIFY_ALLOWED:
            return recipients
        return [uid for uid in recipients if uid != event.actor_id]

    async def _load_prefs(self, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(NotificationPreference).where(
                        NotificationPreference.user_id.in_(user_ids),
                        NotificationPreference.channel == NotificationChannel.INAPP,
                    )
                )
            ).scalars().all()
        return {
            row.user_id: {
                "channel": NotificationChannel.INAPP.value,
                "enabled": row.enabled,
                "event_types": list(row.event_types or []),
            }
            for row in rows
        }

    async def _fallback_recipients(self, event: DomainEvent) -> list[uuid.UUID]:
        """HITL 类事件的兜底接收人：项目内审图角色。

        只查"成员目录"，不查业务状态 —— 后者必须进 payload 快照，
        否则事件重试时收件人已经变了，消息会发给一个不该收到的人。
        """
        try:
            etype = NotificationType(event.event_type)
        except ValueError:
            return []
        if etype not in SELF_NOTIFY_ALLOWED:
            return []
        payload = event.payload or {}
        if payload.get("recipient_ids") or event.project_id is None:
            return []

        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(ProjectMember.user_id).where(
                        ProjectMember.project_id == event.project_id,
                        ProjectMember.project_role.in_(REVIEWER_ROLES),
                    )
                )
            ).scalars().all()
        return list(rows)


_service: NotificationService | None = None


def get_notification_service() -> NotificationService:
    """进程内单例：适配器表按配置构建一次（配置改了要重启，这是有意的）。"""
    global _service
    if _service is None:
        _service = NotificationService()
    return _service
