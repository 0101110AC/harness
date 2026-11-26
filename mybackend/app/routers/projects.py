"""项目管理路由：/api/v1/projects"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import DB, check_project_member, require
from app.models.enums import ProjectStatus
from app.models.project import ProjectMember
from app.models.user import User
from app.schemas.common import MessageOut, Page
from app.schemas.project import (
    MemberAddRequest,
    MemberUpdateRequest,
    ProjectCreateRequest,
    ProjectDetailOut,
    ProjectMemberOut,
    ProjectOut,
    ProjectUpdateRequest,
)
from app.services import project_service as svc

router = APIRouter(prefix="/projects", tags=["projects"])

MemberGate = Annotated[ProjectMember | None, Depends(check_project_member)]


@router.post(
    "",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建项目（自动成为负责人）",
)
async def create_project(
    body: ProjectCreateRequest,
    db: DB,
    user: Annotated[User, Depends(require("project:write"))],
) -> ProjectOut:
    return await svc.create_project(db, body, user)


@router.get("", response_model=Page[ProjectOut], summary="我的项目列表")
async def list_projects(
    db: DB,
    user: Annotated[User, Depends(require("project:read"))],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    status_: ProjectStatus | None = Query(default=None, alias="status"),
    sort: str = Query("recent", pattern="^(recent|name|created)$"),
    keyword: str | None = Query(default=None, max_length=64),
) -> Page[ProjectOut]:
    return await svc.list_projects(
        db, user, page=page, page_size=page_size, status=status_, sort=sort, keyword=keyword
    )


@router.get("/{project_id}", response_model=ProjectDetailOut, summary="项目详情（含成员）")
async def get_project(
    project_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("project:read"))],
    _member: MemberGate,
) -> ProjectDetailOut:
    await svc.touch_project(db, project_id, user.id)  # 记录"最近打开"，供 sort=recent 使用
    return await svc.get_project_detail(db, project_id, user)


@router.patch("/{project_id}", response_model=ProjectOut, summary="编辑项目（仅负责人）")
async def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdateRequest,
    db: DB,
    user: Annotated[User, Depends(require("project:write"))],
    _member: MemberGate,
) -> ProjectOut:
    return await svc.update_project(db, project_id, body, user)


@router.delete("/{project_id}", response_model=MessageOut, summary="删除项目（软删除，级联图纸）")
async def delete_project(
    project_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("project:write"))],
    _member: MemberGate,
) -> MessageOut:
    await svc.delete_project(db, project_id, user)
    return MessageOut(message="项目已删除")


# ------------------------------------------------------------------ 成员
@router.get(
    "/{project_id}/members",
    response_model=list[ProjectMemberOut],
    summary="成员列表（含未关闭批注数）",
)
async def list_members(
    project_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("project:read"))],
    _member: MemberGate,
) -> list[ProjectMemberOut]:
    return await svc.list_members(db, project_id, user)


@router.post(
    "/{project_id}/members",
    response_model=ProjectMemberOut,
    status_code=status.HTTP_201_CREATED,
    summary="添加成员",
)
async def add_member(
    project_id: uuid.UUID,
    body: MemberAddRequest,
    db: DB,
    user: Annotated[User, Depends(require("project:member:manage"))],
    _member: MemberGate,
) -> ProjectMemberOut:
    return await svc.add_member(db, project_id, body, user)


@router.patch(
    "/{project_id}/members/{user_id}",
    response_model=ProjectMemberOut,
    summary="调整成员项目角色",
)
async def update_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberUpdateRequest,
    db: DB,
    user: Annotated[User, Depends(require("project:member:manage"))],
    _member: MemberGate,
) -> ProjectMemberOut:
    return await svc.update_member_role(db, project_id, user_id, body.project_role, user)


@router.delete(
    "/{project_id}/members/{user_id}",
    response_model=dict,
    summary="移除成员（摘空其未关闭批注的责任人）",
)
async def remove_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("project:member:manage"))],
    _member: MemberGate,
) -> dict:
    return await svc.remove_member(db, project_id, user_id, user)
