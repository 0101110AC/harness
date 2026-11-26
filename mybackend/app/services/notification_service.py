"""站内信查询与已读（prd-04 §5~§8、prd-07 §6.8）。

这一层只做"读自己那张表"的轻查询，投递逻辑在 `app/notify/` 里 ——
把"写消息"和"读消息"分开，是因为它们的稳定性要求完全不同：
投递链路的任何改动都不该有机会拖慢铃铛角标的轮询。

三个全表通用的过滤条件（漏一个就是线上事故）：
- `user_id = me`：横向越权防线；
- `is_deleted = false`：软删除；
- `merged_into_id IS NULL`：批量合并的子行只是幂等台账，不是用户可见消息。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, ValidationFailed
from app.models.enums import (
    NOTIFICATION_TYPE_TEXT,
    NotificationChannel,
    NotificationType,
)
from app.models.notification import Notification, NotificationPreference
from app.models.user import User
from app.schemas.common import Page
from app.schemas.notification import (
    ChannelPreference,
    NotificationOut,
    PreferencesOut,
    PreferencesUpdateRequest,
    ReadAllResponse,
    UnreadCountResponse,
)

UNREAD_CAP = 100

# V1.0 只有站内信可用；另外两个是"证明架构预留"的占位（prd-04 §8）
CHANNEL_PLAN = {
    NotificationChannel.INAPP: (True, None),
    NotificationChannel.FEISHU: (False, "飞书渠道将在 V1.5 开放"),
    NotificationChannel.EMAIL: (False, "邮件摘要在规划中"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_out(n: Notification) -> NotificationOut:
    return NotificationOut(
        id=n.id,
        type=n.type,
        type_text=NOTIFICATION_TYPE_TEXT.get(n.type, "系统"),
        title=n.title,
        content=n.content,
        biz_type=n.biz_type,
        biz_id=n.biz_id,
        project_id=n.project_id,
        deeplink=n.deeplink,
        is_read=n.is_read,
        read_at=n.read_at,
        created_at=n.created_at,
        merge_count=n.merge_count or 1,
    )


def _visible(user: User):
    """用户可见消息的公共条件。"""
    return (
        Notification.user_id == user.id,
        Notification.is_deleted.is_(False),
        Notification.merged_into_id.is_(None),
    )


async def list_notifications(
    db: AsyncSession,
    user: User,
    *,
    tab: str = "all",
    type_: NotificationType | None = None,
    page: int = 1,
    page_size: int = 20,
) -> Page[NotificationOut]:
    conditions = list(_visible(user))
    if tab == "unread":
        conditions.append(Notification.is_read.is_(False))
    elif tab == "system":
        conditions.append(Notification.type == NotificationType.SYSTEM)
    if type_ is not None:
        conditions.append(Notification.type == type_)

    total = (
        await db.execute(select(func.count()).select_from(Notification).where(*conditions))
    ).scalar_one()

    rows = (
        await db.execute(
            select(Notification)
            .where(*conditions)
            .order_by(Notification.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return Page[NotificationOut](
        items=[_to_out(n) for n in rows],
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )


async def unread_count(db: AsyncSession, user: User) -> UnreadCountResponse:
    """铃铛角标：单表 count，走 (user_id, is_read, created_at) 索引。"""
    count = (
        await db.execute(
            select(func.count()).select_from(Notification).where(
                *_visible(user), Notification.is_read.is_(False)
            )
        )
    ).scalar_one()
    return UnreadCountResponse(count=count, capped=count >= UNREAD_CAP)


async def recent(db: AsyncSession, user: User, *, limit: int = 5) -> list[NotificationOut]:
    """铃铛浮层：最近 N 条（未读优先不排序，按时间倒序，与列表页一致）。"""
    rows = (
        await db.execute(
            select(Notification)
            .where(*_visible(user))
            .order_by(Notification.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [_to_out(n) for n in rows]


async def mark_read(
    db: AsyncSession, user: User, notification_id: uuid.UUID
) -> NotificationOut:
    """单条已读：**幂等**（重复点同一条返回 200，prd-04 §7 防坑清单）。"""
    notification = (
        await db.execute(
            select(Notification).where(
                Notification.id == notification_id, Notification.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if notification is None:
        raise NotFound("消息不存在")

    if not notification.is_read:
        notification.is_read = True
        notification.read_at = _now()
        await db.commit()
        await db.refresh(notification)
    return _to_out(notification)


async def read_all(db: AsyncSession, user: User) -> ReadAllResponse:
    result = await db.execute(
        update(Notification)
        .where(*_visible(user), Notification.is_read.is_(False))
        .values(is_read=True, read_at=_now())
    )
    await db.commit()
    return ReadAllResponse(updated=result.rowcount or 0)


# ---------------------------------------------------------------------------
# 偏好（V1.0 只读 + 仅允许改 event_types，prd-04 §8）
# ---------------------------------------------------------------------------
def _mask(target: str | None) -> str | None:
    """V1.5 展示飞书 user_id / webhook 时用；V1.0 全部为空。"""
    if not target:
        return None
    if len(target) <= 4:
        return "****"
    return f"{target[:2]}****{target[-2:]}"


async def _inapp_pref(
    db: AsyncSession, user: User, *, create: bool = False
) -> NotificationPreference | None:
    pref = (
        await db.execute(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user.id,
                NotificationPreference.channel == NotificationChannel.INAPP,
            )
        )
    ).scalar_one_or_none()
    if pref is None and create:
        pref = NotificationPreference(
            user_id=user.id, channel=NotificationChannel.INAPP, enabled=True, event_types=[]
        )
        db.add(pref)
        await db.flush()
    return pref


async def get_preferences(db: AsyncSession, user: User) -> PreferencesOut:
    inapp = await _inapp_pref(db, user)
    channels: list[ChannelPreference] = []
    for channel, (available, reason) in CHANNEL_PLAN.items():
        if channel is NotificationChannel.INAPP:
            channels.append(
                ChannelPreference(
                    channel=channel,
                    enabled=inapp.enabled if inapp else True,
                    available=True,
                    target_masked=None,
                    event_types=list(inapp.event_types or []) if inapp else [],
                    unavailable_reason=None,
                )
            )
        else:
            channels.append(
                ChannelPreference(
                    channel=channel,
                    enabled=False,
                    available=available,
                    target_masked=_mask(None),
                    event_types=[],
                    unavailable_reason=reason,
                )
            )
    return PreferencesOut(channels=channels)


async def update_preferences(
    db: AsyncSession, user: User, payload: PreferencesUpdateRequest
) -> PreferencesOut:
    """V1.0 只允许改 `event_types`；想关站内信 → 422 明说原因，不做静默忽略。"""
    if payload.event_types is not None:
        allowed = {t.value for t in NotificationType}
        unknown = [t for t in payload.event_types if t not in allowed]
        if unknown:
            raise ValidationFailed(
                "包含未知的事件类型", detail={"field": "event_types", "unknown": unknown}
            )
        pref = await _inapp_pref(db, user, create=True)
        pref.event_types = payload.event_types
        await db.commit()

    return await get_preferences(db, user)
