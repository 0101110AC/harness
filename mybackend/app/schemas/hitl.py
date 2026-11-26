"""HITL（人工介入）请求/响应模型（prd-07 §6.11）。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.models.enums import (
    HitlResolution,
    InterruptStatus,
    InterruptType,
    ReportDecision,
    ReportStatus,
    RunStatus,
)
from app.schemas.auth import UserBrief
from app.schemas.common import APIModel
from app.schemas.drawing import Location


class RiskItemOut(APIModel):
    risk_id: str
    title: str
    level: Literal["HIGH", "CRITICAL"]
    metric_key: str | None = None
    detected_value: float | None = None
    threshold: float | None = None
    clause_ref: str | None = None
    why_suspicious: str | None = None
    drawing_id: uuid.UUID | None = None
    location: Location | None = None


class RiskItemDecision(APIModel):
    risk_id: str
    decision: HitlResolution
    remark: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def exempt_needs_remark(self) -> "RiskItemDecision":
        if self.decision is HitlResolution.MARK_EXEMPT and not (
            self.remark and len(self.remark.strip()) >= 5
        ):
            raise ValueError("标记为特殊豁免时必须填写依据（≥5 字）")
        return self


class HitlResumeRequest(APIModel):
    interrupt_id: uuid.UUID
    item_decisions: list[RiskItemDecision] = Field(min_length=1)
    remark: str | None = Field(default=None, max_length=1000)


class HitlResumeResponse(APIModel):
    interrupt_id: uuid.UUID
    status: InterruptStatus
    run_status: RunStatus
    resumed: bool


class ConfirmReportRequest(APIModel):
    interrupt_id: uuid.UUID
    decision: ReportDecision
    remark: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def reject_needs_remark(self) -> "ConfirmReportRequest":
        if self.decision is ReportDecision.REJECTED and not (
            self.remark and len(self.remark.strip()) >= 10
        ):
            raise ValueError("退回修改必须填写意见（≥10 字）")
        return self


class ConfirmReportResponse(APIModel):
    report_id: uuid.UUID | None
    status: ReportStatus
    run_status: RunStatus
    artifact_url: str | None = None


class InterruptTaskOut(APIModel):
    id: uuid.UUID
    thread_id: str
    review_task_id: uuid.UUID
    drawing_id: uuid.UUID
    type: InterruptType
    type_text: str
    status: InterruptStatus
    title: str
    items: list[RiskItemOut] = []
    report_id: uuid.UUID | None = None
    assignee: UserBrief | None = None
    resolution: str | None = None
    item_decisions: list[dict] = []
    remark: str | None = None
    expires_at: datetime
    remaining_seconds: int
    created_at: datetime
