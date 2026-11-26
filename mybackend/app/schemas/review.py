"""预审任务 / 问题 / 报告初稿 / 实时流 token（prd-07 §6.6、§6.11）。"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.models.enums import (
    Discipline,
    IssueCategory,
    IssueStatus,
    ReportStatus,
    ReviewMode,
    ReviewStage,
    ReviewTaskStatus,
    RunStatus,
    Severity,
)
from app.schemas.common import APIModel
from app.schemas.drawing import Location


class ReviewTaskCreateRequest(APIModel):
    disciplines: list[Discipline] = [Discipline.ARCH, Discipline.STRUCT, Discipline.MEP]
    rule_sets: list[str] = ["GB50016", "GB50352"]
    review_mode: ReviewMode = ReviewMode.PROFESSIONAL


class ReviewTaskOut(APIModel):
    id: uuid.UUID
    drawing_id: uuid.UUID
    status: ReviewTaskStatus
    status_text: str
    stage: ReviewStage | None
    stage_text: str | None
    progress: int = Field(ge=0, le=100)
    engine: str
    issue_total: int
    issue_blocker: int
    summary: dict | None
    error_reason: str | None
    cost_ms: int | None
    created_at: datetime
    finished_at: datetime | None


class ReviewTaskSnapshot(ReviewTaskOut):
    """任务终态快照：只给首屏/断线重连用，禁止轮询（prd-02 §8）。"""

    thread_id: str
    run_status: RunStatus
    run_status_text: str
    current_node: str | None = None
    review_mode: ReviewMode = ReviewMode.PROFESSIONAL
    waiting_interrupt_id: uuid.UUID | None = None
    report_id: uuid.UUID | None = None
    last_seq: int = 0


class ReviewTaskBrief(APIModel):
    """图纸下"最近一次任务"的轻量视图。

    用途只有一个：图纸列表/预审中心的「查看预审」要拿到 taskId。
    PRD 的列表接口清单没有单列它，但前端 `listTasks()` 依赖它——
    缺了这条 GET，刷新或重进预审中心就会退化成"这张图纸还没有预审任务"。
    """

    id: uuid.UUID
    thread_id: str
    drawing_id: uuid.UUID
    status: ReviewTaskStatus
    status_text: str
    run_status: RunStatus
    progress: int = Field(ge=0, le=100)
    issue_total: int
    created_at: datetime


class IssueOut(APIModel):
    id: uuid.UUID
    review_task_id: uuid.UUID
    drawing_id: uuid.UUID
    category: IssueCategory
    category_text: str
    severity: Severity
    severity_text: str
    discipline: Discipline
    discipline_text: str
    title: str
    description: str | None
    suggestion: str | None
    rule_code: str | None
    confidence: int
    location: Location
    location_valid: bool
    status: IssueStatus
    status_text: str
    annotation_id: uuid.UUID | None = None
    created_at: datetime


class ReportDraftOut(APIModel):
    id: uuid.UUID
    review_task_id: uuid.UUID
    drawing_id: uuid.UUID
    version: int
    status: ReportStatus
    status_text: str
    title: str
    summary: dict | None
    sections: list[dict]
    risk_decisions: list[dict] = []
    artifact_url: str | None = None
    reject_reason: str | None = None
    confirmed_at: datetime | None = None
    created_at: datetime


class StreamTokenRequest(APIModel):
    thread_id: str = Field(min_length=1, max_length=64)


class StreamTokenOut(APIModel):
    token: str
    expires_at: datetime
    stream_url: str


class BatchAdoptRequest(APIModel):
    issue_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class BatchAdoptOut(APIModel):
    created: int
    annotations: list[uuid.UUID]


__all__ = [
    "BatchAdoptOut",
    "BatchAdoptRequest",
    "IssueOut",
    "ReportDraftOut",
    "ReviewTaskBrief",
    "ReviewTaskCreateRequest",
    "ReviewTaskOut",
    "ReviewTaskSnapshot",
    "StreamTokenOut",
    "StreamTokenRequest",
]
