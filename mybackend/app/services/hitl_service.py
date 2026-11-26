"""HITL 服务：pending / 风险确认恢复 / 报告确认（prd-06 §8.3、prd-07 §7.4.1~§7.4.2）。

HTTP 语义全部在这里，节点里只管"挂起"与"读回 resume 值"：
- 幂等：同一 interrupt_id 重复提交 → 409 INTERRUPT_ALREADY_RESOLVED，并回带首次处理结果；
- 超时：>24h → 410 INTERRUPT_EXPIRED，工单置 EXPIRED，运行置 FAILED(HITL_TIMEOUT)；
- 并发：靠 `resolve_if_pending` 的条件更新，只有一个请求能抢到"恢复权"。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import interrupt_repo
from app.ai.events import publish_event
from app.ai.graph import MAX_PLAN_REVISION
from app.ai.runtime import run_manager
from app.core.deps import ensure_project_member
from app.core.errors import AppError, Conflict, NotFound, ValidationFailed
from app.models.agent import InterruptTask
from app.models.annotation import Annotation
from app.models.drawing import Drawing
from app.models.enums import (
    INTERRUPT_TYPE_TEXT,
    AnnotationSource,
    AnnotationStatus,
    Discipline,
    HitlResolution,
    InterruptStatus,
    InterruptType,
    ReportDecision,
    ReportStatus,
    RunStatus,
    Severity,
    ShapeType,
)
from app.models.project import Project
from app.models.review import ReportDraft, ReviewIssue, ReviewTask
from app.models.user import User
from app.schemas.drawing import BBox, Location, PageSize
from app.schemas.hitl import (
    ConfirmReportRequest,
    ConfirmReportResponse,
    HitlResumeRequest,
    HitlResumeResponse,
    InterruptTaskOut,
    RiskItemOut,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _to_out(task: InterruptTask) -> InterruptTaskOut:
    payload = task.payload or {}
    items = payload.get("items") or []
    remaining = int((task.expires_at - _now()).total_seconds())
    return InterruptTaskOut(
        id=task.id,
        thread_id=task.thread_id,
        review_task_id=task.review_task_id,
        drawing_id=task.drawing_id,
        type=task.type,
        type_text=INTERRUPT_TYPE_TEXT.get(task.type, str(task.type)),
        status=task.status,
        title=task.title,
        items=[RiskItemOut(**item) for item in items],
        report_id=uuid.UUID(str(payload["report_id"])) if payload.get("report_id") else None,
        assignee=None,
        resolution=task.resolution,
        item_decisions=task.item_decisions if isinstance(task.item_decisions, list) else [],
        remark=task.remark,
        expires_at=task.expires_at,
        remaining_seconds=max(0, remaining),
        created_at=task.created_at,
    )


async def get_pending(
    db: AsyncSession, drawing_id: uuid.UUID, user: User
) -> InterruptTaskOut | None:
    """刷新页面后恢复弹窗用；返回 null 表示"没人等着你处理"，直接续流。"""
    await _get_drawing(db, drawing_id, user)
    task = await interrupt_repo.get_pending_for_drawing(db, drawing_id)
    if task is None:
        return None
    if task.expires_at < _now():
        return None
    return _to_out(task)


async def _load_open_interrupt(
    db: AsyncSession,
    *,
    interrupt_id: uuid.UUID,
    drawing_id: uuid.UUID,
    expected: InterruptType,
    user: User,
) -> tuple[InterruptTask, ReviewTask, Drawing]:
    drawing = await _get_drawing(db, drawing_id, user)

    task = await interrupt_repo.get_by_id(db, interrupt_id)
    if task is None or task.drawing_id != drawing_id:
        raise NotFound("中断任务不存在")

    if task.status is InterruptStatus.RESOLVED:
        raise Conflict(
            "该中断已被处理",
            code="INTERRUPT_ALREADY_RESOLVED",
            detail={"resolution": task.resolution,
                    "resolved_at": task.resolved_at.isoformat() if task.resolved_at else None},
        )
    if task.status is InterruptStatus.EXPIRED:
        raise AppError(
            "INTERRUPT_EXPIRED", "该确认已超过 24 小时有效期，请重新发起预审", 410
        )
    if task.status is InterruptStatus.CANCELED:
        raise Conflict("该中断已被取消", code="INTERRUPT_CANCELED")
    if task.type is not expected:
        raise Conflict(
            "中断类型不匹配", code="INTERRUPT_TYPE_MISMATCH",
            detail={"type": task.type.value, "expected": expected.value},
        )

    if task.expires_at < _now():
        # 扫描器还没跑到：这里就地补偿，避免"看起来还能提交"
        from app.ai.hitl import sweep_expired_once

        await sweep_expired_once()
        raise AppError("INTERRUPT_EXPIRED", "该确认已超过 24 小时有效期，请重新发起预审", 410)

    review_task = (
        await db.execute(select(ReviewTask).where(ReviewTask.id == task.review_task_id))
    ).scalar_one_or_none()
    if review_task is None:
        raise NotFound("预审任务不存在")
    return task, review_task, drawing


async def _mark_running(db: AsyncSession, review_task: ReviewTask) -> RunStatus:
    review_task.run_status = RunStatus.RUNNING
    review_task.waiting_interrupt_id = None
    await db.commit()
    return review_task.run_status


# ---------------------------------------------------------------------------
# 卡点一：风险确认 / 特殊豁免
# ---------------------------------------------------------------------------

# 风险项通常带坐标（RiskItemOut.location），但"图签不完整 / 资料缺失"这类风险是
# 整页级判断，引擎不给 bbox。此时落一条"位置待确认"的批注：
# 用 A1 横放（pt）作页面、在页心放一个默认框，并把 location_valid 置 False，
# 前端会打上"位置失效"标记，而不是假装它有精确位置。
_DEFAULT_PAGE = PageSize(w=2383.94, h=1683.78)  # A1 横放（841×594mm @ pt）
_DEFAULT_BBOX = BBox(x=_DEFAULT_PAGE.w / 2 - 120, y=_DEFAULT_PAGE.h / 2 - 70, w=240, h=140)


def _risk_location(item: dict, existing: ReviewIssue | None) -> tuple[dict, bool]:
    """风险项的落库坐标：优先用风险自带坐标，其次复用已有问题，最后退回页心默认框。"""
    raw = item.get("location")
    if raw:
        try:
            return Location.model_validate(raw).model_dump(mode="json"), True
        except ValueError:
            pass  # 坐标不合契约时按"无效"处理，绝不猜

    if existing is not None and existing.location:
        try:
            return Location.model_validate(existing.location).model_dump(mode="json"), bool(
                existing.location_valid
            )
        except ValueError:
            pass

    fallback = Location(
        file_type="pdf",
        page_no=1,
        bbox=_DEFAULT_BBOX,
        unit="pt",
        coord_system="pdf_user_space",
        origin="top-left",
        page_size=_DEFAULT_PAGE,
    )
    return fallback.model_dump(mode="json"), False


def _annotation_content(item: dict, item_remark: str | None, overall_remark: str | None) -> str:
    """批注正文：判断理由 + 条文依据 + 人工备注，保持"为什么判它错"可追溯。"""
    lines: list[str] = []
    if item.get("why_suspicious"):
        lines.append(str(item["why_suspicious"]))
    if item.get("clause_ref"):
        lines.append(f"依据：{item['clause_ref']}")
    detected, threshold = item.get("detected_value"), item.get("threshold")
    if detected is not None or threshold is not None:
        lines.append(f"当前 {detected if detected is not None else '—'} / 限值 {threshold if threshold is not None else '—'}")
    if item_remark:
        lines.append(f"人工备注：{item_remark}")
    if overall_remark:
        lines.append(f"整体备注：{overall_remark}")
    lines.append("（由风险确认自动生成的批注草稿，请复核后下发）")
    return "\n".join(lines)


async def _create_annotations_for_confirmed_risks(
    db: AsyncSession,
    *,
    task: InterruptTask,
    review_task: ReviewTask,
    drawing: Drawing,
    decisions: list,
    user: User,
    overall_remark: str | None,
) -> dict[str, str]:
    """prd-06 §8.3：`CONFIRM_AS_ISSUE` 必须自动生成批注草稿。

    这里只往 session 里 add/flush，**不 commit** —— 由调用方随
    `resolve_if_pending()` 的那次提交一起落盘，保证"插批注 / 改中断状态 / 回填问题"
    在同一个事务里。任何一步失败都整体回滚，不会出现"弹窗已关闭但图上没有框"。
    """
    items = {
        str(item.get("risk_id")): item
        for item in ((task.payload or {}).get("items") or [])
    }

    mapping: dict[str, str] = {}
    for decision in decisions:
        if decision.decision is not HitlResolution.CONFIRM_AS_ISSUE:
            continue  # MARK_EXEMPT 只进报告豁免说明，不产批注

        item = items.get(str(decision.risk_id)) or {}
        rule_code = f"RISK:{item.get('metric_key') or item.get('risk_id') or decision.risk_id}"

        # 节点重放时问题可能已经存在：复用它的坐标，并就地回填 annotation_id
        existing = (
            await db.execute(
                select(ReviewIssue).where(
                    ReviewIssue.review_task_id == review_task.id,
                    ReviewIssue.rule_code == rule_code,
                )
            )
        ).scalars().first()

        location, location_valid = _risk_location(item, existing)

        annotation = Annotation(
            project_id=drawing.project_id,
            drawing_id=drawing.id,
            drawing_version=drawing.version,
            issue_id=existing.id if existing is not None else None,
            source=AnnotationSource.AI,
            shape_type=ShapeType.RECT,
            location=location,
            location_valid=location_valid,
            title=str(item.get("title") or "风险确认")[:60],
            content=_annotation_content(item, decision.remark, overall_remark),
            severity=Severity.BLOCKER if item.get("level") == "CRITICAL" else Severity.MAJOR,
            discipline=Discipline.GENERAL,
            status=AnnotationStatus.PENDING_CONFIRM,
            creator_id=user.id,
        )
        db.add(annotation)
        await db.flush()  # 拿到自增主键（uuid 默认值需要 flush 才可见）

        mapping[str(decision.risk_id)] = str(annotation.id)

        if existing is not None:
            existing.annotation_id = annotation.id

    if mapping:
        # 计数走数据库端原子自增（D2 的教训：ORM 里 += 1 是读-改-写）。
        # 新批注都是 PENDING_CONFIRM，一定落在"未关闭"口径里。
        await db.execute(
            update(Project)
            .where(Project.id == drawing.project_id)
            .values(open_annotation_count=Project.open_annotation_count + len(mapping))
        )
    return mapping

async def resume_risk(
    db: AsyncSession, drawing_id: uuid.UUID, body: HitlResumeRequest, user: User
) -> HitlResumeResponse:
    task, review_task, drawing = await _load_open_interrupt(
        db, interrupt_id=body.interrupt_id, drawing_id=drawing_id,
        expected=InterruptType.RISK_CONFIRM, user=user,
    )
    if review_task.run_status is not RunStatus.WAITING_HUMAN:
        raise Conflict("当前运行不在等待人工状态", code="RUN_NOT_WAITING")

    expected_ids = {str(item.get("risk_id")) for item in (task.payload or {}).get("items") or []}
    given = {str(d.risk_id) for d in body.item_decisions}
    missing = expected_ids - given
    if missing:
        raise ValidationFailed(
            "存在未处理的风险项",
            detail={"field": "item_decisions", "missing": sorted(missing)},
        )
    unknown = given - expected_ids
    if unknown:
        raise ValidationFailed(
            "提交了不存在的风险项",
            detail={"field": "item_decisions", "unknown": sorted(unknown)},
        )

    decisions = [d.model_dump(mode="json") for d in body.item_decisions]

    # prd-06 §8.3：选「确认是图纸问题」→ 自动生成批注草稿。
    # 注意顺序：先 add/flush 批注，再让 resolve_if_pending 那次 commit 一并提交，
    # 这样"插批注 / 改中断状态 / 回填 review_issues.annotation_id"是同一个事务。
    annotation_ids = await _create_annotations_for_confirmed_risks(
        db,
        task=task,
        review_task=review_task,
        drawing=drawing,
        decisions=body.item_decisions,
        user=user,
        overall_remark=body.remark,
    )

    granted = await interrupt_repo.resolve_if_pending(
        db,
        task.id,
        resolved_by=user.id,
        resolution="RISK_CONFIRM",
        item_decisions=decisions,
        remark=body.remark,
    )
    if not granted:
        # 没抢到"恢复权"（别人先提交了）：把刚插入的批注一起回滚掉
        await db.rollback()
        raise Conflict("该中断已被处理", code="INTERRUPT_ALREADY_RESOLVED")

    await _mark_running(db, review_task)

    # 先推 hitl_resolved 再唤醒图：前端关弹窗的时序必须早于后续进度事件
    await publish_event(
        thread_id=task.thread_id,
        run_id=task.run_id,
        event="hitl_resolved",
        data={
            "interrupt_id": str(task.id),
            "type": "RISK_CONFIRM",
            "resolution": "RISK_CONFIRM",
            "operator": user.name,
            "item_decisions": decisions,
            "remark": body.remark,
            "resumed": True,
        },
    )

    await run_manager.resume(
        thread_id=task.thread_id,
        run_id=task.run_id,
        resume_value={
            "interrupt_id": str(task.id),
            "item_decisions": decisions,
            "remark": body.remark,
            "operator_id": str(user.id),
            "operator_name": user.name,
            # risk_check 节点恢复时会把这张表挂到新生成的 ReviewIssue 上
            # （首次提交时问题还不存在，回填只能发生在那一步）
            "annotation_ids": annotation_ids,
        },
    )

    return HitlResumeResponse(
        interrupt_id=task.id,
        status=InterruptStatus.RESOLVED,
        run_status=RunStatus.RUNNING,
        resumed=True,
    )


# ---------------------------------------------------------------------------
# 卡点二：报告确认 / 退回修改
# ---------------------------------------------------------------------------
async def confirm_report(
    db: AsyncSession, drawing_id: uuid.UUID, body: ConfirmReportRequest, user: User
) -> ConfirmReportResponse:
    task, review_task, _drawing = await _load_open_interrupt(
        db, interrupt_id=body.interrupt_id, drawing_id=drawing_id,
        expected=InterruptType.REPORT_CONFIRM, user=user,
    )
    if review_task.run_status is not RunStatus.WAITING_HUMAN:
        raise Conflict("当前运行不在等待人工状态", code="RUN_NOT_WAITING")

    payload = task.payload or {}
    report_id = payload.get("report_id")
    version = int(payload.get("version") or 1)

    if body.decision is ReportDecision.REJECTED and version > MAX_PLAN_REVISION:
        raise Conflict(
            "退回次数已达上限，请直接确认或重新发起预审", code="REPLAN_LIMIT"
        )

    granted = await interrupt_repo.resolve_if_pending(
        db,
        task.id,
        resolved_by=user.id,
        resolution=body.decision.value,
        item_decisions=[],
        remark=body.remark,
    )
    if not granted:
        raise Conflict("该中断已被处理", code="INTERRUPT_ALREADY_RESOLVED")

    await _mark_running(db, review_task)

    await publish_event(
        thread_id=task.thread_id,
        run_id=task.run_id,
        event="hitl_resolved",
        data={
            "interrupt_id": str(task.id),
            "type": "REPORT_CONFIRM",
            "resolution": body.decision.value,
            "operator": user.name,
            "item_decisions": [],
            "resumed": True,
        },
    )

    await run_manager.resume_and_wait(
        thread_id=task.thread_id,
        run_id=task.run_id,
        resume_value={
            "interrupt_id": str(task.id),
            "decision": body.decision.value,
            "remark": body.remark,
            "operator_id": str(user.id),
            "operator_name": user.name,
        },
    )

    await db.refresh(review_task)
    draft = None
    if report_id:
        draft = await db.get(ReportDraft, uuid.UUID(str(report_id)))

    return ConfirmReportResponse(
        report_id=uuid.UUID(str(report_id)) if report_id else None,
        status=draft.status if draft is not None else ReportStatus.REJECTED,
        run_status=review_task.run_status,
        artifact_url=None,
    )
