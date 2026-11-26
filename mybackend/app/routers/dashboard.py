"""工作台路由：/api/v1/dashboard（prd-01 §6.1）"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.deps import DB, require
from app.models.user import User
from app.schemas.dashboard import DashboardOverview
from app.services import dashboard_service as svc

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get(
    "/overview",
    response_model=DashboardOverview,
    summary="三张统计卡 + HITL 横幅（P95 < 500ms）",
)
async def overview(
    db: DB,
    user: Annotated[User, Depends(require("project:read"))],
) -> DashboardOverview:
    return await svc.overview(db, user)
