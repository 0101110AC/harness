"""批注路由：/api/v1/annotations"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import DB, require
from app.core.errors import ValidationFailed
from app.models.enums import AnnotationStatus, Severity
from app.models.user import User
from app.schemas.annotation import (
    AnnotationCreateRequest,
    AnnotationOut,
    AnnotationTransitionRequest,
    AnnotationUpdateRequest,
    BatchTransitionRequest,
    BatchTransitionResponse,
    CommentCreateRequest,
    TimelineItemOut,
)
from app.schemas.common import MessageOut, Page
from app.services import annotation_service as svc
from app.services import annotation_timeline as timeline_svc

router = APIRouter(prefix="/annotations", tags=["annotations"])


@router.post(
    "",
    response_model=AnnotationOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建批注（人工）",
)
async def create_annotation(
    body: AnnotationCreateRequest,
    db: DB,
    user: Annotated[User, Depends(require("annotation:write"))],
) -> AnnotationOut:
    return await svc.create_annotation(db, body, user)


@router.get("", response_model=Page[AnnotationOut], summary="批注列表（跨项目 / 多维筛选）")
async def list_annotations(
    db: DB,
    user: Annotated[User, Depends(require("annotation:read"))],
    project_id: uuid.UUID | None = Query(default=None),
    drawing_id: uuid.UUID | None = Query(default=None),
    status_: AnnotationStatus | None = Query(default=None, alias="status"),
    assignee_id: str | None = Query(
        default=None, description="用户 UUID；传 me 表示当前登录人"
    ),
    severity: Severity | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=64),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Page[AnnotationOut]:
    assignee_uuid: uuid.UUID | None = None
    if assignee_id:
        if assignee_id.strip().lower() == "me":
            assignee_uuid = user.id
        else:
            try:
                assignee_uuid = uuid.UUID(assignee_id)
            except ValueError as exc:
                raise ValidationFailed(
                    "assignee_id 必须是用户 UUID 或 me",
                    detail={"field": "assignee_id"},
                ) from exc

    return await svc.list_annotations(
        db,
        user,
        page=page,
        page_size=page_size,
        project_id=project_id,
        drawing_id=drawing_id,
        status=status_,
        assignee_id=assignee_uuid,
        severity=severity,
        keyword=keyword,
    )


@router.get("/{annotation_id}", response_model=AnnotationOut, summary="批注详情")
async def get_annotation(
    annotation_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("annotation:read"))],
) -> AnnotationOut:
    return await svc.get_annotation(db, annotation_id, user)
@router.patch("/{annotation_id}", response_model=AnnotationOut, summary="编辑批注（乐观锁）")
async def update_annotation(
    annotation_id: uuid.UUID,
    body: AnnotationUpdateRequest,
    db: DB,
    user: Annotated[User, Depends(require("annotation:write"))],
) -> AnnotationOut:
    return await svc.update_annotation(db, annotation_id, body, user)


@router.delete("/{annotation_id}", response_model=MessageOut, summary="作废批注（软删除）")
async def delete_annotation(
    annotation_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("annotation:delete"))],
    remark: str | None = Query(default=None, max_length=500),
) -> MessageOut:
    await svc.delete_annotation(db, annotation_id, user, remark)
    return MessageOut(message="批注已作废")


# ---------------------------------------------------------------------------
# 状态流转（prd-03 §5）
# ---------------------------------------------------------------------------
@router.post(
    "/{annotation_id}/transition",
    response_model=AnnotationOut,
    summary="状态流转（下发 / 标记已修改 / 复核通过 / 复核不通过 / 作废）",
)
async def transition_annotation(
    annotation_id: uuid.UUID,
    body: AnnotationTransitionRequest,
    db: DB,
    user: Annotated[User, Depends(require("annotation:transition"))],
) -> AnnotationOut:
    return await svc.transition(db, annotation_id, body, user)


@router.post(
    "/batch-transition",
    response_model=BatchTransitionResponse,
    summary="批量流转（默认严格模式；skip_invalid=true 仅处理合法项）",
)
async def batch_transition(
    body: BatchTransitionRequest,
    db: DB,
    user: Annotated[User, Depends(require("annotation:transition"))],
) -> BatchTransitionResponse:
    return await svc.batch_transition(db, body, user)


# ---------------------------------------------------------------------------
# 时间线与评论（prd-03 §6.2 / §6.3）
# ---------------------------------------------------------------------------
@router.get(
    "/{annotation_id}/timeline",
    response_model=Page[TimelineItemOut],
    summary="时间线（评论 + 状态日志，按 created_at 正序）",
)
async def get_timeline(
    annotation_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("annotation:read"))],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Page[TimelineItemOut]:
    return await timeline_svc.get_timeline(
        db, annotation_id, user, page=page, page_size=page_size
    )


@router.post(
    "/{annotation_id}/comments",
    response_model=TimelineItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="发表评论",
)
async def create_comment(
    annotation_id: uuid.UUID,
    body: CommentCreateRequest,
    db: DB,
    user: Annotated[User, Depends(require("annotation:write"))],
) -> TimelineItemOut:
    return await timeline_svc.add_comment(db, annotation_id, body, user)


@router.delete(
    "/comments/{comment_id}",
    response_model=MessageOut,
    summary="删除本人评论（5 分钟内，软删除）",
)
async def delete_comment(
    comment_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("annotation:write"))],
) -> MessageOut:
    await timeline_svc.delete_comment(db, comment_id, user)
    return MessageOut(message="评论已删除")

