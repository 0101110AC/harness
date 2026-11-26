"""trace_id 上下文 + 全局异常处理器。"""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, STATUS_DEFAULT_CODE

log = logging.getLogger(__name__)

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


def new_trace_id() -> str:
    return str(uuid.uuid4())


def get_trace_id() -> str:
    return _trace_id.get()


def set_trace_id(tid: str) -> None:
    _trace_id.set(tid)


def _payload(code: str, message: str, detail: dict | None = None) -> dict:
    return {"code": code, "message": message, "detail": detail, "trace_id": get_trace_id()}


async def trace_middleware(request: Request, call_next):
    tid = request.headers.get("X-Request-Id") or new_trace_id()
    set_trace_id(tid)
    response = await call_next(request)
    response.headers["X-Trace-Id"] = tid
    return response


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(exc.code, exc.message, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", [])[1:]) or "body"
        msg = first.get("msg", "参数校验失败")
        return JSONResponse(
            status_code=422,
            content=_payload(
                "VALIDATION_ERROR",
                f"{loc}: {msg}",
                {"field": loc, "errors": [
                    {"loc": ".".join(str(p) for p in e.get("loc", [])), "msg": e.get("msg")}
                    for e in exc.errors()
                ]},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = STATUS_DEFAULT_CODE.get(exc.status_code, "ERROR")
        detail = exc.detail if isinstance(exc.detail, dict) else None
        message = exc.detail if isinstance(exc.detail, str) else code
        headers = getattr(exc, "headers", None) or {}
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(code, message, detail),
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=_payload("INTERNAL_ERROR", "服务异常，请稍后重试"),
        )
