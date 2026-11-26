"""工作台聚合（prd-01 §6.1）。

性能目标 P95 < 500ms。实现上刻意避免"先拉全量再在 Python 里过滤"：
- 项目范围只算一次子查询，7 个指标全部是库端 count；
- 每个 count 都命中项目成员 / 指派人 / 状态索引；
- 不做 N+1、不 join 大表取明细。

口径说明（V1.0 无"问题采纳"独立写接口）：
`confirmed_issues_*` 取「AI 来源批注在本周/上周关闭」的数量，
`ai_block_rate_this_week` = 本周 AI 来源批注中已关闭数 / 本周 AI 来源批注总数。
这条口径写进复盘，避免前端把两个不同来源的数字强行解释成同一个。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import is_superuser
from app.models.agent import InterruptTask
from app.models.annotation import Annotation
from app.models.drawing import Drawing
from app.models.enums import (
    AnnotationSource,
    AnnotationStatus,
    DrawingStatus,
    InterruptStatus,
)
from app.models.project import Project, ProjectMember
from app.models.user import User
from app.schemas.dashboard import DashboardOverview

OPEN_STATUSES = (
    AnnotationStatus.PENDING_CONFIRM,
    AnnotationStatus.ISSUED,
    AnnotationStatus.MODIFIED,
)
PENDING_DRAWING_STATUSES = (
    DrawingStatus.UPLOADED,
    DrawingStatus.PARSING,
    DrawingStatus.REVIEWING,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _week_start(now: datetime) -> datetime:
    """本周一 00:00 UTC（与前端"本周"口径一致，不按本地时区切）。"""
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _project_scope(user: User) -> Select:
    """我可见的项目 ID 集合；ADMIN 看到所有未删除项目。"""
    if is_superuser(user):
        return select(Project.id).where(Project.is_deleted.is_(False))
    return (
        select(ProjectMember.project_id)
        .join(Project, Project.id == ProjectMember.project_id)
        .where(ProjectMember.user_id == user.id, Project.is_deleted.is_(False))
    )


async def overview(db: AsyncSession, user: User) -> DashboardOverview:
    now = _now()
    this_start = _week_start(now)
    last_start = this_start - timedelta(days=7)
    next_week = this_start + timedelta(days=7)
    scope = _project_scope(user)

    # 7 个指标合成 1 条 SQL：首页每次刷新都要跑，7 次往返在网络/连接池上就是
    # 固定的几十毫秒。单语句下 SQLite/PG 都能在一次执行里完成所有聚合。
    pending_drawings_sq = (
        select(func.count())
        .select_from(Drawing)
        .where(
            Drawing.project_id.in_(scope),
            Drawing.is_deleted.is_(False),
            Drawing.status.in_(PENDING_DRAWING_STATUSES),
        )
        .scalar_subquery()
    )
    in_review_sq = (
        select(func.count())
        .select_from(Drawing)
        .where(
            Drawing.project_id.in_(scope),
            Drawing.is_deleted.is_(False),
            Drawing.status == DrawingStatus.REVIEWING,
        )
        .scalar_subquery()
    )

    mine_open = (
        Annotation.assignee_id == user.id,
        Annotation.is_deleted.is_(False),
        Annotation.project_id.in_(scope),
        Annotation.status.in_(OPEN_STATUSES),
    )
    pending_ann_sq = (
        select(func.count()).select_from(Annotation).where(*mine_open).scalar_subquery()
    )
    to_review_sq = (
        select(func.count())
        .select_from(Annotation)
        .where(*mine_open, Annotation.status == AnnotationStatus.MODIFIED)
        .scalar_subquery()
    )

    ai_scope = (
        Annotation.source == AnnotationSource.AI,
        Annotation.is_deleted.is_(False),
        Annotation.project_id.in_(scope),
    )
    confirmed_this_sq = (
        select(func.count())
        .select_from(Annotation)
        .where(
            *ai_scope,
            Annotation.status == AnnotationStatus.CLOSED,
            Annotation.closed_at >= this_start,
            Annotation.closed_at < next_week,
        )
        .scalar_subquery()
    )
    confirmed_last_sq = (
        select(func.count())
        .select_from(Annotation)
        .where(
            *ai_scope,
            Annotation.status == AnnotationStatus.CLOSED,
            Annotation.closed_at >= last_start,
            Annotation.closed_at < this_start,
        )
        .scalar_subquery()
    )
    ai_created_sq = (
        select(func.count())
        .select_from(Annotation)
        .where(*ai_scope, Annotation.created_at >= this_start)
        .scalar_subquery()
    )
    ai_adopted_sq = (
        select(func.count())
        .select_from(Annotation)
        .where(
            *ai_scope,
            Annotation.created_at >= this_start,
            Annotation.status == AnnotationStatus.CLOSED,
        )
        .scalar_subquery()
    )
    pending_hitl_sq = (
        select(func.count())
        .select_from(InterruptTask)
        .where(
            InterruptTask.status == InterruptStatus.PENDING,
            InterruptTask.expires_at > now,
            InterruptTask.project_id.in_(scope),
        )
        .scalar_subquery()
    )

    row = (
        await db.execute(
            select(
                pending_drawings_sq,
                in_review_sq,
                pending_ann_sq,
                to_review_sq,
                confirmed_this_sq,
                confirmed_last_sq,
                ai_created_sq,
                ai_adopted_sq,
                pending_hitl_sq,
            )
        )
    ).one()
    (
        pending_drawings,
        pending_drawings_in_review,
        pending_annotations,
        pending_annotations_to_review,
        confirmed_this_week,
        confirmed_last_week,
        ai_created_this_week,
        ai_adopted_this_week,
        pending_hitl,
    ) = row

    wow_change = (
        round((confirmed_this_week - confirmed_last_week) / confirmed_last_week, 4)
        if confirmed_last_week
        else None
    )
    ai_block_rate = (
        round(ai_adopted_this_week / ai_created_this_week, 4) if ai_created_this_week else 0.0
    )

    return DashboardOverview(
        pending_drawings=pending_drawings,
        pending_drawings_in_review=pending_drawings_in_review,
        pending_annotations=pending_annotations,
        pending_annotations_to_review=pending_annotations_to_review,
        confirmed_issues_this_week=confirmed_this_week,
        confirmed_issues_last_week=confirmed_last_week,
        wow_change=wow_change,
        ai_block_rate_this_week=ai_block_rate,
        pending_hitl=pending_hitl,
    )
