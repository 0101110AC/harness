"""统一错误体：{"code": "...", "message": "...", "detail": {...}, "trace_id": "..."}"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    """所有业务异常的统一基类；由全局异常处理器转成统一响应体。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail


class BadRequest(AppError):
    def __init__(self, message: str = "请求格式错误", detail: dict | None = None) -> None:
        super().__init__("BAD_REQUEST", message, 400, detail)


class Unauthorized(AppError):
    def __init__(self, message: str = "未登录或登录已失效", code: str = "UNAUTHORIZED") -> None:
        super().__init__(code, message, 401)


class Forbidden(AppError):
    def __init__(self, message: str = "无权限访问该资源", code: str = "FORBIDDEN",
                 detail: dict | None = None) -> None:
        super().__init__(code, message, 403, detail)


class NotFound(AppError):
    def __init__(self, message: str = "资源不存在") -> None:
        super().__init__("NOT_FOUND", message, 404)


class Conflict(AppError):
    def __init__(self, message: str, code: str = "CONFLICT",
                 detail: dict | None = None) -> None:
        super().__init__(code, message, 409, detail)


class ValidationFailed(AppError):
    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__("VALIDATION_ERROR", message, 422, detail)


class InvalidStateTransition(AppError):
    """状态机非法流转（prd-03 §5.1 / prd-07 §7.6）：422，不是 409。

    409 留给乐观锁冲突（"别人刚改过"），422 表示"在当前状态下这个动作本身不成立"。
    两者混用会让前端无法决定"刷新重试"还是"这个动作根本不该出现"。
    """

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__("INVALID_STATE_TRANSITION", message, 422, detail)


class RateLimited(AppError):
    def __init__(self, message: str = "操作过于频繁，请稍后再试") -> None:
        super().__init__("RATE_LIMITED", message, 429)


# HTTP 状态码 → 默认错误码（prd-07 §1.3）
STATUS_DEFAULT_CODE: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    501: "NOT_IMPLEMENTED",
}
