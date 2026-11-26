"""应用入口：CORS、异常处理、路由挂载、OpenAPI（Bearer 可直接 Authorize 调试）。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from app.ai.runtime import run_manager
from app.core.config import settings
from app.core.exceptions import register_exception_handlers, trace_middleware
from app.core.request_context import request_context_middleware
from app.notify.outbox import consumer as outbox_consumer
from app.routers import (
    annotations,
    auth,
    dashboard,
    drawings,
    hitl,
    notifications,
    projects,
    review,
    system,
    uploads,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logging.getLogger("app").setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)
# 三方库日志压到 WARNING，避免 DEBUG=true 时被 aiosqlite 刷屏
for noisy in ("aiosqlite", "sqlalchemy", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

TAGS_METADATA = [
    {"name": "auth", "description": "认证与当前用户"},
    {"name": "dashboard", "description": "工作台"},
    {"name": "projects", "description": "项目与成员"},
    {"name": "drawings", "description": "图纸与上传"},
    {"name": "review", "description": "预审任务与问题"},
    {"name": "hitl", "description": "人工确认（风险 / 报告 / 校园退回）"},
    {"name": "stream", "description": "SSE 实时事件流"},
    {"name": "annotations", "description": "批注与时间线"},
    {"name": "notifications", "description": "站内信"},
    {"name": "admin", "description": "用户 / 角色 / 审计"},
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Checkpointer 与 Outbox 消费者都是应用级单例（prd-06 §12.3 / prd-04 §9.1）。"""
    await run_manager.startup()
    await outbox_consumer.startup()
    try:
        yield
    finally:
        await outbox_consumer.shutdown()
        await run_manager.shutdown()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",  # 与产品版本 V1.0 对齐
    description="建筑 AI 方案预审与图纸资料协同平台 · V1.0 (MVP)",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Trace-Id"],
)
app.middleware("http")(trace_middleware)
# 先写请求上下文（IP / UA），审计服务在 Service 层读取
app.middleware("http")(request_context_middleware)

register_exception_handlers(app)

app.include_router(auth.router, prefix=settings.API_V1_PREFIX)
app.include_router(dashboard.router, prefix=settings.API_V1_PREFIX)
app.include_router(projects.router, prefix=settings.API_V1_PREFIX)
app.include_router(drawings.router, prefix=settings.API_V1_PREFIX)
app.include_router(uploads.router, prefix=settings.API_V1_PREFIX)
app.include_router(annotations.router, prefix=settings.API_V1_PREFIX)
app.include_router(review.router, prefix=settings.API_V1_PREFIX)
app.include_router(hitl.router, prefix=settings.API_V1_PREFIX)
app.include_router(notifications.router, prefix=settings.API_V1_PREFIX)
app.include_router(system.router, prefix=settings.API_V1_PREFIX)


@app.get("/health", tags=["auth"], summary="健康检查")
async def health() -> dict:
    return {"status": "ok", "version": "1.0.0"}


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title, version=app.version, description=app.description, routes=app.routes
    )
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }
    schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
