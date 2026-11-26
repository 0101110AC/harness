"""HITL 路由：待处理中断 / 风险恢复 / 报告确认（prd-07 §5.5.1）。"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.deps import DB, require
from app.models.drawing import Drawing  # noqa: F401  (OpenAPI 类型解析需要)
from app.models.user import User
from app.schemas.hitl import (
    ConfirmReportRequest,
    ConfirmReportResponse,
    HitlResumeRequest,
    HitlResumeResponse,
    InterruptTaskOut,
)
from app.services import hitl_service as svc

router = APIRouter(tags=["hitl"])


@router.get(
    "/drawings/{drawing_id}/hitl/pending",
    response_model=InterruptTaskOut | None,
    summary="当前待处理中断（刷新/首屏恢复弹窗用；null 表示无待处理）",
)
async def get_pending(
    drawing_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("review:read"))],
) -> InterruptTaskOut | None:
    return await svc.get_pending(db, drawing_id, user)


@router.post(
    "/drawings/{drawing_id}/hitl/resume",
    response_model=HitlResumeResponse,
    summary="提交风险确认（确认问题 / 特殊豁免）并恢复运行",
)
async def resume(
    drawing_id: uuid.UUID,
    body: HitlResumeRequest,
    db: DB,
    user: Annotated[User, Depends(require("annotation:write"))],
) -> HitlResumeResponse:
    return await svc.resume_risk(db, drawing_id, body, user)


@router.post(
    "/drawings/{drawing_id}/hitl/confirm-report",
    response_model=ConfirmReportResponse,
    summary="确认生成正式文档 / 退回修改",
)
async def confirm_report(
    drawing_id: uuid.UUID,
    body: ConfirmReportRequest,
    db: DB,
    user: Annotated[User, Depends(require("annotation:write"))],
) -> ConfirmReportResponse:
    return await svc.confirm_report(db, drawing_id, body, user)
