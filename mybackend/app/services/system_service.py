"""用户管理 / 角色权限（prd-05 §5、§7）。

三条业务红线都写在这一层，且都在数据库可见的状态上判断，不信任前端：
1. 不能禁用自己；
2. 不能移除自己的 ADMIN 角色；
3. 系统至少保留一名启用状态的 ADMIN。
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.security import generate_password, hash_password
from app.models.enums import AUDIT_ACTION_TEXT, AuditAction, RoleCode, UserStatus
from app.models.project import ProjectMember
from app.models.user import Permission, Role, User, UserRole
from app.schemas.common import Page
from app.schemas.system import (
    AuditActionOption,
    PermissionGroupOut,
    PermissionOut,
    ResetPasswordResponse,
    RoleOut,
    StatusChangeRequest,
    UserCreateRequest,
    UserCreatedResponse,
    UserOut,
    UserUpdateRequest,
)
from app.services import audit_service

ROLE_ORDER = [RoleCode.ADMIN, RoleCode.REVIEWER, RoleCode.DESIGNER, RoleCode.VIEWER]
MODULE_ORDER = ["project", "drawing", "review", "annotation", "notification", "system"]
MODULE_TEXT = {
    "project": "项目",
    "drawing": "图纸",
    "review": "预审",
    "annotation": "批注",
    "notification": "通知",
    "system": "系统",
}
USER_STATUS_TEXT = {
    UserStatus.ACTIVE: "正常",
    UserStatus.DISABLED: "已禁用",
}


def _project_count_subquery():
    return (
        select(func.count())
        .select_from(ProjectMember)
        .where(ProjectMember.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )


def _to_out(user: User, project_count: int = 0) -> UserOut:
    return UserOut(
        id=user.id,
        name=user.name,
        email=user.email,
        avatar_url=user.avatar_url,
        status=user.status,
        status_text=USER_STATUS_TEXT[user.status],
        roles=[r.code.value for r in user.roles],
        role_names=[r.name for r in user.roles],
        project_count=project_count,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


async def _load_user(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = (
        await db.execute(
            select(User).options(selectinload(User.roles)).where(User.id == user_id)
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFound("用户不存在")
    return user


async def _resolve_roles(db: AsyncSession, codes: list[RoleCode]) -> list[Role]:
    wanted = list(dict.fromkeys(codes))  # 去重且保序
    roles = (
        await db.execute(select(Role).where(Role.code.in_(wanted)))
    ).scalars().all()
    found = {r.code for r in roles}
    missing = [c.value for c in wanted if c not in found]
    if missing:
        raise ValidationFailed(
            "角色不存在", detail={"field": "role_codes", "missing": missing}
        )
    by_code = {r.code: r for r in roles}
    return [by_code[c] for c in wanted]


def _is_active_admin(user: User) -> bool:
    return user.status is UserStatus.ACTIVE and any(r.code is RoleCode.ADMIN for r in user.roles)


async def _active_admin_count(db: AsyncSession, exclude_user_id: uuid.UUID | None = None) -> int:
    stmt = (
        select(func.count(func.distinct(User.id)))
        .select_from(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.code == RoleCode.ADMIN, User.status == UserStatus.ACTIVE)
    )
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    return (await db.execute(stmt)).scalar_one()


async def _assert_keeps_an_admin(db: AsyncSession, target: User) -> None:
    """目标当前是启用的 ADMIN 时，确保"除他之外"还有至少一名启用的 ADMIN。"""
    if not _is_active_admin(target):
        return
    if await _active_admin_count(db, exclude_user_id=target.id) < 1:
        raise ValidationFailed(
            "系统必须保留至少一名启用的管理员",
            detail={"field": "status", "user_id": str(target.id)},
        )


async def _assert_email_name_free(
    db: AsyncSession,
    *,
    email: str,
    name: str,
    exclude_user_id: uuid.UUID | None = None,
) -> None:
    conditions = [(User.email == email) | (User.name == name)]
    if exclude_user_id is not None:
        conditions.append(User.id != exclude_user_id)
    exists = (
        await db.execute(select(User.id).where(*conditions).limit(1))
    ).scalar_one_or_none()
    if exists is not None:
        # 分开判断，返回字段级错误（prd-05 §10：邮箱/用户名重复 → 409）
        email_stmt = select(User.id).where(User.email == email)
        name_stmt = select(User.id).where(User.name == name)
        if exclude_user_id is not None:
            email_stmt = email_stmt.where(User.id != exclude_user_id)
            name_stmt = name_stmt.where(User.id != exclude_user_id)
        field = "email" if (await db.execute(email_stmt)).scalar_one_or_none() else "name"
        message = "该邮箱已被注册" if field == "email" else "该用户名已被占用"
        raise Conflict(message, code="DUPLICATE_RESOURCE", detail={"field": field})


# ------------------------------------------------------------------ 用户
async def list_users(
    db: AsyncSession,
    *,
    page: int,
    page_size: int,
    keyword: str | None = None,
    role_codes: list[RoleCode] | None = None,
    status: UserStatus | None = None,
    sort: str = "created",
) -> Page[UserOut]:
    conditions = []
    if keyword:
        like = f"%{keyword.strip()}%"
        conditions.append(User.name.ilike(like) | User.email.ilike(like))
    if role_codes:
        conditions.append(
            User.id.in_(
                select(UserRole.user_id)
                .join(Role, Role.id == UserRole.role_id)
                .where(Role.code.in_(role_codes))
            )
        )
    if status is not None:
        conditions.append(User.status == status)

    total = (
        await db.execute(select(func.count()).select_from(User).where(*conditions))
    ).scalar_one()

    order = (
        User.last_login_at.desc().nulls_last()
        if sort == "last_login"
        else User.created_at.desc()
    )
    rows = (
        await db.execute(
            select(User, _project_count_subquery().label("project_count"))
            .options(selectinload(User.roles))
            .where(*conditions)
            .order_by(order, User.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    return Page[UserOut](
        items=[_to_out(u, count) for u, count in rows],
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )


async def create_user(
    db: AsyncSession, payload: UserCreateRequest, operator: User
) -> UserCreatedResponse:
    email = payload.email.lower()
    await _assert_email_name_free(db, email=email, name=payload.name)
    roles = await _resolve_roles(db, payload.role_codes)

    plain = payload.password or generate_password()
    user = User(
        name=payload.name,
        email=email,
        password_hash=hash_password(plain),
        status=payload.status,
    )
    user.roles = roles
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:  # 并发下的最后一道唯一约束
        await db.rollback()
        raise Conflict("用户名或邮箱已存在", code="DUPLICATE_RESOURCE") from exc

    audit_service.record(
        db,
        action=AuditAction.USER_CREATE,
        target_type="USER",
        user=operator,
        target_id=user.id,
        target_name=user.name,
        detail={
            "email": email,
            "roles": [r.code.value for r in roles],
            "status": payload.status.value,
            "password_generated": payload.password is None,
        },
    )
    await db.commit()
    user = await _load_user(db, user.id)
    base = _to_out(user)
    return UserCreatedResponse(**base.model_dump(), initial_password=plain)


async def update_user(
    db: AsyncSession, user_id: uuid.UUID, payload: UserUpdateRequest, operator: User
) -> UserOut:
    target = await _load_user(db, user_id)
    is_self = target.id == operator.id
    data = payload.model_dump(exclude_unset=True)

    new_email = data.get("email")
    new_name = data.get("name")
    if new_email is not None or new_name is not None:
        await _assert_email_name_free(
            db,
            email=(new_email or target.email).lower(),
            name=new_name or target.name,
            exclude_user_id=target.id,
        )

    role_codes: list[RoleCode] | None = data.get("role_codes")
    old_codes = {r.code for r in target.roles}
    new_roles: list[Role] | None = None
    if role_codes is not None:
        new_roles = await _resolve_roles(db, role_codes)
        new_codes = {r.code for r in new_roles}
        if is_self and RoleCode.ADMIN in old_codes and RoleCode.ADMIN not in new_codes:
            raise ValidationFailed(
                "不能移除自己的管理员角色", detail={"field": "role_codes"}
            )
        if RoleCode.ADMIN in old_codes and RoleCode.ADMIN not in new_codes:
            await _assert_keeps_an_admin(db, target)

    new_status: UserStatus | None = data.get("status")
    if new_status is not None and new_status is not target.status:
        if is_self and new_status is UserStatus.DISABLED:
            raise ValidationFailed(
                "不能禁用当前登录账号", detail={"field": "status"}
            )
        if new_status is UserStatus.DISABLED:
            await _assert_keeps_an_admin(db, target)

    changed: list[str] = []
    if new_name is not None and new_name != target.name:
        target.name = new_name
        changed.append("name")
    if new_email is not None and new_email.lower() != target.email:
        target.email = new_email.lower()
        changed.append("email")
    if new_roles is not None:
        target.roles = new_roles
        if {r.code for r in new_roles} != old_codes:
            changed.append("roles")
    if new_status is not None and new_status is not target.status:
        target.status = new_status
        if new_status is UserStatus.DISABLED:
            target.token_version += 1
        changed.append("status")

    if "roles" in changed and new_roles is not None:
        audit_service.record(
            db,
            action=AuditAction.USER_ROLE_UPDATE,
            target_type="USER",
            user=operator,
            target_id=target.id,
            target_name=target.name,
            detail={
                "from": sorted(c.value for c in old_codes),
                "to": [r.code.value for r in new_roles],
            },
        )
    if "status" in changed:
        action = (
            AuditAction.USER_DISABLE
            if target.status is UserStatus.DISABLED
            else AuditAction.USER_ENABLE
        )
        audit_service.record(
            db,
            action=action,
            target_type="USER",
            user=operator,
            target_id=target.id,
            target_name=target.name,
            detail={"status": target.status.value},
        )
    if {"name", "email"} & set(changed):
        audit_service.record(
            db,
            action=AuditAction.USER_UPDATE,
            target_type="USER",
            user=operator,
            target_id=target.id,
            target_name=target.name,
            detail={k: data[k] for k in ("name", "email") if k in changed},
        )

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise Conflict("用户名或邮箱已存在", code="DUPLICATE_RESOURCE") from exc

    target = await _load_user(db, user_id)
    count = (
        await db.execute(
            select(func.count()).select_from(ProjectMember).where(
                ProjectMember.user_id == user_id
            )
        )
    ).scalar_one()
    return _to_out(target, count)


async def reset_password(
    db: AsyncSession, user_id: uuid.UUID, operator: User
) -> ResetPasswordResponse:
    target = await _load_user(db, user_id)
    plain = generate_password()
    target.password_hash = hash_password(plain)
    # 重置后该用户所有 refresh token / access token 立即失效（prd-05 §5.4）
    target.token_version += 1
    audit_service.record(
        db,
        action=AuditAction.PASSWORD_RESET,
        target_type="USER",
        user=operator,
        target_id=target.id,
        target_name=target.name,
        detail={"status": target.status.value, "sessions_invalidated": True},
    )
    await db.commit()
    return ResetPasswordResponse(user_id=target.id, new_password=plain)


async def change_status(
    db: AsyncSession, user_id: uuid.UUID, payload: StatusChangeRequest, operator: User
) -> UserOut:
    target = await _load_user(db, user_id)
    if payload.status is target.status:
        return _to_out(target)

    if target.id == operator.id and payload.status is UserStatus.DISABLED:
        raise ValidationFailed("不能禁用当前登录账号", detail={"field": "status"})

    if payload.status is UserStatus.DISABLED:
        await _assert_keeps_an_admin(db, target)
        target.token_version += 1

    target.status = payload.status
    audit_service.record(
        db,
        action=(
            AuditAction.USER_DISABLE
            if payload.status is UserStatus.DISABLED
            else AuditAction.USER_ENABLE
        ),
        target_type="USER",
        user=operator,
        target_id=target.id,
        target_name=target.name,
        detail={"status": payload.status.value},
    )
    await db.commit()

    count = (
        await db.execute(
            select(func.count()).select_from(ProjectMember).where(
                ProjectMember.user_id == user_id
            )
        )
    ).scalar_one()
    return _to_out(target, count)


# ------------------------------------------------------------------ 角色 / 权限
async def list_roles(db: AsyncSession) -> list[RoleOut]:
    roles = (
        await db.execute(select(Role).options(selectinload(Role.permissions)))
    ).scalars().all()
    counts = dict(
        (
            await db.execute(
                select(UserRole.role_id, func.count()).group_by(UserRole.role_id)
            )
        ).all()
    )
    order = {code: i for i, code in enumerate(ROLE_ORDER)}
    roles = sorted(roles, key=lambda r: order.get(r.code, len(order)))
    return [
        RoleOut(
            id=r.id,
            code=r.code,
            name=r.name,
            description=r.description,
            is_system=r.is_system,
            user_count=counts.get(r.id, 0),
            permissions=sorted(p.code for p in r.permissions),
        )
        for r in roles
    ]


async def list_permissions(db: AsyncSession) -> list[PermissionGroupOut]:
    rows = (
        await db.execute(select(Permission).order_by(Permission.module, Permission.code))
    ).scalars().all()
    groups: dict[str, list[Permission]] = {}
    for perm in rows:
        groups.setdefault(perm.module, []).append(perm)

    order = {m: i for i, m in enumerate(MODULE_ORDER)}
    modules = sorted(groups, key=lambda m: order.get(m, len(order)))
    return [
        PermissionGroupOut(
            module=m,
            module_text=MODULE_TEXT.get(m, m),
            permissions=[
                PermissionOut(
                    id=p.id,
                    code=p.code,
                    name=p.name,
                    module=p.module,
                    description=p.description,
                )
                for p in groups[m]
            ],
        )
        for m in modules
    ]


async def get_user_out(db: AsyncSession, user_id: uuid.UUID) -> UserOut:
    user = await _load_user(db, user_id)
    count = (
        await db.execute(
            select(func.count()).select_from(ProjectMember).where(
                ProjectMember.user_id == user_id
            )
        )
    ).scalar_one()
    return _to_out(user, count)


def audit_action_options() -> list[AuditActionOption]:
    return [
        AuditActionOption(action=action.value, text=text)
        for action, text in AUDIT_ACTION_TEXT.items()
    ]
