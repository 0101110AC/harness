"""短时签名 URL：HMAC 能力令牌（不落库、不可篡改、可过期）。

设计取舍：
- 不用 JWT 库，避免把"文件下载"和"身份令牌"混在同一条密钥语义里；
- payload 只放 drawing_id + exp，不放路径 —— 路径由 DB 反查，杜绝伪造 key；
- 签名用 `hmac.compare_digest` 常量时间比较，防时序侧信道。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.errors import Unauthorized

TOKEN_VERSION = 1
_DOMAIN = b"arch-harness:file-token:"


def _key() -> bytes:
    return _DOMAIN + settings.JWT_SECRET.encode()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_file_token(drawing_id: uuid.UUID, *, ttl: int | None = None) -> tuple[str, datetime]:
    ttl = settings.FILE_URL_TTL if ttl is None else ttl
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    payload = {
        "v": TOKEN_VERSION,
        "d": str(drawing_id),
        "e": int(expires_at.timestamp()),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}", expires_at


def verify_file_token(token: str) -> uuid.UUID:
    try:
        body, sig = token.split(".", 1)
    except ValueError as exc:
        raise Unauthorized("下载链接无效", code="UNAUTHORIZED") from exc

    expected = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        raise Unauthorized("下载链接无效", code="UNAUTHORIZED")

    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise Unauthorized("下载链接无效", code="UNAUTHORIZED") from exc

    if payload.get("v") != TOKEN_VERSION:
        raise Unauthorized("下载链接无效", code="UNAUTHORIZED")

    if int(payload.get("e", 0)) < int(datetime.now(timezone.utc).timestamp()):
        raise Unauthorized("下载链接已过期，请重新获取", code="TOKEN_EXPIRED")

    try:
        return uuid.UUID(payload["d"])
    except (KeyError, ValueError) as exc:
        raise Unauthorized("下载链接无效", code="UNAUTHORIZED") from exc
