"""密码哈希与 JWT。

设计说明：
- 密码用 bcrypt；因 bcrypt 只取前 72 字节，统一先做 SHA-256 再喂给 bcrypt（等价 bcrypt_sha256），
  避免长密码/多字节密码被静默截断。
- JWT 用 PyJWT（python-jose 已停止维护且有已知 CVE）。
- token 载荷固定含 ver（users.token_version），禁用/改密时 +1 即可让旧 token 全失效，无需黑名单。
"""
from __future__ import annotations

import hashlib
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
import jwt

from app.core.config import settings
from app.core.errors import Unauthorized

TokenType = Literal["access", "refresh"]
BCRYPT_ROUNDS = 12


# ---------------- 密码 ----------------
def _prehash(password: str) -> bytes:
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode())
    except (ValueError, TypeError):
        return False


def generate_password(length: int = 12) -> str:
    """生成初始密码：字母 + 数字，满足 prd-05 §5.2 的强度要求。

    用 `secrets` 而不是 `random`：初始密码会经 HTTP 返回一次，属于凭证。
    固定塞入字母与数字各一个，避免"随机出来全是数字"被校验器拒绝。
    """
    if length < 8:
        raise ValueError("密码长度至少 8")
    alphabet = string.ascii_letters + string.digits
    body = [secrets.choice(alphabet) for _ in range(length - 2)]
    body.append(secrets.choice(string.ascii_uppercase + string.ascii_lowercase))
    body.append(secrets.choice(string.digits))
    secrets.SystemRandom().shuffle(body)
    return "".join(body)


# ---------------- JWT ----------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_token(
    subject: str,
    token_type: TokenType,
    *,
    token_version: int,
    expires_in: int | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    if expires_in is None:
        expires_in = (
            settings.JWT_EXPIRES if token_type == "access" else settings.JWT_REFRESH_EXPIRES
        )
    issued = _now()
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "ver": token_version,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(seconds=expires_in)).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_token_pair(
    subject: str, *, token_version: int, remember_me: bool = True
) -> tuple[str, str, int]:
    """返回 (access, refresh, access_expires_in)。"""
    access = create_token(subject, "access", token_version=token_version)
    refresh_ttl = (
        settings.JWT_REFRESH_EXPIRES if remember_me else settings.JWT_REFRESH_EXPIRES_SHORT
    )
    refresh = create_token(
        subject, "refresh", token_version=token_version, expires_in=refresh_ttl
    )
    return access, refresh, settings.JWT_EXPIRES


def decode_token(token: str, *, expected_type: TokenType | None = None) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("登录已过期，请重新登录", code="TOKEN_EXPIRED") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthorized("无效的凭证", code="UNAUTHORIZED") from exc

    if expected_type and payload.get("type") != expected_type:
        raise Unauthorized("凭证类型不匹配", code="UNAUTHORIZED")
    return payload


def expires_seconds(token_type: TokenType) -> int:
    return settings.JWT_EXPIRES if token_type == "access" else settings.JWT_REFRESH_EXPIRES


# ---------------- 登出（refresh token 吊销） ----------------
# V1.0 没有 Redis：用进程内集合记已登出的 refresh jti。
# 限制与登录失败计数一致：单实例有效、重启后清空；多实例部署必须换共享存储。
_REVOKED_REFRESH_JTIS: set[str] = set()
_REVOKED_LIMIT = 10_000


def revoke_refresh_token(token: str) -> bool:
    """把 refresh token 的 jti 加入吊销表；返回是否成功解析（过期/伪造返回 False）。"""
    try:
        payload = decode_token(token, expected_type="refresh")
    except Unauthorized:
        return False
    jti = str(payload.get("jti") or "")
    if not jti:
        return False
    if len(_REVOKED_REFRESH_JTIS) >= _REVOKED_LIMIT:
        # 粗暴但安全的上限：满了就清空，宁可多放行几个旧 token，也不让内存无限增长
        _REVOKED_REFRESH_JTIS.clear()
    _REVOKED_REFRESH_JTIS.add(jti)
    return True


def is_refresh_revoked(token: str) -> bool:
    try:
        payload = decode_token(token, expected_type="refresh")
    except Unauthorized:
        return True  # 解不开的凭证一律按"已失效"处理
    return str(payload.get("jti") or "") in _REVOKED_REFRESH_JTIS
