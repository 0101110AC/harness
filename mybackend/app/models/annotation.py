"""annotations / annotation_comments / annotation_status_logs（prd-07 §3.13~§3.15）"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import UTCDateTime, JSONType, Base, CreatedAtMixin, TimestampMixin, UUIDMixin, enum_col
from app.models.enums import (
    AnnotationSource,
    AnnotationStatus,
    Discipline,
    LogType,
    Severity,
    ShapeType,
)

if TYPE_CHECKING:
    from app.models.user import User


class Annotation(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "annotations"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    drawing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("drawings.id"), nullable=False)
    drawing_version: Mapped[int] = mapped_column(Integer, nullable=False)

    issue_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("review_issues.id", ondelete="SET NULL"))
    source: Mapped[AnnotationSource] = mapped_column(
        enum_col(AnnotationSource, 8),
        default=AnnotationSource.HUMAN,
        server_default=AnnotationSource.HUMAN.value,
    )
    shape_type: Mapped[ShapeType] = mapped_column(
        enum_col(ShapeType, 10), default=ShapeType.RECT, server_default=ShapeType.RECT.value
    )

    location: Mapped[dict] = mapped_column(JSONType, nullable=False)
    location_valid: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    title: Mapped[str] = mapped_column(String(60), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[Severity] = mapped_column(enum_col(Severity, 8), nullable=False)
    discipline: Mapped[Discipline] = mapped_column(enum_col(Discipline, 8), nullable=False)

    status: Mapped[AnnotationStatus] = mapped_column(
        enum_col(AnnotationStatus, 20),
        default=AnnotationStatus.PENDING_CONFIRM,
        server_default=AnnotationStatus.PENDING_CONFIRM.value,
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    assignee_missing: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    creator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    version_drawing_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("drawings.id", ondelete="SET NULL")
    )

    close_remark: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    comment_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 乐观锁：PATCH / transition 必须回传并比对
    row_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)

    __table_args__ = (
        Index("ix_annotations_drawing_status", "drawing_id", "status"),
        # 跨项目/项目内列表的排序键：旧 (assignee_id, status) 是新索引的前缀，已删除避免写放大
        Index("ix_annotations_assignee_status_updated", "assignee_id", "status", "updated_at"),
        Index("ix_annotations_project_status_updated", "project_id", "status", "updated_at"),
        Index("ix_annotations_project_closed", "project_id", "closed_at"),
        Index("ix_annotations_project_severity", "project_id", "severity"),
        Index("ix_annotations_creator", "creator_id"),
    )

    # joined 预加载，避免列表接口 N+1（D2 复盘 2.1）
    assignee: Mapped["User | None"] = relationship(
        "User", foreign_keys="Annotation.assignee_id", lazy="joined"
    )
    creator: Mapped["User"] = relationship(
        "User", foreign_keys="Annotation.creator_id", lazy="joined"
    )


class AnnotationComment(Base, UUIDMixin, CreatedAtMixin):
    __tablename__ = "annotation_comments"

    annotation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("annotations.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    __table_args__ = (Index("ix_annotation_comments_annotation_created", "annotation_id", "created_at"),)


class AnnotationStatusLog(Base, UUIDMixin):
    """只插入不更新不删除（审计要求）。"""

    __tablename__ = "annotation_status_logs"

    annotation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("annotations.id", ondelete="CASCADE"), nullable=False
    )
    log_type: Mapped[LogType] = mapped_column(enum_col(LogType, 20), nullable=False)
    from_status: Mapped[AnnotationStatus | None] = mapped_column(enum_col(AnnotationStatus, 20))
    to_status: Mapped[AnnotationStatus | None] = mapped_column(enum_col(AnnotationStatus, 20))
    from_assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    to_assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    operator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_annotation_status_logs_annotation_created", "annotation_id", "created_at"),
    )
