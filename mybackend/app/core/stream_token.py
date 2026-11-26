"""一次性 SSE 鉴权 token（prd-06 §9.1 / prd-07 §1.4）。

为什么不能直接用 access_token：
`EventSource` 规范不允许自定义 header，token 只能放 query string，
而 query string 会进 Nginx 访问日志、浏览器历史、Referer —— 长期有效的 JWT 放进去
等于把账号密码写进日志。所以换一张 **60s、绑定 user+thread、单次有效** 的票。

实现取舍（与 core/file_token.py 同一套思路，但多一层"一次性"）：
- HMAC-SHA256 签名，payload 只放 uid/thread_id/exp/nonce，路径与权限由 DB 反查；
- `consume()` 用进程内 nonce 表做一次性校验 —— MVP 单实例够用；
  多实例部署需把 nonce 换 Redis `SET NX EX`，接口不变。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.errors import Unauthorized

TOKEN_VERSION = 1
_DOMAIN = b"arch-harness:stream-token:"

# nonce → 过期时间戳（秒）。进程内单实例实现，多实例需 Redis。
_consumed: dict[str, float] = {}


def _key() -> bytes:
    return _DOMAIN + settings.JWT_SECRET.encode()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _prune() -> None:
    now = time.time()
    for nonce in [n for n, exp in _consumed.items() if exp < now]:
        _consumed.pop(nonce, None)


def sign_stream_token(
    user_id: uuid.UUID | str, thread_id: str, *, ttl: int | None = None
) -> tuple[str, datetime]:
    ttl = settings.STREAM_TOKEN_TTL if ttl is None else ttl
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    payload = {
        "v": TOKEN_VERSION,
        "u": str(user_id),
        "t": thread_id,
        "e": int(expires_at.timestamp()),
        "n": secrets.token_urlsafe(12),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"st_{body}.{sig}", expires_at


def _decode(token: str) -> dict:
    if not token.startswith("st_"):
        raise Unauthorized("实时通道票据无效", code="STREAM_TOKEN_INVALID")
    try:
        body, sig = token[3:].split(".", 1)
    except ValueError as exc:
        raise Unauthorized("实时通道票据无效", code="STREAM_TOKEN_INVALID") from exc

    expected = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        raise Unauthorized("实时通道票据无效", code="STREAM_TOKEN_INVALID")

    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise Unauthorized("实时通道票据无效", code="STREAM_TOKEN_INVALID") from exc

    if payload.get("v") != TOKEN_VERSION:
        raise Unauthorized("实时通道票据无效", code="STREAM_TOKEN_INVALID")
    if int(payload.get("e", 0)) < int(time.time()):
        raise Unauthorized("实时通道票据已过期，请重新获取", code="STREAM_TOKEN_EXPIRED")
    return payload


def consume_stream_token(token: str, *, thread_id: str) -> dict:
    """校验并消费票据：同一张票只能用一次，且必须匹配 thread_id。"""
    _prune()
    payload = _decode(token)

    if payload.get("t") != thread_id:
        raise Unauthorized("实时通道票据与任务不匹配", code="STREAM_TOKEN_MISMATCH")

    nonce = str(payload.get("n") or "")
    if not nonce or nonce in _consumed:
        raise Unauthorized("实时通道票据已使用，请重新获取", code="STREAM_TOKEN_INVALID")

    _consumed[nonce] = float(payload["e"])
    return payload


def reset_consumed() -> None:
    """测试钩子：清空 nonce 表。"""
    _consumed.clear()
