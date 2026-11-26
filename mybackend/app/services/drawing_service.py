"""图纸查询与删除（上传在 D3 实现）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import ensure_project_member
from app.core.errors import NotFound
from app.models.drawing import Drawing
from app.models.enums import DRAWING_STATUS_TEXT, DrawingStatus, IssueStatus, Severity
from app.models.project import Project
from app.models.review import ReviewIssue, ReviewTask
from app.models.user import User
from app.schemas.auth import UserBrief
from app.schemas.common import Page
from app.schemas.drawing import DrawingOut

# 只统计"有效问题"：驳回/忽略不计，避免 issue_stat 虚高
COUNTED_ISSUE_STATUSES = (IssueStatus.PENDING, IssueStatus.ADOPTED)


def _to_out(drawing: Drawing, uploader: User, issue_stat: dict | None) -> DrawingOut:
    return DrawingOut(
        id=drawing.id,
        project_id=drawing.project_id,
        name=drawing.name,
        file_name=drawing.file_name,
        file_type=drawing.file_type,
        file_size=drawing.file_size,
        version=drawing.version,
        parent_drawing_id=drawing.parent_drawing_id,
        is_current=drawing.is_current,
        status=drawing.status,
        status_text=DRAWING_STATUS_TEXT[drawing.status],
        page_count=drawing.page_count,
        issue_stat=issue_stat,
        parse_error=drawing.parse_error,
        uploaded_by=UserBrief.model_validate(uploader),
        created_at=drawing.created_at,
        updated_at=drawing.updated_at,
    )


async def _issue_stat_map(
    db: AsyncSession, drawing_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, int]]:
    """批量取"每张图纸最近一次预审任务"的问题统计，避免逐图查询（N+1）。"""
    if not drawing_ids:
        return {}

    latest = (
        select(ReviewTask.drawing_id, func.max(ReviewTask.created_at).label("ts"))
        .where(ReviewTask.drawing_id.in_(drawing_ids))
        .group_by(ReviewTask.drawing_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(ReviewIssue.drawing_id, ReviewIssue.severity, func.count())
            .join(ReviewTask, ReviewTask.id == ReviewIssue.review_task_id)
            .join(
                latest,
                (ReviewTask.drawing_id == latest.c.drawing_id)
                & (ReviewTask.created_at == latest.c.ts),
            )
            .where(
                ReviewIssue.drawing_id.in_(drawing_ids),
                ReviewIssue.status.in_(COUNTED_ISSUE_STATUSES),
            )
            .group_by(ReviewIssue.drawing_id, ReviewIssue.severity)
        )
    ).all()

    result: dict[uuid.UUID, dict[str, int]] = {}
    for did, severity, count in rows:
        bucket = result.setdefault(did, {s.value: 0 for s in Severity})
        bucket[severity.value] = count
    return result


async def _get_or_404(db: AsyncSession, drawing_id: uuid.UUID) -> Drawing:
    drawing = (
        await db.execute(
            select(Drawing).where(Drawing.id == drawing_id, Drawing.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if drawing is None:
        raise NotFound("图纸不存在或已删除")
    return drawing


async def list_drawings(
    db: AsyncSession,
    user: User,
    *,
    project_id: uuid.UUID,
    page: int,
    page_size: int,
    status: DrawingStatus | None,
    is_current: bool | None,
) -> Page[DrawingOut]:
    await ensure_project_member(db, project_id, user)

    # 项目存在性单独判（避免把"无权限"误报成"空列表"）
    exists = (
        await db.execute(
            select(Project.id).where(Project.id == project_id, Project.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if exists is None:
        raise NotFound("项目不存在或已删除")

    conditions = [Drawing.project_id == project_id, Drawing.is_deleted.is_(False)]
    if status is not None:
        conditions.append(Drawing.status == status)
    if is_current is not None:
        conditions.append(Drawing.is_current.is_(is_current))

    total = (
        await db.execute(select(func.count()).select_from(Drawing).where(*conditions))
    ).scalar_one()

    drawings = (
        await db.execute(
            select(Drawing)
            .where(*conditions)
            .order_by(Drawing.is_current.desc(), Drawing.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    # 上传人一次性批量查（替代逐行 lazy load）
    uploader_ids = {d.uploaded_by for d in drawings}
    uploaders: dict[uuid.UUID, User] = {}
    if uploader_ids:
        rows = (await db.execute(select(User).where(User.id.in_(uploader_ids)))).scalars().all()
        uploaders = {u.id: u for u in rows}

    stats = await _issue_stat_map(db, [d.id for d in drawings])

    items = [
        _to_out(d, uploaders[d.uploaded_by], stats.get(d.id, {s.value: 0 for s in Severity}))
        for d in drawings
    ]
    return Page[DrawingOut](
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )


async def get_drawing(db: AsyncSession, drawing_id: uuid.UUID, user: User) -> DrawingOut:
    drawing = await _get_or_404(db, drawing_id)
    await ensure_project_member(db, drawing.project_id, user)

    uploader = (await db.execute(select(User).where(User.id == drawing.uploaded_by))).scalar_one()
    stats = await _issue_stat_map(db, [drawing.id])
    return _to_out(drawing, uploader, stats.get(drawing.id, {s.value: 0 for s in Severity}))


async def delete_drawing(db: AsyncSession, drawing_id: uuid.UUID, user: User) -> None:
    drawing = await _get_or_404(db, drawing_id)
    await ensure_project_member(db, drawing.project_id, user)

    was_current = drawing.is_current
    drawing.is_deleted = True
    drawing.is_current = False
    drawing.updated_at = datetime.now(timezone.utc)

    # 维护物化计数：只有删掉"当前版本"才让项目图纸数减 1（原子自减，避免丢弃更新）
    if was_current:
        await db.execute(
            update(Project)
            .where(Project.id == drawing.project_id, Project.drawing_count > 0)
            .values(drawing_count=Project.drawing_count - 1)
        )

    await db.commit()
