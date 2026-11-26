"""认证与权限依赖注入。

两层校验的分工（prd-03 §5.5 / prd-05 §4.3.1）：
- 本文件的 require(perm) 只做「功能权限」：这个角色能不能调这类接口。
- 「数据权限」（这条记录是不是我的）必须写在 Service 层，D1 不涉及业务，故此处只留说明。
"""
from __future__ import annotations

import uuid
from typing import Annotated, Callable

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.errors import Forbidden, Unauthorized
from app.core.security import decode_token
from app.database import get_db
from app.models.enums import RoleCode, UserStatus
from app.models.project import ProjectMember
from app.models.user import Role, User

bearer_scheme = HTTPBearer(auto_error=False, description="Bearer <access_token>")


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    if credentials is None or not credentials.credentials:
        raise Unauthorized("缺少访问令牌")

    payload = decode_token(credentials.credentials, expected_type="access")

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise Unauthorized("无效的凭证") from exc

    # 一次 join 取回 user + roles + permissions：默认 selectin 是 3 次往返，
    # 而鉴权发生在每个请求上，压测里它是固定成本（D7 复盘）。
    user = (
        await db.execute(
            select(User)
            .options(joinedload(User.roles).joinedload(Role.permissions))
            .where(User.id == user_id)
        )
    ).unique().scalar_one_or_none()
    if user is None:
        raise Unauthorized("用户不存在或已被删除")

    if user.status is not UserStatus.ACTIVE:
        raise Forbidden("账号已被禁用，请联系管理员", code="ACCOUNT_DISABLED")

    # token_version 失效校验：禁用/改密后旧 token 立即作废
    if int(payload.get("ver", -1)) != int(user.token_version):
        raise Unauthorized("登录状态已失效，请重新登录", code="TOKEN_EXPIRED")

    return user


async def get_user_permissions(user: User) -> set[str]:
    """扁平化角色 → 权限码。ADMIN 在库里也显式绑定全部权限（不依赖 * 通配）。"""
    perms: set[str] = set()
    for role in user.roles:
        for perm in role.permissions:
            perms.add(perm.code)
    return perms


def is_superuser(user: User) -> bool:
    return any(role.code is RoleCode.ADMIN for role in user.roles)


def require(perm: str) -> Callable[..., User]:
    """依赖注入工厂：校验功能权限。ADMIN 短路（同时支持 '*' 通配码）。"""

    async def _checker(user: Annotated[User, Depends(get_current_user)]) -> User:
        if is_superuser(user):
            return user
        perms = await get_user_permissions(user)
        if "*" in perms or perm in perms:
            return user
        raise Forbidden(f"缺少权限：{perm}", detail={"required_permission": perm})

    return _checker


CurrentUser = Annotated[User, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------------
# 数据隔离：项目成员校验（prd-03 §5.5 / D2 任务 1）
# ---------------------------------------------------------------------------
async def check_project_member(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
) -> ProjectMember | None:
    """校验当前用户是该项目成员；非成员 → 403 NOT_PROJECT_MEMBER。

    ADMIN 放行（返回 None），与 require() 的短路策略保持一致；
    返回值供 Router 判断"我在这个项目里是什么角色"。
    """
    if is_superuser(user):
        return None

    member = (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user.id,
            )
        )
    ).scalar_one_or_none()

    if member is None:
        raise Forbidden("你不是该项目成员", code="NOT_PROJECT_MEMBER",
                        detail={"project_id": str(project_id)})
    return member


async def ensure_project_member(
    db: AsyncSession, project_id: uuid.UUID, user: User
) -> ProjectMember | None:
    """Service 层复用的同一检查（用于 project_id 来自 body / query 的场景）。

    返回成员行（ADMIN 返回 None），便于调用方继续判断项目内角色。
    """
    if is_superuser(user):
        return None
    member = (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise Forbidden("你不是该项目成员", code="NOT_PROJECT_MEMBER",
                        detail={"project_id": str(project_id)})
    return member

