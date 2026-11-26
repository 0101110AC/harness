"""项目与成员的业务逻辑。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.deps import ensure_project_member, is_superuser
from app.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.models.annotation import Annotation
from app.models.drawing import Drawing
from app.models.enums import AnnotationStatus, AuditAction, ProjectRole, ProjectStatus
from app.models.project import Project, ProjectMember
from app.models.user import User
from app.schemas.auth import UserBrief
from app.schemas.common import Page
from app.schemas.project import (
    PROJECT_ROLE_TEXT,
    PROJECT_STATUS_TEXT,
    MemberAddRequest,
    ProjectCreateRequest,
    ProjectDetailOut,
    ProjectMemberOut,
    ProjectOut,
    ProjectUpdateRequest,
)
from app.services import audit_service

OPEN_STATUSES = (
    AnnotationStatus.PENDING_CONFIRM,
    AnnotationStatus.ISSUED,
    AnnotationStatus.MODIFIED,
)


# ---------------------------------------------------------------- helpers
def _to_out(project: Project, my_role: ProjectRole | None) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        code=project.code,
        description=project.description,
        building_type=project.building_type,
        design_stage=project.design_stage,
        status=project.status,
        status_text=PROJECT_STATUS_TEXT[project.status],
        owner=UserBrief.model_validate(project.owner),
        drawing_count=project.drawing_count,
        open_annotation_count=project.open_annotation_count,
        my_project_role=my_role,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


async def _my_role(db: AsyncSession, project_id: uuid.UUID, user: User) -> ProjectRole | None:
    if is_superuser(user):
        return ProjectRole.OWNER  # ADMIN 视角视为负责人，仅用于展示
    return (
        await db.execute(
            select(ProjectMember.project_role).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == user.id
            )
        )
    ).scalar_one_or_none()


async def get_project_or_404(db: AsyncSession, project_id: uuid.UUID) -> Project:
    project = (
        await db.execute(
            select(Project)
            .options(joinedload(Project.owner))
            .where(Project.id == project_id, Project.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if project is None:
        raise NotFound("项目不存在或已删除")
    return project


# ---------------------------------------------------------------- 项目 CRUD
async def create_project(db: AsyncSession, payload: ProjectCreateRequest, user: User) -> ProjectOut:
    if payload.code:
        exists = (
            await db.execute(select(Project.id).where(Project.code == payload.code))
        ).scalar_one_or_none()
        if exists:
            raise Conflict("项目编号已存在", code="DUPLICATE_RESOURCE", detail={"field": "code"})

    project = Project(
        name=payload.name,
        code=payload.code,
        description=payload.description,
        building_type=payload.building_type,
        design_stage=payload.design_stage,
        status=ProjectStatus.ACTIVE,
        owner_id=user.id,
    )
    db.add(project)
    await db.flush()

    # 创建者自动成为 OWNER（prd-05 §6.3）
    db.add(ProjectMember(project_id=project.id, user_id=user.id, project_role=ProjectRole.OWNER))
    await db.commit()

    project = await get_project_or_404(db, project.id)
    return _to_out(project, ProjectRole.OWNER)


async def list_projects(
    db: AsyncSession,
    user: User,
    *,
    page: int,
    page_size: int,
    status: ProjectStatus | None,
    sort: str,
    keyword: str | None,
) -> Page[ProjectOut]:
    base = (
        select(Project, ProjectMember.project_role)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(Project.is_deleted.is_(False), ProjectMember.user_id == user.id)
        .options(joinedload(Project.owner))
    )
    if status is not None:
        base = base.where(Project.status == status)
    if keyword:
        base = base.where(Project.name.ilike(f"%{keyword}%"))

    # sort=recent：按"我在这个项目的最近打开时间"倒序，未打开过回落创建时间
    # coalesce 保证 SQLite / PostgreSQL 排序语义一致（PG DESC 默认 NULLS FIRST）
    if sort == "recent":
        order = func.coalesce(ProjectMember.last_opened_at, Project.created_at).desc()
    elif sort == "name":
        order = Project.name.asc()
    else:
        order = Project.created_at.desc()

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (await db.execute(base.order_by(order).offset((page - 1) * page_size).limit(page_size))).all()

    items = [_to_out(p, role) for p, role in rows]
    return Page[ProjectOut](
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )


async def get_project_detail(db: AsyncSession, project_id: uuid.UUID, user: User) -> ProjectDetailOut:
    project = await get_project_or_404(db, project_id)
    await ensure_project_member(db, project_id, user)

    members = (
        await db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .options(joinedload(ProjectMember.user))
        )
    ).scalars().all()

    # 成员维度"未关闭批注数"一次聚合，避免按成员逐条 count（N+1）
    counts = dict(
        (
            await db.execute(
                select(Annotation.assignee_id, func.count())
                .where(
                    Annotation.project_id == project_id,
                    Annotation.assignee_id.is_not(None),
                    Annotation.status.in_(OPEN_STATUSES),
                    Annotation.is_deleted.is_(False),
                )
                .group_by(Annotation.assignee_id)
            )
        ).all()
    )

    base = _to_out(project, await _my_role(db, project_id, user))
    return ProjectDetailOut(
        **base.model_dump(),
        members=[
            ProjectMemberOut(
                user=UserBrief.model_validate(m.user),
                project_role=m.project_role,
                project_role_text=PROJECT_ROLE_TEXT[m.project_role],
                open_annotation_count=counts.get(m.user_id, 0),
                joined_at=m.created_at,
            )
            for m in members
        ],
    )


async def update_project(
    db: AsyncSession, project_id: uuid.UUID, payload: ProjectUpdateRequest, user: User
) -> ProjectOut:
    project = await get_project_or_404(db, project_id)
    member = await ensure_project_member(db, project_id, user)
    if not is_superuser(user) and (member is None or member.project_role is not ProjectRole.OWNER):
        raise Forbidden("仅项目负责人可编辑项目", detail={"required_project_role": "OWNER"})

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(project, field, value)

    await db.commit()
    # commit 后对象属性可能过期，重新带 owner 查一次，避免异步上下文里的懒加载
    project = await get_project_or_404(db, project_id)
    return _to_out(project, await _my_role(db, project_id, user))


async def delete_project(db: AsyncSession, project_id: uuid.UUID, user: User) -> None:
    project = await get_project_or_404(db, project_id)
    member = await ensure_project_member(db, project_id, user)
    if not is_superuser(user) and (member is None or member.project_role is not ProjectRole.OWNER):
        raise Forbidden("仅项目负责人可删除项目", detail={"required_project_role": "OWNER"})

    now = datetime.now(timezone.utc)
    project.is_deleted = True
    project.updated_at = now
    # 级联软删除图纸（批注随图纸一并视为不可见，不物理删除以保留审计）
    await db.execute(
        update(Drawing)
        .where(Drawing.project_id == project_id, Drawing.is_deleted.is_(False))
        .values(is_deleted=True, updated_at=now)
    )
    audit_service.record(
        db,
        action=AuditAction.PROJECT_DELETE,
        target_type="PROJECT",
        user=user,
        target_id=project.id,
        target_name=project.name,
        detail={"code": project.code, "cascade": "drawings_soft_deleted"},
    )
    await db.commit()


# ---------------------------------------------------------------- 成员管理
async def list_members(db: AsyncSession, project_id: uuid.UUID, user: User) -> list[ProjectMemberOut]:
    await get_project_or_404(db, project_id)
    await ensure_project_member(db, project_id, user)

    members = (
        await db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .options(joinedload(ProjectMember.user))
            .order_by(ProjectMember.created_at.asc())
        )
    ).scalars().all()

    counts = dict(
        (
            await db.execute(
                select(Annotation.assignee_id, func.count())
                .where(
                    Annotation.project_id == project_id,
                    Annotation.assignee_id.is_not(None),
                    Annotation.status.in_(OPEN_STATUSES),
                    Annotation.is_deleted.is_(False),
                )
                .group_by(Annotation.assignee_id)
            )
        ).all()
    )
    return [
        ProjectMemberOut(
            user=UserBrief.model_validate(m.user),
            project_role=m.project_role,
            project_role_text=PROJECT_ROLE_TEXT[m.project_role],
            open_annotation_count=counts.get(m.user_id, 0),
            joined_at=m.created_at,
        )
        for m in members
    ]


async def add_member(
    db: AsyncSession, project_id: uuid.UUID, payload: MemberAddRequest, operator: User
) -> ProjectMemberOut:
    project = await get_project_or_404(db, project_id)
    await _assert_can_manage_members(db, project_id, operator)

    target = (await db.execute(select(User).where(User.id == payload.user_id))).scalar_one_or_none()
    if target is None:
        raise ValidationFailed("用户不存在", detail={"field": "user_id"})

    exists = (
        await db.execute(
            select(ProjectMember.id).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == payload.user_id
            )
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise Conflict("该用户已在项目中", code="DUPLICATE_RESOURCE", detail={"field": "user_id"})

    member = ProjectMember(
        project_id=project_id, user_id=payload.user_id, project_role=payload.project_role
    )
    db.add(member)
    audit_service.record(
        db,
        action=AuditAction.MEMBER_ADD,
        target_type="PROJECT",
        user=operator,
        target_id=project.id,
        target_name=project.name,
        detail={
            "member_id": str(target.id),
            "member_name": target.name,
            "project_role": payload.project_role.value,
        },
    )
    await db.commit()
    await db.refresh(member)

    return ProjectMemberOut(
        user=UserBrief.model_validate(target),
        project_role=member.project_role,
        project_role_text=PROJECT_ROLE_TEXT[member.project_role],
        open_annotation_count=0,
        joined_at=member.created_at,
    )


async def remove_member(
    db: AsyncSession, project_id: uuid.UUID, target_user_id: uuid.UUID, operator: User
) -> dict:
    """移除成员 + 处理其名下未关闭批注（D2 任务 2 重点）。"""
    project = await get_project_or_404(db, project_id)
    await _assert_can_manage_members(db, project_id, operator)

    member = (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == target_user_id
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise NotFound("该用户不是项目成员")

    if member.project_role is ProjectRole.OWNER:
        owners = (
            await db.execute(
                select(func.count()).select_from(ProjectMember).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.project_role == ProjectRole.OWNER,
                )
            )
        ).scalar_one()
        if owners <= 1:
            raise ValidationFailed("项目至少需要一名负责人", detail={"field": "user_id"})

    # 先把名下未关闭批注的责任人摘空，并打标记，再看板/列表能提示"责任人已离项"
    orphaned = (
        await db.execute(
            update(Annotation)
            .where(
                Annotation.project_id == project_id,
                Annotation.assignee_id == target_user_id,
                Annotation.status.in_(OPEN_STATUSES),
                Annotation.is_deleted.is_(False),
            )
            .values(assignee_id=None, assignee_missing=True)
        )
    ).rowcount or 0

    await db.delete(member)
    audit_service.record(
        db,
        action=AuditAction.MEMBER_REMOVE,
        target_type="PROJECT",
        user=operator,
        target_id=project.id,
        target_name=project.name,
        detail={
            "member_id": str(target_user_id),
            "project_role": member.project_role.value,
            "orphaned_annotations": orphaned,
        },
    )
    await db.commit()
    return {"removed": True, "orphaned_annotations": orphaned}


async def update_member_role(
    db: AsyncSession, project_id: uuid.UUID, target_user_id: uuid.UUID,
    role: ProjectRole, operator: User,
) -> ProjectMemberOut:
    await get_project_or_404(db, project_id)
    await _assert_can_manage_members(db, project_id, operator)

    member = (
        await db.execute(
            select(ProjectMember)
            .where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == target_user_id
            )
            .options(joinedload(ProjectMember.user))
        )
    ).scalar_one_or_none()
    if member is None:
        raise NotFound("该用户不是项目成员")

    member.project_role = role
    await db.commit()
    return ProjectMemberOut(
        user=UserBrief.model_validate(member.user),
        project_role=member.project_role,
        project_role_text=PROJECT_ROLE_TEXT[member.project_role],
        joined_at=member.created_at,
    )


async def _assert_can_manage_members(
    db: AsyncSession, project_id: uuid.UUID, operator: User
) -> None:
    """功能权限由 require() 保证；这里做数据权限：必须是负责人。"""
    if is_superuser(operator):
        return
    role = (
        await db.execute(
            select(ProjectMember.project_role).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == operator.id
            )
        )
    ).scalar_one_or_none()
    if role is not ProjectRole.OWNER:
        raise Forbidden("仅项目负责人可管理成员", detail={"required_project_role": "OWNER"})


async def touch_project(db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """记录"最近打开"，供 sort=recent 使用。"""
    await db.execute(
        update(ProjectMember)
        .where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
        .values(last_opened_at=datetime.now(timezone.utc))
    )
    await db.commit()
