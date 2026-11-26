"""批注 CRUD + 数据权限 + 状态流转（prd-03 §5 的落地实现）。"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import ensure_project_member, is_superuser
from app.core.errors import (
    Conflict,
    Forbidden,
    InvalidStateTransition,
    NotFound,
    ValidationFailed,
)
from app.models.annotation import Annotation, AnnotationStatusLog
from app.models.drawing import Drawing
from app.models.enums import (
    ANNOTATION_STATUS_TEXT,
    DISCIPLINE_TEXT,
    SEVERITY_TEXT,
    AnnotationSource,
    AnnotationStatus,
    AuditAction,
    Discipline,
    LogType,
    Severity,
)
from app.models.project import Project, ProjectMember
from app.models.user import User
from app.notify.outbox import enqueue
from app.services import audit_service
from app.schemas.annotation import (
    AnnotationAction,
    AnnotationCreateRequest,
    AnnotationOut,
    AnnotationTransitionRequest,
    AnnotationUpdateRequest,
    BatchTransitionRequest,
    BatchTransitionResponse,
    InvalidItem,
    TransitionAction,
)
from app.schemas.auth import UserBrief
from app.schemas.common import Page
from app.schemas.drawing import clip_location
from app.services import annotation_state as sm

log = logging.getLogger(__name__)

OPEN_STATUSES = (
    AnnotationStatus.PENDING_CONFIRM,
    AnnotationStatus.ISSUED,
    AnnotationStatus.MODIFIED,
)

SEVERITY_RANK = case(
    (Annotation.severity == Severity.BLOCKER, 0),
    (Annotation.severity == Severity.MAJOR, 1),
    (Annotation.severity == Severity.MINOR, 2),
    else_=3,
)


# ------------------------------------------------------------------ 数据权限
def allowed_actions(ann: Annotation, user: User, admin: bool) -> list[AnnotationAction]:
    """按钮白名单由后端算（prd-03 §5.3/§5.5），前端只渲染不判断。"""
    is_creator = ann.creator_id == user.id
    is_assignee = ann.assignee_id == user.id
    acts: list[AnnotationAction] = []

    if ann.status is AnnotationStatus.PENDING_CONFIRM and (is_creator or admin):
        acts.append(AnnotationAction.ISSUE)
    if ann.status is AnnotationStatus.ISSUED and (is_assignee or admin):
        acts.append(AnnotationAction.MARK_MODIFIED)
    if ann.status is AnnotationStatus.MODIFIED and (is_creator or admin):
        acts.extend([AnnotationAction.CLOSE, AnnotationAction.REJECT_AND_REISSUE])
    if ann.status is not AnnotationStatus.CLOSED and (is_creator or admin):
        acts.extend([AnnotationAction.EDIT, AnnotationAction.DELETE])
    if ann.status is AnnotationStatus.CLOSED and admin:
        acts.append(AnnotationAction.DELETE)
    if ann.status is not AnnotationStatus.CLOSED:
        acts.append(AnnotationAction.COMMENT)
    return acts


def _ensure_can_edit(ann: Annotation, user: User, admin: bool) -> None:
    """编辑/作废：仅创建者或 ADMIN（横向越权防线，不能只靠 require()）。"""
    if admin or ann.creator_id == user.id:
        return
    raise Forbidden(
        "仅创建者可编辑该批注",
        detail={"action": "EDIT", "creator_id": str(ann.creator_id)},
    )


# ------------------------------------------------------------------ 装配
def _to_out(
    ann: Annotation,
    user: User,
    admin: bool,
    current_version: int,
    *,
    project_name: str | None = None,
    drawing_name: str | None = None,
) -> AnnotationOut:
    return AnnotationOut(
        id=ann.id,
        project_id=ann.project_id,
        drawing_id=ann.drawing_id,
        project_name=project_name,
        drawing_name=drawing_name,
        drawing_version=ann.drawing_version,
        issue_id=ann.issue_id,
        source=ann.source,
        shape_type=ann.shape_type,
        location=ann.location,
        location_valid=ann.location_valid,
        title=ann.title,
        content=ann.content,
        severity=ann.severity,
        severity_text=SEVERITY_TEXT[ann.severity],
        discipline=ann.discipline,
        discipline_text=DISCIPLINE_TEXT[ann.discipline],
        status=ann.status,
        status_text=ANNOTATION_STATUS_TEXT[ann.status],
        assignee=UserBrief.model_validate(ann.assignee) if ann.assignee else None,
        assignee_missing=ann.assignee_missing,
        creator=UserBrief.model_validate(ann.creator),
        due_date=ann.due_date,
        version_drawing_id=ann.version_drawing_id,
        close_remark=ann.close_remark,
        closed_at=ann.closed_at,
        comment_count=ann.comment_count,
        is_stale_version=ann.drawing_version < current_version,
        allowed_actions=allowed_actions(ann, user, admin),
        row_version=ann.row_version,
        created_at=ann.created_at,
        updated_at=ann.updated_at,
    )


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


async def _drawing_in_project(
    db: AsyncSession, drawing_id: uuid.UUID, project_id: uuid.UUID
) -> Drawing:
    drawing = (
        await db.execute(
            select(Drawing).where(
                Drawing.id == drawing_id,
                Drawing.project_id == project_id,
                Drawing.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if drawing is None:
        # 不区分"图纸不存在"与"图纸属于别的项目"，避免探测他人项目
        raise ValidationFailed("图纸不属于该项目或已删除", detail={"field": "drawing_id"})
    return drawing


async def _assert_assignee_is_member(
    db: AsyncSession, project_id: uuid.UUID, assignee_id: uuid.UUID | None
) -> None:
    if assignee_id is None:
        return
    exists = (
        await db.execute(
            select(ProjectMember.id).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == assignee_id
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise ValidationFailed("被指派人不是项目成员", detail={"field": "assignee_id"})


# ------------------------------------------------------------------ CRUD
async def create_annotation(
    db: AsyncSession, payload: AnnotationCreateRequest, user: User
) -> AnnotationOut:
    await ensure_project_member(db, payload.project_id, user)

    project = (
        await db.execute(
            select(Project).where(
                Project.id == payload.project_id, Project.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    if project is None:
        raise NotFound("项目不存在或已删除")

    drawing = await _drawing_in_project(db, payload.drawing_id, payload.project_id)
    await _assert_assignee_is_member(db, payload.project_id, payload.assignee_id)

    loc, valid = clip_location(payload.location)

    ann = Annotation(
        project_id=payload.project_id,
        drawing_id=payload.drawing_id,
        drawing_version=drawing.version,
        source=AnnotationSource.HUMAN,
        shape_type=payload.shape_type,
        location=loc.model_dump(mode="json"),
        location_valid=valid,
        title=payload.title,
        content=payload.content,
        severity=payload.severity,
        discipline=payload.discipline,
        status=AnnotationStatus.PENDING_CONFIRM,
        assignee_id=payload.assignee_id,
        creator_id=user.id,
        due_date=payload.due_date,
        row_version=1,
        comment_count=0,
    )
    db.add(ann)
    await db.flush()

    # 同一事务内：状态日志 + 物化计数（避免"批注建了但计数没加"）
    db.add(
        AnnotationStatusLog(
            annotation_id=ann.id,
            log_type=LogType.CREATED,
            to_status=AnnotationStatus.PENDING_CONFIRM,
            operator_id=user.id,
        )
    )
    await db.execute(
        update(Project)
        .where(Project.id == project.id)
        .values(open_annotation_count=Project.open_annotation_count + 1)
    )

    await db.commit()
    ann = await _get_or_404(db, ann.id)
    return _to_out(ann, user, is_superuser(user), drawing.version)


async def list_annotations(
    db: AsyncSession,
    user: User,
    *,
    page: int,
    page_size: int,
    project_id: uuid.UUID | None,
    drawing_id: uuid.UUID | None,
    status: AnnotationStatus | None,
    assignee_id: uuid.UUID | None,
    severity: Severity | None,
    keyword: str | None,
) -> Page[AnnotationOut]:
    # 数据隔离：三种口径都要能反查到"我能看哪些项目"
    # 1) 指定 project_id → 项目成员校验（ADMIN 放行）
    # 2) 只给 drawing_id → 反查 project_id 后再校验
    # 3) 都不给（全局批注页）→ 非管理员按 project_members 子查询收敛；
    #    ADMIN 看全部未删除项目。跨项目列表按 updated_at 降序（任务 2）。
    cross_project = False
    if project_id is None:
        if drawing_id is not None:
            project_id = (
                await db.execute(select(Drawing.project_id).where(Drawing.id == drawing_id))
            ).scalar_one_or_none()
            if project_id is None:
                raise NotFound("图纸不存在")
        else:
            cross_project = True

    if project_id is not None:
        await ensure_project_member(db, project_id, user)

    conditions = [Annotation.is_deleted.is_(False)]
    if project_id is not None:
        conditions.append(Annotation.project_id == project_id)
    elif is_superuser(user):
        conditions.append(
            Annotation.project_id.in_(
                select(Project.id).where(Project.is_deleted.is_(False))
            )
        )
    else:
        # 关联 project_members（含未删除项目过滤），子查询而不是把 id 列表拉回 Python：
        # 项目多时 IN (几百个 UUID) 会让参数包膨胀，也让执行计划无法复用。
        conditions.append(
            Annotation.project_id.in_(
                select(ProjectMember.project_id)
                .join(Project, Project.id == ProjectMember.project_id)
                .where(
                    ProjectMember.user_id == user.id,
                    Project.is_deleted.is_(False),
                )
            )
        )
    if drawing_id is not None:
        conditions.append(Annotation.drawing_id == drawing_id)
    if status is not None:
        conditions.append(Annotation.status == status)
    if assignee_id is not None:
        conditions.append(Annotation.assignee_id == assignee_id)
    if severity is not None:
        conditions.append(Annotation.severity == severity)
    if keyword:
        conditions.append(Annotation.title.ilike(f"%{keyword}%"))

    total = (
        await db.execute(select(func.count()).select_from(Annotation).where(*conditions))
    ).scalar_one()

    # 项目内视图沿用 prd-03 §5.2 的「严重度优先」；跨项目视图按最近变更排序，
    # 否则低严重度的新批注会被历史 BLOCKER 永久压在后面，全局页失去时间感。
    if cross_project:
        order_by = (Annotation.updated_at.desc(), Annotation.id.desc())
    else:
        order_by = (SEVERITY_RANK.asc(), Annotation.updated_at.desc(), Annotation.id.desc())

    anns_rows = (
        await db.execute(
            select(Annotation, Project.name, Drawing.name, Drawing.version)
            .join(Project, Project.id == Annotation.project_id)
            .join(Drawing, Drawing.id == Annotation.drawing_id)
            .where(*conditions)
            .order_by(*order_by)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    admin = is_superuser(user)
    # 项目名 / 图纸名 / 当前版本号随主查询一起 join 出来：
    # 之前列表为了这三列额外跑 2~3 条查询，压测里它们占了一半往返。
    items = [
        _to_out(
            ann,
            user,
            admin,
            version,
            project_name=project_name,
            drawing_name=drawing_name,
        )
        for ann, project_name, drawing_name, version in anns_rows
    ]
    return Page[AnnotationOut](
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )


async def _name_maps(
    db: AsyncSession, project_ids: set[uuid.UUID], drawing_ids: set[uuid.UUID]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    """项目名 / 图纸名的批量映射（全局批注页一行内要显示归属）。"""
    projects: dict[uuid.UUID, str] = {}
    drawings: dict[uuid.UUID, str] = {}
    if project_ids:
        projects = dict(
            (
                await db.execute(
                    select(Project.id, Project.name).where(Project.id.in_(project_ids))
                )
            ).all()
        )
    if drawing_ids:
        drawings = dict(
            (
                await db.execute(
                    select(Drawing.id, Drawing.name).where(Drawing.id.in_(drawing_ids))
                )
            ).all()
        )
    return projects, drawings


async def get_annotation(db: AsyncSession, annotation_id: uuid.UUID, user: User) -> AnnotationOut:
    ann = await _get_or_404(db, annotation_id)
    await ensure_project_member(db, ann.project_id, user)

    version = (
        await db.execute(select(Drawing.version).where(Drawing.id == ann.drawing_id))
    ).scalar_one_or_none() or ann.drawing_version
    project_names, drawing_names = await _name_maps(
        db, {ann.project_id}, {ann.drawing_id}
    )
    return _to_out(
        ann,
        user,
        is_superuser(user),
        version,
        project_name=project_names.get(ann.project_id),
        drawing_name=drawing_names.get(ann.drawing_id),
    )


async def update_annotation(
    db: AsyncSession, annotation_id: uuid.UUID, payload: AnnotationUpdateRequest, user: User
) -> AnnotationOut:
    ann = await _get_or_404(db, annotation_id)
    await ensure_project_member(db, ann.project_id, user)

    admin = is_superuser(user)
    _ensure_can_edit(ann, user, admin)

    if ann.status is AnnotationStatus.CLOSED:
        raise Conflict("批注已关闭，不可编辑", code="INVALID_STATE_TRANSITION",
                       detail={"status": ann.status.value})

    data = payload.model_dump(exclude_unset=True, exclude={"row_version"})

    assignee_changed = False
    if "assignee_id" in data:
        await _assert_assignee_is_member(db, ann.project_id, data["assignee_id"])
        assignee_changed = data["assignee_id"] != ann.assignee_id
        if data["assignee_id"] is not None:
            data["assignee_missing"] = False

    if "location" in data and data["location"] is not None:
        from app.schemas.drawing import Location

        loc_in = Location.model_validate(data["location"])
        loc, valid = clip_location(loc_in)
        data["location"] = loc.model_dump(mode="json")
        data["location_valid"] = valid

    # 乐观锁用「条件更新」而不是「读到内存再比」：
    # WHERE row_version = 客户端版本，命中 0 行即表示期间被别人改过（复盘 2.3）
    values = {**data, "row_version": Annotation.row_version + 1,
              "updated_at": datetime.now(timezone.utc)}
    result = await db.execute(
        update(Annotation)
        .where(
            Annotation.id == annotation_id,
            Annotation.row_version == payload.row_version,
            Annotation.is_deleted.is_(False),
        )
        .values(**values)
    )
    if (result.rowcount or 0) == 0:
        await db.rollback()
        latest = (
            await db.execute(
                select(Annotation.row_version).where(Annotation.id == annotation_id)
            )
        ).scalar_one_or_none()
        raise Conflict(
            "该批注已被他人修改，请刷新后重试",
            detail={"row_version": latest},
        )

    if assignee_changed:
        db.add(
            AnnotationStatusLog(
                annotation_id=annotation_id,
                log_type=LogType.ASSIGNED,
                from_assignee_id=ann.assignee_id,
                to_assignee_id=data["assignee_id"],
                operator_id=user.id,
            )
        )

    await db.commit()
    refreshed = await _get_or_404(db, annotation_id)
    version = (
        await db.execute(select(Drawing.version).where(Drawing.id == refreshed.drawing_id))
    ).scalar_one_or_none() or refreshed.drawing_version
    return _to_out(refreshed, user, admin, version)


async def delete_annotation(
    db: AsyncSession, annotation_id: uuid.UUID, user: User, remark: str | None = None
) -> None:
    """作废（软删除）。状态机不新增"作废"态，避免污染统计口径（prd-03 §5.1）。"""
    ann = await _get_or_404(db, annotation_id)
    await ensure_project_member(db, ann.project_id, user)

    admin = is_superuser(user)
    if ann.status is AnnotationStatus.CLOSED and not admin:
        raise Forbidden("已关闭的批注仅管理员可删除", detail={"status": ann.status.value})
    _ensure_can_edit(ann, user, admin)

    was_open = ann.status in OPEN_STATUSES
    ann.is_deleted = True
    ann.row_version += 1

    db.add(
        AnnotationStatusLog(
            annotation_id=ann.id,
            log_type=LogType.DELETED,
            from_status=ann.status,
            operator_id=user.id,
            remark=remark,
        )
    )
    if was_open:
        await db.execute(
            update(Project)
            .where(Project.id == ann.project_id, Project.open_annotation_count > 0)
            .values(open_annotation_count=Project.open_annotation_count - 1)
        )

    audit_service.record(
        db,
        action=AuditAction.ANNOTATION_DELETE,
        target_type="ANNOTATION",
        user=user,
        target_id=ann.id,
        target_name=ann.title,
        detail={
            "project_id": str(ann.project_id),
            "drawing_id": str(ann.drawing_id),
            "status": (ann.status.value if ann.status else None),
            "via": "DELETE /annotations/{id}",
        },
    )
    await db.commit()


# ---------------------------------------------------------------------------
# 状态流转（prd-03 §5）
# ---------------------------------------------------------------------------
async def _drawing_brief(db: AsyncSession, drawing_id: uuid.UUID) -> tuple[str, int]:
    row = (
        await db.execute(select(Drawing.name, Drawing.version).where(Drawing.id == drawing_id))
    ).first()
    return (row[0], row[1]) if row else ("图纸", 0)


async def _user_name(db: AsyncSession, user_id: uuid.UUID | None) -> str | None:
    if user_id is None:
        return None
    return (await db.execute(select(User.name).where(User.id == user_id))).scalar_one_or_none()


async def _apply_transition(
    db: AsyncSession,
    ann: Annotation,
    payload: AnnotationTransitionRequest | BatchTransitionRequest,
    user: User,
    admin: bool,
    *,
    expected_row_version: int | None,
    drawing_name: str,
    drawing_version: int,
) -> AnnotationOut | None:
    """执行一次流转：条件更新 + 状态日志 + 领域事件（同一事务，不在这里 commit）。

    `expected_row_version=None` 表示批量模式：批量请求体里没有逐条版本号
    （prd-07 §6.7 的契约如此），并发保护退化为"状态条件更新"。
    """
    action = payload.action
    now = datetime.now(timezone.utc)
    new_row_version = (expected_row_version or ann.row_version) + 1

    # ---- 作废：软删除，不进状态枚举 ----
    if action is TransitionAction.DELETE:
        if ann.status is AnnotationStatus.CLOSED and not admin:
            raise Forbidden("已关闭的批注仅管理员可删除", detail={"status": ann.status.value})
        ann.is_deleted = True
        ann.row_version += 1
        snapshot = _to_out(ann, user, admin, drawing_version)
        db.add(
            AnnotationStatusLog(
                annotation_id=ann.id,
                log_type=LogType.DELETED,
                from_status=ann.status,
                operator_id=user.id,
                remark=payload.close_remark,
            )
        )
        audit_service.record(
            db,
            action=AuditAction.ANNOTATION_DELETE,
            target_type="ANNOTATION",
            user=user,
            target_id=ann.id,
            target_name=ann.title,
            detail={
                "project_id": str(ann.project_id),
                "drawing_id": str(ann.drawing_id),
                "status": ann.status.value,
                "via": "POST /annotations/{id}/transition:DELETE",
                "remark": payload.close_remark,
            },
        )
        if ann.status in OPEN_STATUSES:
            await db.execute(
                update(Project)
                .where(Project.id == ann.project_id, Project.open_annotation_count > 0)
                .values(open_annotation_count=Project.open_annotation_count - 1)
            )
        return snapshot

    # 先按住旧值：下面的 `update()` 会 synchronize 到内存对象，
    # `ann.status` 语句执行后立刻变成新值，直接读会把 from_status 写成 to_status
    # （D5 复盘 2.1：审计日志"从已下发改到已下发"就是这么来的）
    from_status = ann.status
    from_assignee_id = ann.assignee_id

    to_status = sm.next_status(ann.status, action)
    if to_status is None:  # 防御：状态机没覆盖到的组合一律按非法流转处理
        raise InvalidStateTransition(
            "当前状态不支持该操作", detail={"from": from_status.value, "action": action.value}
        )

    new_assignee_id = payload.assignee_id if action is TransitionAction.ISSUE else ann.assignee_id
    version_drawing = None
    version_drawing_id = getattr(payload, "version_drawing_id", None)
    if action is TransitionAction.MARK_MODIFIED and version_drawing_id is not None:
        version_drawing = await _drawing_in_project(db, version_drawing_id, ann.project_id)

    values: dict = {
        "status": to_status,
        "row_version": Annotation.row_version + 1,
        "updated_at": now,
    }
    if action is TransitionAction.ISSUE:
        values["assignee_id"] = new_assignee_id
        values["assignee_missing"] = False
    if version_drawing is not None:
        values["version_drawing_id"] = version_drawing.id
    if action is TransitionAction.CLOSE:
        values["closed_at"] = now
        if payload.close_remark:
            values["close_remark"] = payload.close_remark
    if action is TransitionAction.REJECT_AND_REISSUE:
        # 重新下发 = 又开了一条待办，closed_at 必须清掉（统计口径按 closed_at）
        values["closed_at"] = None

    # 条件更新：WHERE row_version = 客户端版本（单条）或 仅状态匹配（批量）
    conditions = [Annotation.id == ann.id, Annotation.is_deleted.is_(False)]
    if expected_row_version is not None:
        conditions.append(Annotation.row_version == expected_row_version)
    result = await db.execute(update(Annotation).where(*conditions).values(**values))
    if (result.rowcount or 0) == 0:
        await db.rollback()
        latest = (
            await db.execute(select(Annotation.row_version).where(Annotation.id == ann.id))
        ).scalar_one_or_none()
        raise Conflict(
            "该批注已被他人修改，请刷新后重试", detail={"row_version": latest}
        )

    # ---- 状态日志（审计要求：只插入，不更新不删除）----
    # created_at 必须显式写：SQLite 的 CURRENT_TIMESTAMP 只有秒精度，
    # 同一次流转里的多条日志会拿到同一个时间戳，时间线排序就成了随机。
    remark = None
    if action is TransitionAction.REJECT_AND_REISSUE:
        remark = payload.reject_reason
    db.add(
        AnnotationStatusLog(
            annotation_id=ann.id,
            log_type=LogType.STATUS_CHANGED,
            from_status=from_status,
            to_status=to_status,
            from_assignee_id=from_assignee_id,
            to_assignee_id=new_assignee_id,
            operator_id=user.id,
            remark=remark,
            created_at=now,
        )
    )
    if action is TransitionAction.CLOSE:
        db.add(
            AnnotationStatusLog(
                annotation_id=ann.id,
                log_type=LogType.CLOSED,
                from_status=from_status,
                to_status=to_status,
                operator_id=user.id,
                remark=payload.close_remark,
                created_at=now + timedelta(microseconds=1),
            )
        )
        await db.execute(
            update(Project)
            .where(Project.id == ann.project_id, Project.open_annotation_count > 0)
            .values(open_annotation_count=Project.open_annotation_count - 1)
        )
    if version_drawing is not None:
        db.add(
            AnnotationStatusLog(
                annotation_id=ann.id,
                log_type=LogType.VERSION_LINKED,
                from_status=from_status,
                to_status=to_status,
                operator_id=user.id,
                remark=f"关联到图纸 v{version_drawing.version}",
                created_at=now + timedelta(microseconds=2),
            )
        )

    # ---- 领域事件：与业务写入同事务（prd-04 §9.1）----
    event_type = sm.ACTION_EVENT_TYPE[action]
    if event_type is not None:
        assignee_name = (
            ann.assignee.name
            if new_assignee_id is not None and new_assignee_id == ann.assignee_id and ann.assignee
            else await _user_name(db, new_assignee_id)
        )
        await enqueue(
            db,
            event_type=event_type.value,
            aggregate_type="ANNOTATION",
            aggregate_id=ann.id,
            version=new_row_version,  # 用流转后的版本号做幂等键：同一版本只发一次
            payload=sm.event_payload(
                ann,
                action=action,
                operator=user,
                creator_name=ann.creator.name,
                assignee_id=new_assignee_id,
                assignee_name=assignee_name,
                drawing_name=drawing_name,
                close_remark=payload.close_remark,
            ),
            project_id=ann.project_id,
            actor_id=user.id,
        )
    return None


async def transition(
    db: AsyncSession, annotation_id: uuid.UUID, payload: AnnotationTransitionRequest, user: User
) -> AnnotationOut:
    """单条流转（prd-03 §5.5.3 的五步范式，一步都不能省）。"""
    # 1) 取记录
    ann = await _get_or_404(db, annotation_id)
    # 2) 数据权限①：项目归属
    await ensure_project_member(db, ann.project_id, user)
    admin = is_superuser(user)
    # 3) 数据权限②：动作 vs 身份（功能权限已由依赖注入校验）
    sm.ensure_action_allowed(ann, user, payload.action, admin)
    # 4) 乐观锁：用"客户端版本 vs 库里版本"直接比，命中即 409
    if ann.row_version != payload.row_version:
        raise Conflict("该批注已被他人修改，请刷新后重试", detail={"row_version": ann.row_version})
    # 5) 状态机合法性（非法 → 422，不是 409）
    sm.ensure_transition_legal(ann.status, payload.action)
    sm.ensure_action_fields(
        payload.action, assignee_id=payload.assignee_id, reject_reason=payload.reject_reason
    )
    if payload.action is TransitionAction.ISSUE:
        await _assert_assignee_is_member(db, ann.project_id, payload.assignee_id)

    drawing_name, drawing_version = await _drawing_brief(db, ann.drawing_id)
    snapshot = await _apply_transition(
        db,
        ann,
        payload,
        user,
        admin,
        expected_row_version=payload.row_version,
        drawing_name=drawing_name,
        drawing_version=drawing_version,
    )
    await db.commit()

    if snapshot is not None:  # DELETE：返回作废前的快照
        return snapshot

    refreshed = await _get_or_404(db, annotation_id)
    return _to_out(refreshed, user, admin, drawing_version)


async def batch_transition(
    db: AsyncSession, payload: BatchTransitionRequest, user: User
) -> BatchTransitionResponse:
    """批量流转（prd-03 §5.4）：默认严格模式，一条非法则整体 409 且**零变更**。

    关键点：校验阶段跑完之前**一条都不写**。这样"严格模式整体回滚"不是靠事务回滚
    实现的，而是压根没开始写 —— 回滚在长事务里会牵扯锁和自增，能不做就不做。
    """
    admin = is_superuser(user)
    anns = (
        await db.execute(
            select(Annotation).where(
                Annotation.id.in_(payload.annotation_ids), Annotation.is_deleted.is_(False)
            )
        )
    ).scalars().all()
    by_id = {a.id: a for a in anns}

    invalid: list[InvalidItem] = []
    for aid in payload.annotation_ids:
        if aid not in by_id:
            invalid.append(
                InvalidItem(annotation_id=aid, reason="批注不存在或已作废", code="NOT_FOUND")
            )

    valid: list[Annotation] = []
    for ann in anns:  # 逐条校验，不能只校验第一条
        try:
            await ensure_project_member(db, ann.project_id, user)
            sm.ensure_action_allowed(ann, user, payload.action, admin)
            sm.ensure_transition_legal(ann.status, payload.action)
            sm.ensure_action_fields(
                payload.action,
                assignee_id=payload.assignee_id,
                reject_reason=payload.reject_reason,
            )
            if payload.action is TransitionAction.ISSUE:
                await _assert_assignee_is_member(db, ann.project_id, payload.assignee_id)
        except (Forbidden, InvalidStateTransition, ValidationFailed, NotFound) as exc:
            invalid.append(InvalidItem(annotation_id=ann.id, reason=exc.message, code=exc.code))
            continue
        valid.append(ann)

    if invalid and not payload.skip_invalid:
        raise Conflict(
            "存在无法处理的批注，已取消全部操作",
            code="BATCH_PARTIAL_INVALID",
            detail={
                "invalid_items": [i.model_dump(mode="json") for i in invalid],
                "total": len(payload.annotation_ids),
                "invalid": len(invalid),
            },
        )

    drawings: dict[uuid.UUID, tuple[str, int]] = {}
    if valid:
        rows = (
            await db.execute(
                select(Drawing.id, Drawing.name, Drawing.version).where(
                    Drawing.id.in_({a.drawing_id for a in valid})
                )
            )
        ).all()
        drawings = {rid: (name, version) for rid, name, version in rows}

    for ann in valid:
        name, version = drawings.get(ann.drawing_id, ("图纸", ann.drawing_version))
        await _apply_transition(
            db,
            ann,
            payload,
            user,
            admin,
            expected_row_version=None,
            drawing_name=name,
            drawing_version=version,
        )
    await db.commit()

    return BatchTransitionResponse(succeeded=len(valid), failed=invalid)

