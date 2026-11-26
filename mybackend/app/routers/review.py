"""预审路由：任务创建 / 快照 / 问题 / 报告初稿 / SSE 票据与流（prd-07 §5.5、§5.5.1）。

**进度获取唯一通道是 SSE**：`GET /review-tasks/{id}` 只给首屏与断线恢复，
前端不得拿它轮询（prd-02 §8 硬性要求）。
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.core.deps import DB, require
from app.core.errors import Unauthorized
from app.core.stream_token import consume_stream_token
from app.models.drawing import Drawing  # noqa: F401  (OpenAPI 类型解析需要)
from app.models.enums import IssueStatus, Severity, UserStatus
from app.models.review import ReviewTask
from app.models.user import User
from app.schemas.common import Page
from app.schemas.review import (
    BatchAdoptOut,
    BatchAdoptRequest,
    IssueOut,
    ReportDraftOut,
    ReviewTaskBrief,
    ReviewTaskCreateRequest,
    ReviewTaskSnapshot,
    StreamTokenOut,
    StreamTokenRequest,
)
from app.services import review_service as svc
from app.sse.hub import hub

router = APIRouter(tags=["review"])


@router.post(
    "/drawings/{drawing_id}/review-tasks",
    response_model=ReviewTaskSnapshot,
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建预审任务（异步，返回 thread_id 用于订阅 SSE）",
)
async def create_review_task(
    drawing_id: uuid.UUID,
    body: ReviewTaskCreateRequest,
    db: DB,
    user: Annotated[User, Depends(require("review:execute"))],
) -> ReviewTaskSnapshot:
    return await svc.create_review_task(db, drawing_id, body, user)


@router.get(
    "/drawings/{drawing_id}/review-tasks",
    response_model=Page[ReviewTaskBrief],
    summary="图纸下的预审任务（最新在前）",
)
async def list_review_tasks(
    drawing_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("review:read"))],
    page: int = Query(1, ge=1),
    page_size: int = Query(5, ge=1, le=50),
) -> Page[ReviewTaskBrief]:
    return await svc.list_tasks(db, drawing_id, user, page=page, page_size=page_size)


@router.get(
    "/review-tasks/{task_id}",
    response_model=ReviewTaskSnapshot,
    summary="任务终态快照（仅首屏/断线恢复，禁止轮询）",
)
async def get_review_task(
    task_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("review:read"))],
) -> ReviewTaskSnapshot:
    return await svc.get_task(db, task_id, user)


@router.get(
    "/review-tasks/{task_id}/issues",
    response_model=Page[IssueOut],
    summary="问题列表（多值筛选）",
)
async def list_issues(
    task_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("review:read"))],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    severity: list[Severity] | None = Query(default=None),
    discipline: list[str] | None = Query(default=None),
    status_: list[IssueStatus] | None = Query(default=None, alias="status"),
    keyword: str | None = Query(default=None, max_length=60),
) -> Page[IssueOut]:
    return await svc.list_issues(
        db,
        task_id,
        user,
        page=page,
        page_size=page_size,
        severity=severity,
        discipline=discipline,
        status=status_,
        keyword=keyword,
    )


@router.post(
    "/issues/{issue_id}/adopt",
    response_model=IssueOut,
    summary="采纳问题为批注草稿（AI 问题 → 批注）",
)
async def adopt_issue(
    issue_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("annotation:write"))],
) -> IssueOut:
    return await svc.adopt_issue(db, issue_id, user)


@router.post(
    "/issues/batch-adopt",
    response_model=BatchAdoptOut,
    summary="批量采纳（整批成功或整批回滚）",
)
async def batch_adopt(
    body: BatchAdoptRequest,
    db: DB,
    user: Annotated[User, Depends(require("annotation:write"))],
) -> BatchAdoptOut:
    created, annotations = await svc.batch_adopt(db, body.issue_ids, user)
    return BatchAdoptOut(created=created, annotations=annotations)


@router.get(
    "/review-tasks/{task_id}/report-draft",
    response_model=ReportDraftOut,
    summary="报告初稿（卡点二读到的内容）",
)
async def get_report_draft(
    task_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("review:read"))],
) -> ReportDraftOut:
    return await svc.get_report_draft(db, task_id, user)


# ---------------------------------------------------------------------------
# SSE：先用 Bearer 换一次性票据，再让 EventSource 带 ?token= 建流
# ---------------------------------------------------------------------------
@router.post(
    "/drawings/{drawing_id}/stream-token",
    response_model=StreamTokenOut,
    summary="换取一次性 SSE 票据（TTL 60s，绑定 user + thread_id）",
)
async def issue_stream_token(
    drawing_id: uuid.UUID,
    body: StreamTokenRequest,
    db: DB,
    user: Annotated[User, Depends(require("review:read"))],
) -> StreamTokenOut:
    return await svc.issue_stream_token(db, drawing_id, body.thread_id, user)


@router.get(
    "/drawings/{drawing_id}/stream",
    summary="订阅预审事件流（text/event-stream，支持 Last-Event-ID 重放）",
    tags=["stream"],
    response_class=StreamingResponse,
)
async def stream_events(
    drawing_id: uuid.UUID,
    db: DB,
    thread_id: str = Query(..., description="review_task.id"),
    token: str = Query(..., description="stream-token 换来的票据"),
    from_seq: int = Query(0, ge=0, description="等价 Last-Event-ID"),
    last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    # ① 票据校验（一次性 + 绑定 user/thread）+ 取回用户：EventSource 不能带 Bearer
    payload = consume_stream_token(token, thread_id=thread_id)
    user = await db.get(User, uuid.UUID(str(payload["u"])))
    if user is None or user.status is not UserStatus.ACTIVE:
        raise Unauthorized("实时通道票据无效", code="STREAM_TOKEN_INVALID")

    # ② 数据权限：thread 必须属于该图纸，且当前用户是项目成员
    task = (
        await db.execute(
            select(ReviewTask).where(
                ReviewTask.thread_id == thread_id, ReviewTask.drawing_id == drawing_id
            )
        )
    ).scalar_one_or_none()
    if task is None:
        from app.core.errors import NotFound

        raise NotFound("预审任务不属于该图纸")
    from app.core.deps import ensure_project_member

    await ensure_project_member(db, task.project_id, user)

    # ③ 订阅（超过 3 条/thread 抛 429 STREAM_LIMIT_EXCEEDED）
    sub = await hub.subscribe(thread_id, str(user.id))
    snapshot = await svc.stream_snapshot(thread_id)

    return StreamingResponse(
        hub.stream(
            sub,
            from_seq=max(from_seq, last_event_id or 0),
            snapshot=snapshot,
        ),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx 不缓冲，否则事件会被憋在代理里
        },
    )
