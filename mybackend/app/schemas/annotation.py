"""批注模型（prd-07 §6.7）"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum

from pydantic import Field, model_validator

from app.models.enums import (
    AnnotationSource,
    AnnotationStatus,
    Discipline,
    LogType,
    Severity,
    ShapeType,
)
from app.schemas.auth import UserBrief
from app.schemas.common import APIModel
from app.schemas.drawing import Location


class AnnotationCreateRequest(APIModel):
    project_id: uuid.UUID
    drawing_id: uuid.UUID
    shape_type: ShapeType = ShapeType.RECT
    location: Location
    title: str = Field(min_length=2, max_length=60)
    content: str | None = Field(default=None, max_length=2000)
    severity: Severity = Severity.MINOR
    discipline: Discipline = Discipline.GENERAL
    assignee_id: uuid.UUID | None = None
    due_date: date | None = None


class AnnotationUpdateRequest(APIModel):
    row_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=2, max_length=60)
    content: str | None = Field(default=None, max_length=2000)
    severity: Severity | None = None
    discipline: Discipline | None = None
    assignee_id: uuid.UUID | None = None
    due_date: date | None = None
    location: Location | None = None


class AnnotationAction(str, Enum):
    """allowed_actions 取值（prd-07 §6.7）"""

    ISSUE = "ISSUE"
    MARK_MODIFIED = "MARK_MODIFIED"
    CLOSE = "CLOSE"
    REJECT_AND_REISSUE = "REJECT_AND_REISSUE"
    DELETE = "DELETE"
    EDIT = "EDIT"
    COMMENT = "COMMENT"


class TransitionAction(str, Enum):
    """可提交给 /transition 的动作（会改变状态；AnnotationAction 的子集）"""

    ISSUE = "ISSUE"
    MARK_MODIFIED = "MARK_MODIFIED"
    CLOSE = "CLOSE"
    REJECT_AND_REISSUE = "REJECT_AND_REISSUE"
    DELETE = "DELETE"


class AnnotationTransitionRequest(APIModel):
    action: TransitionAction
    row_version: int = Field(ge=1)
    assignee_id: uuid.UUID | None = Field(default=None, description="ISSUE 必填")
    version_drawing_id: uuid.UUID | None = Field(default=None, description="MARK_MODIFIED 可选")
    close_remark: str | None = Field(default=None, max_length=500)
    reject_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check_required(self) -> "AnnotationTransitionRequest":
        # 必填项在这里拦一次（错误体统一走 VALIDATION_ERROR），
        # Service 层再拦一次：批量流转不走这个模型，且防御直接调用 Service 的场景。
        if self.action is TransitionAction.ISSUE and self.assignee_id is None:
            raise ValueError("下发批注必须指定责任人")
        if self.action is TransitionAction.REJECT_AND_REISSUE:
            reason = (self.reject_reason or "").strip()
            if len(reason) < 10:
                raise ValueError("复核不通过必须填写原因（不少于 10 字）")
        return self


class BatchTransitionRequest(APIModel):
    annotation_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    action: TransitionAction
    skip_invalid: bool = False
    assignee_id: uuid.UUID | None = None
    close_remark: str | None = Field(default=None, max_length=500)
    reject_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check_required(self) -> "BatchTransitionRequest":
        if self.action is TransitionAction.ISSUE and self.assignee_id is None:
            raise ValueError("批量下发必须指定责任人")
        if self.action is TransitionAction.REJECT_AND_REISSUE and len(
            (self.reject_reason or "").strip()
        ) < 10:
            raise ValueError("复核不通过必须填写原因（不少于 10 字）")
        return self


class InvalidItem(APIModel):
    annotation_id: uuid.UUID
    reason: str
    code: str | None = None


class BatchTransitionResponse(APIModel):
    succeeded: int
    failed: list[InvalidItem] = []


class CommentCreateRequest(APIModel):
    content: str = Field(min_length=1, max_length=1000)


class AnnotationOut(APIModel):
    id: uuid.UUID
    project_id: uuid.UUID
    drawing_id: uuid.UUID
    # 名称快照：全局批注页要在一行里显示"属于哪个项目 / 哪张图纸"，
    # 不让前端按 id 再发 N 个请求去凑
    project_name: str | None = None
    drawing_name: str | None = None
    drawing_version: int
    issue_id: uuid.UUID | None = None
    source: AnnotationSource
    shape_type: ShapeType
    location: Location
    location_valid: bool
    title: str
    content: str | None = None
    severity: Severity
    severity_text: str
    discipline: Discipline
    discipline_text: str
    status: AnnotationStatus
    status_text: str
    assignee: UserBrief | None = None
    assignee_missing: bool = False
    creator: UserBrief
    due_date: date | None = None
    version_drawing_id: uuid.UUID | None = None
    close_remark: str | None = None
    closed_at: datetime | None = None
    comment_count: int = 0
    is_stale_version: bool = False
    allowed_actions: list[AnnotationAction] = []
    row_version: int
    created_at: datetime
    updated_at: datetime


class TimelineItemOut(APIModel):
    id: uuid.UUID
    log_type: LogType
    log_type_text: str
    operator: UserBrief
    from_status: AnnotationStatus | None = None
    to_status: AnnotationStatus | None = None
    remark: str | None = None
    content: str | None = None
    can_delete: bool = False
    created_at: datetime
