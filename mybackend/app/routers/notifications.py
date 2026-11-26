"""站内信路由：/api/v1/notifications（prd-04 §10）"""
from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.core.deps import DB, require
from app.models.enums import NotificationType
from app.models.user import User
from app.schemas.common import Page
from app.schemas.notification import (
    NotificationOut,
    PreferencesOut,
    PreferencesUpdateRequest,
    ReadAllResponse,
    UnreadCountResponse,
)
from app.services import notification_service as svc

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=Page[NotificationOut], summary="消息列表（全部 / 未读 / 系统）")
async def list_notifications(
    db: DB,
    user: Annotated[User, Depends(require("notification:read"))],
    tab: Literal["all", "unread", "system"] = Query("all"),
    type_: NotificationType | None = Query(default=None, alias="type"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Page[NotificationOut]:
    return await svc.list_notifications(
        db, user, tab=tab, type_=type_, page=page, page_size=page_size
    )


@router.get(
    "/unread-count",
    response_model=UnreadCountResponse,
    summary="未读数（铃铛角标，P95 < 50ms）",
)
async def unread_count(
    db: DB,
    user: Annotated[User, Depends(require("notification:read"))],
) -> UnreadCountResponse:
    return await svc.unread_count(db, user)


@router.get(
    "/recent",
    response_model=list[NotificationOut],
    summary="最近 N 条（铃铛浮层）",
)
async def recent_notifications(
    db: DB,
    user: Annotated[User, Depends(require("notification:read"))],
    limit: int = Query(5, ge=1, le=20),
) -> list[NotificationOut]:
    return await svc.recent(db, user, limit=limit)


@router.get("/preferences", response_model=PreferencesOut, summary="通知偏好（V1.0 只读渠道）")
async def get_preferences(
    db: DB,
    user: Annotated[User, Depends(require("notification:read"))],
) -> PreferencesOut:
    return await svc.get_preferences(db, user)


@router.put("/preferences", response_model=PreferencesOut, summary="更新偏好（仅 event_types）")
async def update_preferences(
    body: PreferencesUpdateRequest,
    db: DB,
    user: Annotated[User, Depends(require("notification:read"))],
) -> PreferencesOut:
    return await svc.update_preferences(db, user, body)


@router.post("/read-all", response_model=ReadAllResponse, summary="全部已读")
async def read_all(
    db: DB,
    user: Annotated[User, Depends(require("notification:read"))],
) -> ReadAllResponse:
    return await svc.read_all(db, user)


@router.post(
    "/{notification_id}/read",
    response_model=NotificationOut,
    summary="单条已读（幂等）",
)
async def mark_read(
    notification_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("notification:read"))],
) -> NotificationOut:
    return await svc.mark_read(db, user, notification_id)
