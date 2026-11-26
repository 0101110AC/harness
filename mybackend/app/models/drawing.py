"""upload_sessions / drawings（prd-07 §3.8~§3.9）"""
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
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import UTCDateTime, JSONType, Base, CreatedAtMixin, TimestampMixin, UUIDMixin, enum_col
from app.models.enums import DrawingStatus, FileType, UploadSessionStatus


class UploadSession(Base, UUIDMixin, CreatedAtMixin):
    __tablename__ = "upload_sessions"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    file_name: Mapped[str] = mapped_column(String(200), nullable=False)
    file_type: Mapped[FileType] = mapped_column(enum_col(FileType, 8), nullable=False)
    total_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, default=5 * 1024 * 1024, nullable=False)
    chunk_total: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_chunks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    md5: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[UploadSessionStatus] = mapped_column(
        enum_col(UploadSessionStatus, 16),
        default=UploadSessionStatus.UPLOADING,
        server_default=UploadSessionStatus.UPLOADING.value,
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        Index("ix_upload_sessions_user", "user_id"),
        Index("ix_upload_sessions_status_expires", "status", "expires_at"),
    )


class Drawing(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "drawings"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    file_name: Mapped[str] = mapped_column(String(200), nullable=False)
    file_type: Mapped[FileType] = mapped_column(enum_col(FileType, 8), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    # SHA256，Mock 引擎确定性 seed 的唯一来源（prd-06 §11.1.1）
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    parent_drawing_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("drawings.id", ondelete="SET NULL")
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    status: Mapped[DrawingStatus] = mapped_column(
        enum_col(DrawingStatus, 20),
        default=DrawingStatus.UPLOADED,
        server_default=DrawingStatus.UPLOADED.value,
    )
    page_count: Mapped[int | None] = mapped_column(Integer)
    parse_meta: Mapped[dict | None] = mapped_column(JSONType)
    parse_error: Mapped[str | None] = mapped_column(Text)

    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    __table_args__ = (
        Index("ix_drawings_project_current", "project_id", "is_current", "is_deleted"),
        Index("ix_drawings_project_status", "project_id", "status", "is_deleted"),
        Index("ix_drawings_checksum", "checksum"),
        Index("ix_drawings_status", "status"),
    )
