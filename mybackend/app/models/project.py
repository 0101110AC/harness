"""projects / project_members（prd-07 §3.6~§3.7）"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import UTCDateTime, Base, CreatedAtMixin, TimestampMixin, UUIDMixin, enum_col
from app.models.enums import ProjectRole, ProjectStatus

if TYPE_CHECKING:
    from app.models.user import User


class Project(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "projects"

    code: Mapped[str | None] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    building_type: Mapped[str | None] = mapped_column(String(32))
    design_stage: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[ProjectStatus] = mapped_column(
        enum_col(ProjectStatus, 16),
        default=ProjectStatus.ACTIVE,
        server_default=ProjectStatus.ACTIVE.value,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)

    # 物化计数：避免首页聚合查询（prd-07 §8 第 12 条）
    drawing_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    open_annotation_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    last_opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    owner: Mapped["User"] = relationship(lazy="joined")
    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="noload"
    )

    __table_args__ = (
        Index("ix_projects_status_deleted", "status", "is_deleted"),
        Index("ix_projects_owner", "owner_id"),
    )


class ProjectMember(Base, UUIDMixin, CreatedAtMixin):
    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_role: Mapped[ProjectRole] = mapped_column(
        enum_col(ProjectRole, 16),
        default=ProjectRole.DESIGNER,
        server_default=ProjectRole.DESIGNER.value,
    )
    last_opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    project: Mapped[Project] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_members_project_user"),
        Index("ix_project_members_user", "user_id"),
    )
