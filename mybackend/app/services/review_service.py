"""预审任务：创建 / 快照 / 问题列表 / 报告初稿 / SSE 票据（prd-07 §5.5~§5.5.1）。

数据权限：所有入口都先 `ensure_project_member(db, drawing.project_id, user)`，
把"这条记录是不是我的"挡在 Service 层（Router 只做功能权限）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.runtime import run_manager
from app.core.config import settings
from app.core.deps import ensure_project_member
from app.core.errors import Conflict, NotFound
from app.core.stream_token import sign_stream_token
from app.models.annotation import Annotation
from app.models.drawing import Drawing
from app.models.enums import (
    DISCIPLINE_TEXT,
    ISSUE_CATEGORY_TEXT,
    ISSUE_STATUS_TEXT,
    REPORT_STATUS_TEXT,
    RUN_STATUS_TEXT,
    REVIEW_STAGE_TEXT,
    SEVERITY_TEXT,
    AnnotationSource,
    AnnotationStatus,
    IssueStatus,
    ReviewMode,
    ReviewStage,
    ReviewTaskStatus,
    RunStatus,
    Severity,
    ShapeType,
)
from app.models.project import Project
from app.models.review import ReportDraft, ReviewIssue, ReviewTask
from app.models.user import User
from app.schemas.common import Page
from app.schemas.drawing import Location
from app.schemas.review import (
    BatchAdoptOut,
    BatchAdoptRequest,
    IssueOut,
    ReportDraftOut,
    ReviewTaskBrief,
    ReviewTaskCreateRequest,
    ReviewTaskSnapshot,
    StreamTokenOut,
)

ACTIVE_RUN_STATUSES = (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.WAITING_HUMAN)


async def _get_drawing(db: AsyncSession, drawing_id: uuid.UUID, user: User) -> Drawing:
    drawing = (
        await db.execute(
            select(Drawing).where(Drawing.id == drawing_id, Drawing.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if drawing is None:
        raise NotFound("图纸不存在或已删除")
    await ensure_project_member(db, drawing.project_id, user)
    return drawing


def to_snapshot(task: ReviewTask) -> ReviewTaskSnapshot:
    stage = task.stage
    return ReviewTaskSnapshot(
        id=task.id,
        drawing_id=task.drawing_id,
        status=task.status,
        status_text=task.status.value if isinstance(task.status, ReviewTaskStatus) else str(task.status),
        stage=stage,
        stage_text=task.stage_text
        or (REVIEW_STAGE_TEXT.get(stage) if stage is not None else None),
        progress=task.progress,
        engine=task.engine,
        issue_total=task.issue_total,
        issue_blocker=task.issue_blocker,
        summary=task.summary,
        error_reason=task.error_reason,
        cost_ms=task.cost_ms,
        created_at=task.created_at,
        finished_at=task.finished_at,
        thread_id=task.thread_id,
        run_status=task.run_status,
        run_status_text=RUN_STATUS_TEXT.get(task.run_status, str(task.run_status)),
        current_node=task.current_node,
        review_mode=task.review_mode or ReviewMode.PROFESSIONAL,
        waiting_interrupt_id=task.waiting_interrupt_id,
        report_id=task.report_id,
        last_seq=task.last_seq,
    )


async def create_review_task(
    db: AsyncSession,
    drawing_id: uuid.UUID,
    body: ReviewTaskCreateRequest,
    user: User,
) -> ReviewTaskSnapshot:
    drawing = await _get_drawing(db, drawing_id, user)

    # 同一张图纸同时只允许一条在跑的预审（否则两个 run 会争同一批问题和报告）
    running = (
        await db.execute(
            select(ReviewTask.id).where(
                ReviewTask.drawing_id == drawing.id,
                ReviewTask.run_status.in_(ACTIVE_RUN_STATUSES),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if running is not None:
        raise Conflict(
            "该图纸已有进行中的预审任务，请等待完成或取消后再试",
            code="REVIEW_ALREADY_RUNNING",
            detail={"review_task_id": str(running)},
        )

    task_id = uuid.uuid4()
    task = ReviewTask(
        id=task_id,
        project_id=drawing.project_id,
        drawing_id=drawing.id,
        status=ReviewTaskStatus.PENDING,
        stage=None,
        stage_text=None,
        progress=0,
        engine=settings.REVIEW_ENGINE,
        issue_total=0,
        issue_blocker=0,
        created_by=user.id,
        # thread_id 就是 review_task.id：checkpoint 的主键维度，也是 SSE 的定位 key
        thread_id=str(task_id),
        run_id=str(uuid.uuid4()),
        run_status=RunStatus.PENDING,
        current_node="load_context",
        review_mode=body.review_mode,
        last_seq=0,
    )
    db.add(task)
    await db.commit()

    snapshot = to_snapshot(task)

    # 后台异步跑图：HTTP 立刻返回 202，进度全部走 SSE（禁止前端轮询）
    await run_manager.start(
        thread_id=task.thread_id,
        run_id=str(task.run_id),
        project_id=str(task.project_id),
        drawing_id=str(task.drawing_id),
        review_mode=(task.review_mode or ReviewMode.PROFESSIONAL).value,
    )
    return snapshot


async def get_task(db: AsyncSession, task_id: uuid.UUID, user: User) -> ReviewTaskSnapshot:
    task = (
        await db.execute(select(ReviewTask).where(ReviewTask.id == task_id))
    ).scalar_one_or_none()
    if task is None:
        raise NotFound("预审任务不存在")
    await ensure_project_member(db, task.project_id, user)
    return to_snapshot(task)


async def get_task_by_thread(db: AsyncSession, thread_id: str, user: User) -> ReviewTaskSnapshot:
    task = (
        await db.execute(select(ReviewTask).where(ReviewTask.thread_id == thread_id))
    ).scalar_one_or_none()
    if task is None:
        raise NotFound("预审任务不存在")
    await ensure_project_member(db, task.project_id, user)
    return to_snapshot(task)


SEVERITY_ORDER = case(
    (ReviewIssue.severity == Severity.BLOCKER, 0),
    (ReviewIssue.severity == Severity.MAJOR, 1),
    (ReviewIssue.severity == Severity.MINOR, 2),
    else_=3,
)


async def list_issues(
    db: AsyncSession,
    task_id: uuid.UUID,
    user: User,
    *,
    page: int,
    page_size: int,
    severity: list[Severity] | None = None,
    discipline: list[str] | None = None,
    status: list[IssueStatus] | None = None,
    keyword: str | None = None,
) -> Page[IssueOut]:
    task = (
        await db.execute(select(ReviewTask).where(ReviewTask.id == task_id))
    ).scalar_one_or_none()
    if task is None:
        raise NotFound("预审任务不存在")
    await ensure_project_member(db, task.project_id, user)

    conditions = [ReviewIssue.review_task_id == task.id]
    if severity:
        conditions.append(ReviewIssue.severity.in_(severity))
    if discipline:
        conditions.append(ReviewIssue.discipline.in_(discipline))
    if status:
        conditions.append(ReviewIssue.status.in_(status))
    if keyword:
        conditions.append(ReviewIssue.title.contains(keyword))

    total = (
        await db.execute(select(func.count()).select_from(ReviewIssue).where(*conditions))
    ).scalar_one()
    rows = (
        await db.execute(
            select(ReviewIssue)
            .where(*conditions)
            .order_by(SEVERITY_ORDER, ReviewIssue.confidence.desc(), ReviewIssue.created_at)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    items = [to_issue_out(row) for row in rows]
    return Page[IssueOut](
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )


async def list_tasks(
    db: AsyncSession,
    drawing_id: uuid.UUID,
    user: User,
    *,
    page: int = 1,
    page_size: int = 5,
) -> Page[ReviewTaskBrief]:
    """图纸下的预审任务（按创建时间倒序）。

    这是「查看预审 / 刷新预审中心」唯一的任务发现通道：没有它，前端 listTasks()
    只能拿到空列表，页面退化成"这张图纸还没有预审任务"。
    """
    drawing = await _get_drawing(db, drawing_id, user)
    await ensure_project_member(db, drawing.project_id, user)

    total = (
        await db.execute(
            select(func.count())
            .select_from(ReviewTask)
            .where(ReviewTask.drawing_id == drawing.id)
        )
    ).scalar_one()

    rows = (
        await db.execute(
            select(ReviewTask)
            .where(ReviewTask.drawing_id == drawing.id)
            .order_by(ReviewTask.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    items = [
        ReviewTaskBrief(
            id=row.id,
            thread_id=str(row.thread_id),
            drawing_id=row.drawing_id,
            status=row.status,
            status_text=row.status.value if isinstance(row.status, ReviewTaskStatus) else str(row.status),
            run_status=row.run_status,
            progress=row.progress,
            issue_total=row.issue_total,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return Page[ReviewTaskBrief](
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )


def to_issue_out(row: ReviewIssue) -> IssueOut:
    """单一出口：问题 → IssueOut（列表与采纳接口共用，避免两处文案口径漂移）。"""
    return IssueOut(
        id=row.id,
        review_task_id=row.review_task_id,
        drawing_id=row.drawing_id,
        category=row.category,
        category_text=ISSUE_CATEGORY_TEXT[row.category],
        severity=row.severity,
        severity_text=SEVERITY_TEXT[row.severity],
        discipline=row.discipline,
        discipline_text=DISCIPLINE_TEXT[row.discipline],
        title=row.title,
        description=row.description,
        suggestion=row.suggestion,
        rule_code=row.rule_code,
        confidence=row.confidence,
        location=Location.model_validate(row.location),
        location_valid=row.location_valid,
        status=row.status,
        status_text=ISSUE_STATUS_TEXT[row.status],
        annotation_id=row.annotation_id,
        created_at=row.created_at,
    )


async def _adopt_one(db: AsyncSession, issue: ReviewIssue, user: User) -> uuid.UUID:
    """把一个 PENDING 问题落成批注草稿（调用方负责 commit / 回滚）。

    三件事必须一起发生，否则会出现"问题已采纳但图上没有框"或反之：
    建 Annotation（AI 来源、PENDING_CONFIRM）→ 回填 issue.annotation_id → 物化计数 +1。
    """
    if issue.status != IssueStatus.PENDING:
        raise Conflict(
            "该问题已被处理，请刷新后重试", detail={"status": issue.status.value}
        )

    drawing = await db.get(Drawing, issue.drawing_id)
    if drawing is None or drawing.is_deleted:
        raise NotFound("图纸不存在")

    annotation = Annotation(
        project_id=issue.project_id,
        drawing_id=issue.drawing_id,
        drawing_version=drawing.version,
        issue_id=issue.id,
        source=AnnotationSource.AI,
        shape_type=ShapeType.RECT,
        location=issue.location,
        location_valid=issue.location_valid,
        title=issue.title[:60],
        content=issue.description,
        severity=issue.severity,
        discipline=issue.discipline,
        status=AnnotationStatus.PENDING_CONFIRM,
        creator_id=user.id,
    )
    db.add(annotation)
    await db.flush()

    issue.status = IssueStatus.ADOPTED
    issue.annotation_id = annotation.id

    # 计数走数据库端原子自增（D2 的教训：ORM 里 += 1 是读-改-写）
    await db.execute(
        update(Project)
        .where(Project.id == issue.project_id)
        .values(open_annotation_count=Project.open_annotation_count + 1)
    )
    return annotation.id


async def adopt_issue(db: AsyncSession, issue_id: uuid.UUID, user: User) -> IssueOut:
    """单条采纳：问题 → 批注草稿。"""
    issue = (
        await db.execute(select(ReviewIssue).where(ReviewIssue.id == issue_id))
    ).scalar_one_or_none()
    if issue is None:
        raise NotFound("问题不存在")
    await ensure_project_member(db, issue.project_id, user)

    await _adopt_one(db, issue, user)
    await db.commit()
    await db.refresh(issue)
    return to_issue_out(issue)


async def batch_adopt(
    db: AsyncSession, issue_ids: list[uuid.UUID], user: User
) -> tuple[int, list[uuid.UUID]]:
    """批量采纳：整批成功或整批回滚（prd-07 §7.5）。

    先全量校验（存在性 / 项目成员 / 状态都是 PENDING），再统一写；
    任何一条不满足就整批放弃，不留"采纳了一半"的中间态。
    """
    if not issue_ids:
        raise Conflict("没有选择任何问题")

    unique_ids = list(dict.fromkeys(issue_ids))
    issues = (
        await db.execute(select(ReviewIssue).where(ReviewIssue.id.in_(unique_ids)))
    ).scalars().all()
    if len(issues) != len(unique_ids):
        raise NotFound("部分问题不存在，请刷新后重试")

    for project_id in {issue.project_id for issue in issues}:
        await ensure_project_member(db, project_id, user)

    stale = [issue for issue in issues if issue.status != IssueStatus.PENDING]
    if stale:
        raise Conflict(
            "部分问题已被他人处理，请刷新后重试",
            detail={"stale_issue_ids": [str(issue.id) for issue in stale]},
        )

    created: list[uuid.UUID] = []
    for issue in sorted(issues, key=lambda item: (SEVERITY_ORDER, item.created_at)):
        created.append(await _adopt_one(db, issue, user))

    await db.commit()
    return len(created), created


async def get_report_draft(
    db: AsyncSession, task_id: uuid.UUID, user: User
) -> ReportDraftOut:
    task = (
        await db.execute(select(ReviewTask).where(ReviewTask.id == task_id))
    ).scalar_one_or_none()
    if task is None:
        raise NotFound("预审任务不存在")
    await ensure_project_member(db, task.project_id, user)

    draft = (
        await db.execute(
            select(ReportDraft)
            .where(ReportDraft.review_task_id == task.id)
            .order_by(ReportDraft.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if draft is None:
        raise NotFound("暂无报告初稿")

    return ReportDraftOut(
        id=draft.id,
        review_task_id=draft.review_task_id,
        drawing_id=draft.drawing_id,
        version=draft.version,
        status=draft.status,
        status_text=REPORT_STATUS_TEXT[draft.status],
        title=draft.title,
        summary=draft.summary,
        sections=draft.sections if isinstance(draft.sections, list) else [],
        risk_decisions=draft.risk_decisions if isinstance(draft.risk_decisions, list) else [],
        artifact_url=None,
        reject_reason=draft.reject_reason,
        confirmed_at=draft.confirmed_at,
        created_at=draft.created_at,
    )


async def issue_stream_token(
    db: AsyncSession, drawing_id: uuid.UUID, thread_id: str, user: User
) -> StreamTokenOut:
    drawing = await _get_drawing(db, drawing_id, user)
    task = (
        await db.execute(select(ReviewTask).where(ReviewTask.thread_id == thread_id))
    ).scalar_one_or_none()
    if task is None or task.drawing_id != drawing.id:
        raise NotFound("预审任务不属于该图纸")

    token, expires_at = sign_stream_token(user.id, thread_id)
    prefix = settings.API_V1_PREFIX
    return StreamTokenOut(
        token=token,
        expires_at=expires_at,
        stream_url=f"{prefix}/drawings/{drawing.id}/stream?thread_id={thread_id}&token={token}",
    )


async def stream_snapshot(thread_id: str) -> dict:
    """SSE 首帧用的轻量快照（不校验权限：调用方已校验过）。"""
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        task = (
            await db.execute(select(ReviewTask).where(ReviewTask.thread_id == thread_id))
        ).scalar_one_or_none()
        if task is None:
            return {"thread_id": thread_id, "run_status": "UNKNOWN"}
        return {
            "id": str(task.id),
            "thread_id": task.thread_id,
            "run_status": task.run_status.value
            if isinstance(task.run_status, RunStatus)
            else str(task.run_status),
            "stage": task.stage.value if isinstance(task.stage, ReviewStage) else task.stage,
            "stage_text": task.stage_text,
            "progress": task.progress,
            "issue_total": task.issue_total,
            "last_seq": task.last_seq,
            "waiting_interrupt_id": str(task.waiting_interrupt_id)
            if task.waiting_interrupt_id
            else None,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
