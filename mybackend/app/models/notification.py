"""notifications / notification_preferences（prd-07 §3.16~§3.17）"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import UTCDateTime, JSONType, Base, CreatedAtMixin, TimestampMixin, UUIDMixin, enum_col
from app.models.enums import BizType, NotificationChannel, NotificationType


class Notification(Base, UUIDMixin, CreatedAtMixin):
    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[NotificationType] = mapped_column(enum_col(NotificationType, 24), nullable=False)
    channel: Mapped[NotificationChannel] = mapped_column(
        enum_col(NotificationChannel, 10),
        default=NotificationChannel.INAPP,
        server_default=NotificationChannel.INAPP.value,
    )
    # 幂等键：与 domain_events.event_key 对齐，消费者重试不产生重复消息
    event_key: Mapped[str] = mapped_column(String(200), nullable=False)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(String(1000), nullable=False)

    biz_type: Mapped[BizType] = mapped_column(enum_col(BizType, 20), nullable=False)
    biz_id: Mapped[uuid.UUID | None] = mapped_column()
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    deeplink: Mapped[str | None] = mapped_column(String(500))

    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    # 批量合并（prd-04 §4）：同一接收人 60s 内同 type 的消息合并成 1 条。
    # 被合并的那条**仍然落库**（保留 (user_id, event_key) 幂等台账），
    # 但 merged_into_id 非空 → 列表/未读数一律过滤掉，只在合并父行上累加 merge_count。
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    merge_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("user_id", "event_key", name="uq_notifications_user_event_key"),
        Index("ix_notifications_user_read_created", "user_id", "is_read", "created_at"),
        # 未读数走上面这条复合索引的前两列（user_id, is_read）即可；
        # D7 压测的 EXPLAIN 证明 SQLite 不会选择额外的部分索引，故不重复建。
        # 合并窗口查询（user_id + type + created_at）与偏好类筛选共用
        Index("ix_notifications_user_type_created", "user_id", "type", "created_at"),
    )


class NotificationPreference(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[NotificationChannel] = mapped_column(enum_col(NotificationChannel, 10), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # 飞书 user_id / 群 webhook（加密存储）；V1.0 仅 INAPP，该字段留空
    target: Mapped[str | None] = mapped_column(String(500))
    event_types: Mapped[list] = mapped_column(JSONType, default=list, server_default="[]")

    __table_args__ = (UniqueConstraint("user_id", "channel", name="uq_notification_prefs_user_channel"),)
