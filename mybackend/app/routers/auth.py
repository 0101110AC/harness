"""认证路由：login / refresh / me（+ register，prd-07 §5.1 匿名接口）。"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.deps import CurrentUser, DB, get_user_permissions, is_superuser
from app.core.errors import Conflict, Forbidden, RateLimited, Unauthorized
from app.core.menus import build_menus
from app.core.security import (
    create_token_pair,
    decode_token,
    hash_password,
    is_refresh_revoked,
    revoke_refresh_token,
    verify_password,
)
from app.core.config import settings
from app.models.enums import NotificationChannel, RoleCode, UserStatus
from app.models.notification import NotificationPreference
from app.models.user import Role, User
from app.schemas.common import MessageOut
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserMeResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# 单实例登录失败计数（生产多实例请换 Redis；D1 明确此限制）
_fail_counter: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(key: str) -> None:
    now = time.time()
    window = settings.LOGIN_LOCK_SECONDS
    hits = [t for t in _fail_counter[key] if now - t < window]
    _fail_counter[key] = hits
    if len(hits) >= settings.LOGIN_MAX_FAILURES:
        raise RateLimited(f"尝试过于频繁，请 {window // 60} 分钟后再试")


def _mark_failure(key: str) -> None:
    _fail_counter[key].append(time.time())


def _to_me(user: User, permissions: set[str], superuser: bool) -> UserMeResponse:
    return UserMeResponse(
        id=user.id,
        name=user.name,
        email=user.email,
        avatar_url=user.avatar_url,
        roles=[r.code.value for r in user.roles],
        permissions=sorted(permissions) if not superuser else ["*"],
        menus=build_menus(permissions, superuser),
    )


@router.post("/register", response_model=TokenResponse, summary="注册并登录")
async def register(body: RegisterRequest, db: DB) -> TokenResponse:
    email = body.email.lower()
    exists = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if exists:
        raise Conflict("该邮箱已被注册", code="DUPLICATE_RESOURCE", detail={"field": "email"})

    role = (
        await db.execute(select(Role).where(Role.code == RoleCode.REVIEWER))
    ).scalar_one_or_none()
    if role is None:
        raise Forbidden("系统角色未初始化，请先执行 seed", code="FORBIDDEN")

    user = User(name=body.name, email=email, password_hash=hash_password(body.password))
    user.roles = [role]
    db.add(user)
    try:
        await db.flush()
        db.add(
            NotificationPreference(
                user_id=user.id, channel=NotificationChannel.INAPP, enabled=True, event_types=[]
            )
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise Conflict("用户名或邮箱已存在", code="DUPLICATE_RESOURCE") from exc

    access, refresh, expires_in = create_token_pair(
        str(user.id), token_version=user.token_version, remember_me=True
    )
    return TokenResponse(access_token=access, refresh_token=refresh, expires_in=expires_in)


@router.post("/login", response_model=TokenResponse, summary="登录")
async def login(body: LoginRequest, request: Request, db: DB) -> TokenResponse:
    email = body.email.lower()
    _check_rate_limit(email)

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()

    # 统一提示，不区分"邮箱不存在"与"密码错误"，防账号枚举
    if user is None or not verify_password(body.password, user.password_hash):
        _mark_failure(email)
        raise Unauthorized("邮箱或密码错误", code="INVALID_CREDENTIALS")

    if user.status is not UserStatus.ACTIVE:
        raise Forbidden("账号已被禁用，请联系管理员", code="ACCOUNT_DISABLED")

    _fail_counter.pop(email, None)
    user.last_login_at = datetime.now(timezone.utc)
    user.last_login_ip = request.client.host if request.client else None
    await db.commit()

    access, refresh, expires_in = create_token_pair(
        str(user.id), token_version=user.token_version, remember_me=body.remember_me
    )
    return TokenResponse(access_token=access, refresh_token=refresh, expires_in=expires_in)


@router.post("/refresh", response_model=TokenResponse, summary="刷新访问令牌")
async def refresh(body: RefreshRequest, db: DB) -> TokenResponse:
    if is_refresh_revoked(body.refresh_token):
        # 已登出的 refresh token 不能再换 access
        raise Unauthorized("登录状态已失效，请重新登录", code="TOKEN_EXPIRED")

    payload = decode_token(body.refresh_token, expected_type="refresh")

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise Unauthorized("无效的凭证") from exc

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise Unauthorized("用户不存在或已被删除")
    if user.status is not UserStatus.ACTIVE:
        raise Forbidden("账号已被禁用，请联系管理员", code="ACCOUNT_DISABLED")
    if int(payload.get("ver", -1)) != int(user.token_version):
        raise Unauthorized("登录状态已失效，请重新登录", code="TOKEN_EXPIRED")

    access, new_refresh, expires_in = create_token_pair(
        str(user.id), token_version=user.token_version, remember_me=True
    )
    return TokenResponse(access_token=access, refresh_token=new_refresh, expires_in=expires_in)


@router.post("/logout", response_model=MessageOut, summary="登出（refresh token 立即失效）")
async def logout(body: RefreshRequest) -> MessageOut:
    """不需要 access token：access 过期时也应该能登出。

    只吊销 body 里的 refresh token；伪造/过期凭证按"已失效"处理，不报错，
    避免把"这个 token 是否有效"变成探测接口。
    """
    revoke_refresh_token(body.refresh_token)
    return MessageOut(message="已登出")


@router.get("/me", response_model=UserMeResponse, summary="当前用户 + 权限 + 菜单")
async def me(user: CurrentUser) -> UserMeResponse:
    permissions = await get_user_permissions(user)
    return _to_me(user, permissions, is_superuser(user))
