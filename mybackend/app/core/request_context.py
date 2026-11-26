"""请求级上下文：客户端 IP / User-Agent。

审计日志需要「操作人 + IP」，但 Service 层签名不应该被 `Request` 污染
（否则每个业务函数都要多一个与业务无关的参数，批量调用也无法复用）。
这里沿用 trace_id 的做法：中间件写入 ContextVar，Service 读取。

注意：`X-Forwarded-For` 只取第一跳，且仅在部署方确认反向代理可信时才有意义。
V1.0 单机直连场景下 `request.client.host` 才是真实来源，所以默认优先它。
"""
from __future__ import annotations

from contextvars import ContextVar

from starlette.requests import Request

_client_ip: ContextVar[str | None] = ContextVar("client_ip", default=None)
_user_agent: ContextVar[str | None] = ContextVar("user_agent", default=None)


def get_client_ip() -> str | None:
    return _client_ip.get()


def get_user_agent() -> str | None:
    return _user_agent.get()


async def request_context_middleware(request: Request, call_next):
    forwarded = request.headers.get("x-forwarded-for")
    ip = request.client.host if request.client else None
    if not ip and forwarded:
        ip = forwarded.split(",", 1)[0].strip()
    _client_ip.set(ip)
    _user_agent.set((request.headers.get("user-agent") or "")[:300] or None)
    return await call_next(request)
