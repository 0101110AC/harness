"""图纸路由：/api/v1/drawings（上传在 D3）"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.deps import DB, require
from app.models.drawing import Drawing  # noqa: F401  (OpenAPI 类型解析需要)
from app.models.enums import DrawingStatus
from app.models.user import User
from app.schemas.common import MessageOut, Page
from app.schemas.drawing import DrawingOut
from app.services import drawing_service as svc

router = APIRouter(prefix="/drawings", tags=["drawings"])


@router.get("", response_model=Page[DrawingOut], summary="图纸列表（按项目）")
async def list_drawings(
    db: DB,
    user: Annotated[User, Depends(require("drawing:read"))],
    project_id: uuid.UUID = Query(..., description="项目 ID（必填，用于数据隔离）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    status_: DrawingStatus | None = Query(default=None, alias="status"),
    is_current: bool | None = Query(default=None),
) -> Page[DrawingOut]:
    return await svc.list_drawings(
        db,
        user,
        project_id=project_id,
        page=page,
        page_size=page_size,
        status=status_,
        is_current=is_current,
    )


@router.get("/{drawing_id}", response_model=DrawingOut, summary="图纸详情（含问题统计）")
async def get_drawing(
    drawing_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("drawing:read"))],
) -> DrawingOut:
    return await svc.get_drawing(db, drawing_id, user)


@router.delete("/{drawing_id}", response_model=MessageOut, summary="删除图纸（软删除）")
async def delete_drawing(
    drawing_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("drawing:delete"))],
) -> MessageOut:
    await svc.delete_drawing(db, drawing_id, user)
    return MessageOut(message="图纸已删除")
