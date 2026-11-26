"""users / roles / permissions / user_roles / role_permissions（prd-07 §3.1~§3.5）"""
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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import UTCDateTime, Base, CreatedAtMixin, TimestampMixin, UUIDMixin, enum_col
from app.models.enums import RoleCode, UserStatus


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class Role(Base, UUIDMixin):
    __tablename__ = "roles"

    code: Mapped[RoleCode] = mapped_column(enum_col(RoleCode, 32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(String(200))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )

    permissions: Mapped[list["Permission"]] = relationship(
        secondary="role_permissions", lazy="selectin", viewonly=True
    )
    users: Mapped[list["User"]] = relationship(
        secondary="user_roles", lazy="noload", back_populates="roles", viewonly=True
    )


class Permission(Base, UUIDMixin):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    module: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (Index("ix_permissions_module", "module"),)


class User(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "users"

    name: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    # 统一小写入库，等价于 prd-07 的 UNIQUE(lower(email))
    email: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[UserStatus] = mapped_column(
        enum_col(UserStatus, 16), default=UserStatus.ACTIVE, server_default=UserStatus.ACTIVE.value
    )
    # 禁用/改密时 +1，使旧 JWT 立即失效（无需黑名单）
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_login_ip: Mapped[str | None] = mapped_column(String(64))

    roles: Mapped[list[Role]] = relationship(
        secondary="user_roles", lazy="selectin", back_populates="users"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email}>"


__all__ = ["User", "Role", "Permission", "UserRole", "RolePermission"]
