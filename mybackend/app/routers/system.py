"""系统管理路由：/api/v1/users、/roles、/permissions、/audit-logs（prd-05 §5~§8）"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import DB, require
from app.core.errors import ValidationFailed
from app.models.enums import RoleCode, UserStatus
from app.models.user import User
from app.schemas.common import Page
from app.schemas.system import (
    AuditActionOption,
    AuditLogOut,
    PermissionGroupOut,
    ResetPasswordResponse,
    RoleOut,
    StatusChangeRequest,
    UserCreateRequest,
    UserCreatedResponse,
    UserOut,
    UserUpdateRequest,
)
from app.services import audit_service
from app.services import system_service as svc

router = APIRouter(tags=["admin"])


# ------------------------------------------------------------------ 用户
@router.get("/users", response_model=Page[UserOut], summary="用户列表（角色/状态/关键词筛选）")
async def list_users(
    db: DB,
    _: Annotated[User, Depends(require("system:user:manage"))],
    keyword: str | None = Query(default=None, max_length=64, description="用户名或邮箱"),
    role: list[RoleCode] | None = Query(default=None, description="可重复传多个"),
    status_: UserStatus | None = Query(default=None, alias="status"),
    sort: Literal["created", "last_login"] = Query("created"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Page[UserOut]:
    return await svc.list_users(
        db,
        page=page,
        page_size=page_size,
        keyword=keyword,
        role_codes=role,
        status=status_,
        sort=sort,
    )


@router.post(
    "/users",
    response_model=UserCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新建用户（初始密码仅本次返回）",
)
async def create_user(
    body: UserCreateRequest,
    db: DB,
    operator: Annotated[User, Depends(require("system:user:manage"))],
) -> UserCreatedResponse:
    return await svc.create_user(db, body, operator)


@router.patch("/users/{user_id}", response_model=UserOut, summary="编辑用户（邮箱/角色/状态）")
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdateRequest,
    db: DB,
    operator: Annotated[User, Depends(require("system:user:manage"))],
) -> UserOut:
    return await svc.update_user(db, user_id, body, operator)


@router.post(
    "/users/{user_id}/reset-password",
    response_model=ResetPasswordResponse,
    summary="重置密码（新密码仅本次返回，旧会话全部失效）",
)
async def reset_password(
    user_id: uuid.UUID,
    db: DB,
    operator: Annotated[User, Depends(require("system:user:manage"))],
) -> ResetPasswordResponse:
    return await svc.reset_password(db, user_id, operator)


@router.post(
    "/users/{user_id}/status",
    response_model=UserOut,
    summary="启用/禁用（不能禁用自己，至少保留一名启用 ADMIN）",
)
async def change_status(
    user_id: uuid.UUID,
    body: StatusChangeRequest,
    db: DB,
    operator: Annotated[User, Depends(require("system:user:manage"))],
) -> UserOut:
    return await svc.change_status(db, user_id, body, operator)


# ------------------------------------------------------------------ 角色 / 权限
@router.get("/roles", response_model=list[RoleOut], summary="角色列表（含用户数与权限码）")
async def list_roles(
    db: DB,
    _: Annotated[User, Depends(require("system:role:read"))],
) -> list[RoleOut]:
    return await svc.list_roles(db)


@router.get(
    "/permissions",
    response_model=list[PermissionGroupOut],
    summary="权限码（按模块分组，V1.0 只读）",
)
async def list_permissions(
    db: DB,
    _: Annotated[User, Depends(require("system:role:read"))],
) -> list[PermissionGroupOut]:
    return await svc.list_permissions(db)


# ------------------------------------------------------------------ 审计
@router.get(
    "/audit-logs/actions",
    response_model=list[AuditActionOption],
    summary="审计动作枚举（筛选下拉的唯一来源）",
)
async def audit_actions(
    _: Annotated[User, Depends(require("system:audit:read"))],
) -> list[AuditActionOption]:
    return svc.audit_action_options()


def _to_utc(value: datetime | None) -> datetime | None:
    """前端传的是本地时间的绝对时刻；审计表存 UTC，比较前统一换算。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@router.get(
    "/audit-logs",
    response_model=Page[AuditLogOut],
    summary="审计日志（时间/动作/操作人筛选，仅 system:audit:read）",
)
async def list_audit_logs(
    db: DB,
    _: Annotated[User, Depends(require("system:audit:read"))],
    action: str | None = Query(default=None, max_length=40),
    user_id: uuid.UUID | None = Query(default=None),
    target_type: str | None = Query(default=None, max_length=32),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Page[AuditLogOut]:
    if start is not None and end is not None and start > end:
        raise ValidationFailed("start 不能晚于 end", detail={"field": "start"})
    return await audit_service.list_audit_logs(
        db,
        page=page,
        page_size=page_size,
        action=action,
        user_id=user_id,
        target_type=target_type,
        start=_to_utc(start),
        end=_to_utc(end),
    )
