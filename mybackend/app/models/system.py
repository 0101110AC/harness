"""domain_events / audit_logs（prd-07 §3.18~§3.19）"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import UTCDateTime, JSONType, Base, CreatedAtMixin, UUIDMixin, enum_col
from app.models.enums import EventStatus
from app.models.user import User


class DomainEvent(Base, UUIDMixin, CreatedAtMixin):
    """Outbox 表：与业务写入同事务，消费者轮询拉取；语义等价 MQ。"""

    __tablename__ = "domain_events"

    # {event_type}:{aggregate_id}:{version} —— 幂等键
    event_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(40), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    status: Mapped[EventStatus] = mapped_column(
        enum_col(EventStatus, 16), default=EventStatus.PENDING, server_default=EventStatus.PENDING.value
    )
    retry_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_message: Mapped[str | None] = mapped_column(Text)
    # 认领时刻 = 租约起点。消费者崩在 PROCESSING 时，靠它把事件回收成 PENDING，
    # 否则这些事件永远卡在"处理中"，站内信静默丢失（D5 复盘 2.2）。
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        Index("ix_domain_events_status_next_retry", "status", "next_retry_at"),
        Index("ix_domain_events_project", "project_id"),
    )


class AuditLog(Base, UUIDMixin):
    __tablename__ = "audit_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column()
    target_name: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict | None] = mapped_column(JSONType)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )

    user: Mapped[User | None] = relationship("User", lazy="joined", viewonly=True)

    __table_args__ = (
        Index("ix_audit_logs_created", "created_at"),
        Index("ix_audit_logs_action_created", "action", "created_at"),
        Index("ix_audit_logs_user_created", "user_id", "created_at"),
    )
