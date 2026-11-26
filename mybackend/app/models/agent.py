"""agent_state_checkpoints / interrupt_tasks / agent_events（prd-07 §3.20~§3.21、§3.23）"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import UTCDateTime, JSONType, Base, CreatedAtMixin, UUIDMixin, enum_col
from app.models.enums import HitlResolution, InterruptStatus, InterruptType


class AgentStateCheckpoint(Base, UUIDMixin, CreatedAtMixin):
    """可读快照，用于排障与回放；恢复的唯一权威是 LangGraph 自带 checkpoint 表。"""

    __tablename__ = "agent_state_checkpoints"

    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    node: Mapped[str] = mapped_column(String(32), nullable=False)
    checkpoint_id: Mapped[str | None] = mapped_column(String(64))
    run_status: Mapped[str] = mapped_column(String(16), nullable=False)
    state_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    # sha256(规范化 JSON)，用于与 LangGraph checkpoint 比对一致性
    state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    interrupt_id: Mapped[uuid.UUID | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_agent_state_checkpoints_thread_seq"),
        Index("ix_agent_state_checkpoints_thread_created", "thread_id", "created_at"),
        Index("ix_agent_state_checkpoints_run", "run_id"),
    )


class InterruptTask(Base, UUIDMixin, CreatedAtMixin):
    """HITL 工单：前端红点、超时扫描、审计的唯一依据。payload 不可变。"""

    __tablename__ = "interrupt_tasks"

    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    drawing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("drawings.id"), nullable=False)
    review_task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("review_tasks.id"), nullable=False)

    type: Mapped[InterruptType] = mapped_column(enum_col(InterruptType, 20), nullable=False)
    status: Mapped[InterruptStatus] = mapped_column(
        enum_col(InterruptStatus, 16),
        default=InterruptStatus.PENDING,
        server_default=InterruptStatus.PENDING.value,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)

    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolution: Mapped[str | None] = mapped_column(String(24))
    item_decisions: Mapped[list | dict | None] = mapped_column(JSONType)
    remark: Mapped[str | None] = mapped_column(Text)

    viewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        Index("ix_interrupt_tasks_status_expires", "status", "expires_at"),
        Index("ix_interrupt_tasks_project_status", "project_id", "status"),
        Index("ix_interrupt_tasks_assignee_status", "assignee_id", "status"),
        Index("ix_interrupt_tasks_review_task_created", "review_task_id", "created_at"),
        # 同一 thread 同时只允许 1 条待处理（部分唯一索引）
        Index(
            "uq_interrupt_tasks_pending_per_thread",
            "thread_id",
            unique=True,
            sqlite_where=text("status = 'PENDING'"),
            postgresql_where=text("status = 'PENDING'"),
        ),
    )


class AgentEvent(Base):
    """SSE 事件日志：支持 Last-Event-ID 重放。heartbeat 不落库。"""

    __tablename__ = "agent_events"

    # SQLite 只把 `INTEGER PRIMARY KEY` 当 rowid 别名才会自增；
    # 裸 BIGINT PRIMARY KEY 不会（插入直接 NOT NULL 失败），故按方言降级为 Integer。
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_agent_events_thread_seq"),
        Index("ix_agent_events_thread_seq", "thread_id", "seq"),
        Index("ix_agent_events_created", "created_at"),
    )
