"""review_tasks / review_issues / issue_feedbacks / report_drafts（prd-07 §3.10~§3.12、§3.22）"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    UTCDateTime,
    JSONType,
    Base,
    CreatedAtMixin,
    TimestampMixin,
    UUIDMixin,
    enum_col,
)
from app.models.enums import (
    Discipline,
    IssueCategory,
    IssueStatus,
    RejectReason,
    ReportStatus,
    ReviewMode,
    ReviewStage,
    ReviewTaskStatus,
    RunStatus,
    Severity,
)


class ReviewTask(Base, UUIDMixin, CreatedAtMixin):
    __tablename__ = "review_tasks"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    drawing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("drawings.id"), nullable=False)

    status: Mapped[ReviewTaskStatus] = mapped_column(
        enum_col(ReviewTaskStatus, 16),
        default=ReviewTaskStatus.PENDING,
        server_default=ReviewTaskStatus.PENDING.value,
    )
    stage: Mapped[ReviewStage | None] = mapped_column(enum_col(ReviewStage, 24))
    stage_text: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")

    engine: Mapped[str] = mapped_column(String(16), default="mock", server_default="mock")
    issue_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    issue_blocker: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    summary: Mapped[dict | None] = mapped_column(JSONType)
    error_reason: Mapped[str | None] = mapped_column(Text)
    cost_ms: Mapped[int | None] = mapped_column(Integer)

    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # ---- V1.0 引擎相关（prd-07 §3.10）----
    thread_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64))
    run_status: Mapped[RunStatus] = mapped_column(
        enum_col(RunStatus, 16), default=RunStatus.PENDING, server_default=RunStatus.PENDING.value
    )
    current_node: Mapped[str | None] = mapped_column(String(32))
    review_mode: Mapped[ReviewMode] = mapped_column(
        enum_col(ReviewMode, 16),
        default=ReviewMode.PROFESSIONAL,
        server_default=ReviewMode.PROFESSIONAL.value,
    )
    # 逻辑外键：与 report_drafts 互相引用，建 FK 会造成循环依赖，故不加约束
    report_id: Mapped[uuid.UUID | None] = mapped_column()
    waiting_interrupt_id: Mapped[uuid.UUID | None] = mapped_column()
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    __table_args__ = (
        Index("ix_review_tasks_drawing_created", "drawing_id", "created_at"),
        Index("ix_review_tasks_status", "status"),
        Index("ix_review_tasks_run_status_interrupt", "run_status", "waiting_interrupt_id"),
    )


class ReviewIssue(Base, UUIDMixin, CreatedAtMixin):
    __tablename__ = "review_issues"

    review_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="CASCADE"), nullable=False
    )
    drawing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("drawings.id"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)

    category: Mapped[IssueCategory] = mapped_column(enum_col(IssueCategory, 16), nullable=False)
    severity: Mapped[Severity] = mapped_column(enum_col(Severity, 8), nullable=False)
    discipline: Mapped[Discipline] = mapped_column(enum_col(Discipline, 8), nullable=False)

    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    suggestion: Mapped[str | None] = mapped_column(Text)
    rule_code: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    location: Mapped[dict] = mapped_column(JSONType, nullable=False)
    location_valid: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    status: Mapped[IssueStatus] = mapped_column(
        enum_col(IssueStatus, 16), default=IssueStatus.PENDING, server_default=IssueStatus.PENDING.value
    )
    # 逻辑外键：annotation 与 issue 双向引用，避免循环 FK
    annotation_id: Mapped[uuid.UUID | None] = mapped_column()

    __table_args__ = (
        Index("ix_review_issues_task_severity", "review_task_id", "severity", "confidence"),
        Index("ix_review_issues_drawing_status", "drawing_id", "status"),
    )


class IssueFeedback(Base, UUIDMixin, CreatedAtMixin):
    __tablename__ = "issue_feedbacks"

    issue_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_issues.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    reason: Mapped[RejectReason] = mapped_column(enum_col(RejectReason, 24), nullable=False)
    remark: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (Index("ix_issue_feedbacks_issue", "issue_id"),)


class ReportDraft(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "report_drafts"

    review_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    drawing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("drawings.id"), nullable=False)

    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[ReportStatus] = mapped_column(
        enum_col(ReportStatus, 20), default=ReportStatus.DRAFT, server_default=ReportStatus.DRAFT.value
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[dict | None] = mapped_column(JSONType)
    sections: Mapped[list | dict] = mapped_column(JSONType, nullable=False)
    risk_decisions: Mapped[list | dict | None] = mapped_column(JSONType)
    artifact_ref: Mapped[str | None] = mapped_column(String(512))

    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reject_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_report_drafts_task_version", "review_task_id", "version"),
        Index("ix_report_drafts_status", "status"),
    )
