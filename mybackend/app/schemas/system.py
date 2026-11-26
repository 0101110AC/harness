"""系统管理 / 审计（prd-05 §5~§8、prd-07 §6.2 / §6.9）"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import EmailStr, Field, field_validator

from app.models.enums import RoleCode, UserStatus
from app.schemas.auth import UserBrief
from app.schemas.common import APIModel


def _check_password_strength(value: str) -> str:
    if not (any(c.isalpha() for c in value) and any(c.isdigit() for c in value)):
        raise ValueError("密码必须同时包含字母与数字")
    return value


class UserOut(UserBrief):
    status: UserStatus
    status_text: str
    roles: list[str]
    role_names: list[str] = []
    project_count: int = 0
    last_login_at: datetime | None = None
    created_at: datetime


class UserCreateRequest(APIModel):
    name: str = Field(min_length=2, max_length=32)
    email: EmailStr
    # 不传则后端生成强随机密码；无论哪种方式，明文只在本次响应里返回一次
    password: str | None = Field(default=None, min_length=8, max_length=64)
    role_codes: list[RoleCode] = Field(min_length=1)
    status: UserStatus = UserStatus.ACTIVE

    @field_validator("password")
    @classmethod
    def strong_enough(cls, v: str | None) -> str | None:
        return _check_password_strength(v) if v is not None else None


class UserCreatedResponse(UserOut):
    initial_password: str = Field(description="初始密码，仅本次返回，不落明文")


class UserUpdateRequest(APIModel):
    name: str | None = Field(default=None, min_length=2, max_length=32)
    email: EmailStr | None = None
    role_codes: list[RoleCode] | None = Field(default=None, min_length=1)
    status: UserStatus | None = None


class StatusChangeRequest(APIModel):
    status: UserStatus


class ResetPasswordResponse(APIModel):
    user_id: uuid.UUID
    new_password: str = Field(description="仅本次返回，不落库明文")


class RoleOut(APIModel):
    id: uuid.UUID
    code: RoleCode
    name: str
    description: str | None = None
    is_system: bool
    user_count: int = 0
    permissions: list[str] = []


class PermissionOut(APIModel):
    id: uuid.UUID
    code: str
    name: str
    module: str
    description: str | None = None


class PermissionGroupOut(APIModel):
    module: str
    module_text: str
    permissions: list[PermissionOut]


class AuditLogOut(APIModel):
    id: uuid.UUID
    # 操作人可能已删除（FK SET NULL），此时前端显示"系统 / 已注销用户"
    user: UserBrief | None = None
    action: str
    action_text: str
    target_type: str
    target_id: uuid.UUID | None = None
    target_name: str | None = None
    detail: dict | None = None
    ip: str | None = None
    created_at: datetime


class AuditActionOption(APIModel):
    """审计筛选下拉的唯一来源，避免前端硬编码动作文案。"""

    action: str
    text: str


SortKey = Literal["created", "last_login"]
