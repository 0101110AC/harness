"""批注时间线与评论（prd-03 §6.2 / §6.3、prd-07 §6.7 TimelineItem）。

时间线由两张表合并：`annotation_status_logs`（系统记录，只增不改）
+ `annotation_comments`（人说的话，可 5 分钟内自删）。合并策略：

- 两条 SQL 各自按 (annotation_id, created_at) 索引取，**在 Python 里归并**而不是
  `UNION ALL`。原因是两表列集不同（状态枚举 vs 正文），跨方言 UNION 要为 NULL
  列补 `CAST`，SQLite/PG 行为还不一致；而时间线是**单条批注维度**的数据，
  行数天然有限（一条批注的评论在上百条量级），归并成本可以忽略。
- 排序一律 `created_at ASC`："评论 + 状态变更"的时间顺序就是审计顺序，
  倒序展示是前端的事（它只需要倒着渲染）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import ensure_project_member
from app.core.config import settings
from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.models.annotation import Annotation, AnnotationComment, AnnotationStatusLog
from app.models.enums import LOG_TYPE_TEXT, AnnotationStatus, LogType
from app.models.user import User
from app.schemas.annotation import CommentCreateRequest, TimelineItemOut
from app.schemas.auth import UserBrief
from app.schemas.common import Page

# 单条批注的时间线一次性最多取这么多行（超出说明数据异常，宁可截断也不要拖垮接口）
TIMELINE_MAX_ROWS = 2000

# 同一次流转会写多条日志（STATUS_CHANGED / CLOSED / VERSION_LINKED），
# 时间戳可能相同；用固定的语义顺序兜底，避免同秒记录的顺序随机。
LOG_ORDER: dict[LogType, int] = {
    LogType.CREATED: 0,
    LogType.STATUS_CHANGED: 1,
    LogType.ASSIGNED: 2,
    LogType.VERSION_LINKED: 3,
    LogType.CLOSED: 4,
    LogType.DELETED: 5,
    LogType.COMMENT: 6,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _get_or_404(db: AsyncSession, annotation_id: uuid.UUID) -> Annotation:
    ann = (
        await db.execute(
            select(Annotation).where(
                Annotation.id == annotation_id, Annotation.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    if ann is None:
        raise NotFound("批注不存在或已作废")
    return ann


async def _users(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, User]:
    if not ids:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    return {u.id: u for u in rows}


async def get_timeline(
    db: AsyncSession,
    annotation_id: uuid.UUID,
    user: User,
    *,
    page: int,
    page_size: int,
) -> Page[TimelineItemOut]:
    ann = await _get_or_404(db, annotation_id)
    await ensure_project_member(db, ann.project_id, user)

    logs = (
        await db.execute(
            select(AnnotationStatusLog)
            .where(AnnotationStatusLog.annotation_id == annotation_id)
            .order_by(AnnotationStatusLog.created_at.asc())
            .limit(TIMELINE_MAX_ROWS)
        )
    ).scalars().all()

    comments = (
        await db.execute(
            select(AnnotationComment)
            .where(AnnotationComment.annotation_id == annotation_id)
            .order_by(AnnotationComment.created_at.asc())
            .limit(TIMELINE_MAX_ROWS)
        )
    ).scalars().all()

    operators = await _users(db, {l.operator_id for l in logs} | {c.author_id for c in comments})
    now = _now()
    window = timedelta(seconds=settings.COMMENT_DELETE_WINDOW)

    merged: list[tuple[datetime, int, str, TimelineItemOut]] = []
    for item in logs:
        merged.append(
            (
                item.created_at,
                LOG_ORDER.get(item.log_type, 9),
                f"L{item.id}",
                TimelineItemOut(
                    id=item.id,
                    log_type=item.log_type,
                    log_type_text=LOG_TYPE_TEXT.get(item.log_type, item.log_type.value),
                    operator=_brief(operators.get(item.operator_id)),
                    from_status=item.from_status,
                    to_status=item.to_status,
                    remark=item.remark,
                    content=None,
                    can_delete=False,
                    created_at=item.created_at,
                ),
            )
        )

    for item in comments:
        can_delete = (
            not item.is_deleted
            and item.author_id == user.id
            and (now - item.created_at) <= window
        )
        merged.append(
            (
                item.created_at,
                LOG_ORDER[LogType.COMMENT],
                f"C{item.id}",
                TimelineItemOut(
                    id=item.id,
                    log_type=LogType.COMMENT,
                    log_type_text=LOG_TYPE_TEXT[LogType.COMMENT],
                    operator=_brief(operators.get(item.author_id)),
                    # 软删除的评论仍然留在时间线上（审计要求），content 置空 = "该评论已删除"
                    content=None if item.is_deleted else item.content,
                    can_delete=can_delete,
                    created_at=item.created_at,
                ),
            )
        )

    merged.sort(key=lambda row: (row[0], row[1], row[2]))
    total = len(merged)
    start = (page - 1) * page_size
    items = [row[3] for row in merged[start : start + page_size]]
    return Page[TimelineItemOut](
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )


def _brief(user: User | None) -> UserBrief:
    if user is None:  # 操作人已被删除：审计记录不能因为 join 不到人就消失
        return UserBrief(
            id=uuid.UUID(int=0), name="已注销用户", email="deleted@invalid.local", avatar_url=None
        )
    return UserBrief.model_validate(user)


async def add_comment(
    db: AsyncSession, annotation_id: uuid.UUID, payload: CommentCreateRequest, user: User
) -> TimelineItemOut:
    ann = await _get_or_404(db, annotation_id)
    await ensure_project_member(db, ann.project_id, user)

    if ann.status is AnnotationStatus.CLOSED:
        # prd-03 §6.3：V1.0 不提供"重新打开"，评论框直接禁用
        raise ValidationFailed(
            "批注已关闭，如需补充请让创建者重新开启",
            detail={"status": ann.status.value},
        )

    comment = AnnotationComment(
        annotation_id=annotation_id, author_id=user.id, content=payload.content.strip()
    )
    db.add(comment)
    await db.flush()

    # 物化计数 + updated_at：列表卡片按 updated_at 排序，有新评论要浮上来
    await db.execute(
        update(Annotation)
        .where(Annotation.id == annotation_id)
        .values(comment_count=Annotation.comment_count + 1, updated_at=_now())
    )
    await db.commit()
    await db.refresh(comment)  # created_at 是 server_default，提交后取一次真实值

    return TimelineItemOut(
        id=comment.id,
        log_type=LogType.COMMENT,
        log_type_text=LOG_TYPE_TEXT[LogType.COMMENT],
        operator=UserBrief.model_validate(user),
        content=comment.content,
        can_delete=True,
        created_at=comment.created_at,
    )


async def delete_comment(db: AsyncSession, comment_id: uuid.UUID, user: User) -> None:
    """删除本人 5 分钟内的评论（软删除）。"""
    comment = await db.get(AnnotationComment, comment_id)
    if comment is None:
        raise NotFound("评论不存在")
    ann = await _get_or_404(db, comment.annotation_id)
    await ensure_project_member(db, ann.project_id, user)

    if comment.author_id != user.id:
        # 表级规则：只允许本人删自己的评论，管理员也不例外（审计口径不因角色而变）
        raise Forbidden("只能删除自己的评论", detail={"author_id": str(comment.author_id)})
    if comment.is_deleted:
        return  # 幂等：重复删除返回成功，前端不用做状态推断

    if (_now() - comment.created_at) > timedelta(seconds=settings.COMMENT_DELETE_WINDOW):
        raise ValidationFailed(
            f"评论超过 {settings.COMMENT_DELETE_WINDOW // 60} 分钟，不可删除",
            detail={"window_seconds": settings.COMMENT_DELETE_WINDOW},
        )

    comment.is_deleted = True
    await db.execute(
        update(Annotation)
        .where(Annotation.id == comment.annotation_id)
        .values(
            comment_count=case(
                (Annotation.comment_count > 0, Annotation.comment_count - 1), else_=0
            )
        )
    )
    await db.commit()
