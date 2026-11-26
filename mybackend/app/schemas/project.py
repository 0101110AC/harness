"""项目与成员模型（prd-07 §6.4）"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.models.enums import ProjectRole, ProjectStatus
from app.schemas.auth import UserBrief
from app.schemas.common import APIModel


class ProjectCreateRequest(APIModel):
    name: str = Field(min_length=2, max_length=128)
    code: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=2000)
    building_type: str | None = Field(default=None, max_length=32)
    design_stage: str | None = Field(default=None, max_length=32)


class ProjectUpdateRequest(APIModel):
    name: str | None = Field(default=None, min_length=2, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    building_type: str | None = Field(default=None, max_length=32)
    design_stage: str | None = Field(default=None, max_length=32)
    status: ProjectStatus | None = None


class ProjectMemberOut(APIModel):
    user: UserBrief
    project_role: ProjectRole
    project_role_text: str
    open_annotation_count: int = 0
    joined_at: datetime


class ProjectOut(APIModel):
    id: uuid.UUID
    name: str
    code: str | None
    description: str | None
    building_type: str | None
    design_stage: str | None
    status: ProjectStatus
    status_text: str
    owner: UserBrief
    drawing_count: int
    open_annotation_count: int
    my_project_role: ProjectRole | None = None
    discussion_count: int = 0  # 预留（V1.5 讨论区），D2 恒为 0
    created_at: datetime
    updated_at: datetime


class ProjectDetailOut(ProjectOut):
    members: list[ProjectMemberOut] = []


class MemberAddRequest(APIModel):
    user_id: uuid.UUID
    project_role: ProjectRole = ProjectRole.DESIGNER


class MemberUpdateRequest(APIModel):
    project_role: ProjectRole

    @field_validator("project_role")
    @classmethod
    def cannot_be_owner_via_patch(cls, v: ProjectRole) -> ProjectRole:
        # OWNER 只能通过"创建项目"或显式转移产生，PATCH 不允许直接提权
        if v is ProjectRole.OWNER:
            raise ValueError("不能通过此接口转让负责人")
        return v


PROJECT_ROLE_TEXT = {
    ProjectRole.OWNER: "负责人",
    ProjectRole.REVIEWER: "审图",
    ProjectRole.DESIGNER: "设计",
    ProjectRole.VIEWER: "只读",
}

PROJECT_STATUS_TEXT = {
    ProjectStatus.ACTIVE: "进行中",
    ProjectStatus.ARCHIVED: "已归档",
}
