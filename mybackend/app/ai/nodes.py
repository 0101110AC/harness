"""LangGraph 节点实现（prd-06 §4.1）。

每个节点签名固定 `async def node(state) -> dict`（铁律 3）：
- 只返回**增量**，不返回完整 state；
- 只能通过 DB / checkpoint 拿到外部世界，不得依赖进程内单例；
- 所有副作用必须**幂等**：`interrupt()` 恢复后，被中断的节点会从头再跑一遍。

引擎边界：节点只调 `engine.get_engine()` 暴露的语义方法（parse/metrics/issues/…），
不关心数据来自 `mock_engine`（checksum 派生的确定性假数据）还是 `real_engine`
（`ezdxf` / `PyMuPDF` 真实解析）。切换靠环境变量 `REVIEW_ENGINE`，见 `graph.py`。

数值护栏（prd-06 §7）挂在两个出口上：`validate_metrics` 与 `detect_issues`。
节点里不允许出现"手写的 fact_ref"，ref 一律由引擎从工具返回里生成。
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from langgraph.types import interrupt
from sqlalchemy import delete, func, select, update

from app.ai import engine as engine_mod
from app.ai import report_build
from app.ai.events import STAGE_TEXT, publish_event
from app.ai.guard import (
    CONFIDENCE_CAP,
    known_values,
    scan_text_numbers,
    verify_fact_refs,
)
from app.ai.interrupt_repo import default_expiry, interrupt_uuid, open_interrupt
from app.ai.state import (
    ArchReviewState,
    DrawingRef,
    ReportDraftModel,
    RiskDecision,
    RiskItem,
    UserPrefs,
    prune_state,
    state_digest,
)
from app.core.config import settings
from app.database import AsyncSessionLocal
from app.models.agent import AgentStateCheckpoint
from app.models.drawing import Drawing
from app.models.enums import (
    Discipline,
    InterruptType,
    IssueCategory,
    IssueStatus,
    NotificationType,
    ReportStatus,
    ReviewStage,
    Severity,
)
from app.models.project import Project
from app.models.review import ReportDraft, ReviewIssue, ReviewTask
from app.schemas.drawing import Location, clip_location

log = logging.getLogger(__name__)

# 节点 → (ReviewStage, 进度百分比)，与 prd-06 §9.7 的展示文案表对齐
NODE_STAGE: dict[str, tuple[str, int]] = {
    "load_context": ("LOAD_CONTEXT", 3),
    "plan": ("PLANNING", 8),
    "parse_drawings": ("PARSING", 20),
    "retrieve_norms": ("RETRIEVING", 38),
    "validate_metrics": ("VALIDATING", 55),
    "detect_issues": ("DETECTING", 70),
    "risk_check": ("WAITING_RISK", 78),
    "generate_report": ("REPORTING", 88),
    "report_confirm": ("WAITING_REPORT", 95),
    "package_assets": ("PACKAGING", 98),
}

RISK_LEVELS_NEEDING_CONFIRM = {"HIGH", "CRITICAL"}


class RunFailed(Exception):
    """可预期失败 → 任务 FAILED + run_failed(reason)。不可预期异常由 runtime 兜底。"""

    def __init__(self, reason: str, message: str, retriable: bool = True) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.retriable = retriable


class Bus:
    """节点内的事件出口：每个节点自己 push，不需要在外部拼装。"""

    def __init__(self, state: ArchReviewState, node: str) -> None:
        self.thread_id = str(state["thread_id"])
        self.run_id = str(state.get("run_id") or "")
        self.node = node

    async def emit(self, event: str, data: dict[str, Any]) -> int:
        return await publish_event(
            thread_id=self.thread_id, run_id=self.run_id, event=event, data=data
        )

    async def progress(self, stage: str, percent: int | None = None, text: str | None = None) -> int:
        default_percent, default_text = STAGE_TEXT[stage]
        return await self.emit(
            "progress",
            {
                "percent": default_percent if percent is None else percent,
                "stage": stage,
                "text": text or default_text,
                "node": self.node,
            },
        )


# ---------------------------------------------------------------------------
# 工具调用记账 + 事件下发（mock / real 共用同一条路径）
# ---------------------------------------------------------------------------
def merge_tool_calls(
    state: ArchReviewState, calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按 call_id 去重合并。节点重放（HITL resume）时必须幂等，否则 ref 会翻倍。"""
    merged = {str(c.get("call_id")): c for c in (state.get("tool_calls") or [])}
    for call in calls:
        merged[str(call.get("call_id"))] = call
    return list(merged.values())


async def emit_tool_events(
    bus: Bus, calls: list[dict[str, Any]], known: set[str] | None = None
) -> None:
    """把工具调用记录翻译成 `tool_call` / `tool_result` 事件（prd-06 §9.4）。"""
    known = known or set()
    for call in calls:
        call_id = str(call.get("call_id"))
        if call_id in known:
            continue  # 已发过的事件不再重复（节点重放）
        await bus.emit(
            "tool_call",
            {
                "tool": call.get("tool"),
                "args_summary": call.get("args_summary"),
                "sandbox": call.get("sandbox"),
                "call_id": call_id,
            },
        )
        await bus.emit(
            "tool_result",
            {
                "tool": call.get("tool"),
                "call_id": call_id,
                "ok": bool(call.get("ok")),
                "duration_ms": call.get("duration_ms", 0),
                "result_summary": call.get("result_summary") or call.get("code") or "",
                "artifact": call.get("tool"),
            },
        )


def bump_budget(state: ArchReviewState, calls: list[dict[str, Any]]) -> dict[str, Any]:
    budget = dict(state.get("budget") or {})
    budget["used_tool_calls"] = int(budget.get("used_tool_calls", 0)) + len(calls)
    budget["used_steps"] = int(budget.get("used_steps", 0)) + 1
    return budget


async def apply_numeric_guard(
    bus: Bus,
    items: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    *,
    label: str,
    scan_text: bool,
    text_fields: tuple[str, ...] = ("title", "description", "suggestion"),
    cap_key: str | None = "confidence",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """节点出口的数值护栏（prd-06 §7.3），并把结果作为事件送到思考面板。"""
    guarded, violations = verify_fact_refs(
        items, tool_calls, text_fields=text_fields, scan_text=scan_text,
        label=label, cap_key=cap_key,
    )
    await bus.emit(
        "numeric_guard",
        {
            "checked": len(guarded),
            "downgraded": len(violations),
            "mode": "FULL_SCAN" if scan_text else "FIELD_ONLY",
            "label": label,
            "violations": violations[:10],
        },
    )
    return guarded, violations


# ---------------------------------------------------------------------------
# 节点样板：进入发 node_started，离开发 node_finished + 写可读快照
# ---------------------------------------------------------------------------
async def _begin(state: ArchReviewState, node: str) -> tuple[Bus, float]:
    stage, percent = NODE_STAGE[node]
    bus = Bus(state, node)
    await bus.emit(
        "node_started",
        {"node": node, "stage": stage, "percent": percent, "text": STAGE_TEXT[stage][1]},
    )
    if settings.MOCK_NODE_DELAY_MS:
        import asyncio

        await asyncio.sleep(settings.MOCK_NODE_DELAY_MS / 1000)
    return bus, time.monotonic()


async def _end(
    state: ArchReviewState, node: str, result: dict[str, Any], started: float, bus: Bus
) -> dict[str, Any]:
    stage, percent = NODE_STAGE[node]
    merged = {**dict(state), **result}
    await _write_checkpoint(merged, node)
    await bus.emit(
        "node_finished",
        {
            "node": node,
            "stage": stage,
            "percent": int(result.get("progress", percent)),
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return result


async def _write_checkpoint(state: dict[str, Any], node: str) -> None:
    """写 `agent_state_checkpoints`：只用于排障/回放，恢复的唯一权威仍是 LangGraph checkpoint。"""
    try:
        snapshot = prune_state(state)
        thread_id = str(state.get("thread_id") or "")
        if not thread_id:
            return
        interrupt_id = None
        raw = (state.get("interrupt") or {}).get("interrupt_id")
        if raw:
            try:
                interrupt_id = uuid.UUID(str(raw))
            except ValueError:
                interrupt_id = None

        async with AsyncSessionLocal() as db:
            seq = (
                await db.execute(
                    select(func.coalesce(func.max(AgentStateCheckpoint.seq), 0)).where(
                        AgentStateCheckpoint.thread_id == thread_id
                    )
                )
            ).scalar_one()
            db.add(
                AgentStateCheckpoint(
                    thread_id=thread_id,
                    run_id=str(state.get("run_id") or ""),
                    seq=int(seq) + 1,
                    node=node,
                    run_status=str(state.get("status") or "RUNNING"),
                    state_snapshot=snapshot,
                    state_digest=state_digest(snapshot),
                    interrupt_id=interrupt_id,
                )
            )
            await db.commit()
    except Exception:  # 观测数据不能影响主流程
        log.warning("write checkpoint failed node=%s", node, exc_info=True)


async def _load_task_drawing(state: ArchReviewState) -> tuple[ReviewTask, Drawing, Project]:
    async with AsyncSessionLocal() as db:
        task = (
            await db.execute(
                select(ReviewTask).where(ReviewTask.thread_id == str(state["thread_id"]))
            )
        ).scalar_one_or_none()
        if task is None:
            raise RunFailed("TASK_NOT_FOUND", "预审任务不存在", retriable=False)
        drawing = (
            await db.execute(
                select(Drawing).where(
                    Drawing.id == task.drawing_id, Drawing.is_deleted.is_(False)
                )
            )
        ).scalar_one_or_none()
        if drawing is None:
            raise RunFailed("DRAWING_NOT_FOUND", "图纸不存在或已删除", retriable=False)
        project = (
            await db.execute(select(Project).where(Project.id == task.project_id))
        ).scalar_one_or_none()
        if project is None:
            raise RunFailed("PROJECT_NOT_FOUND", "项目不存在", retriable=False)
        return task, drawing, project


def _drawing_ref(drawing: Drawing) -> DrawingRef:
    meta = drawing.parse_meta if isinstance(drawing.parse_meta, dict) else {}
    extents = meta.get("extents")
    if extents is None:
        # PDF 的"页面尺寸"也塞进 extents：坐标契约要求 location.page_size 与真实图纸一致
        pages = meta.get("pages") or []
        first = pages[0] if pages and isinstance(pages[0], dict) else {}
        if first.get("w") and first.get("h"):
            extents = {"x": 0.0, "y": 0.0, "w": float(first["w"]), "h": float(first["h"])}
    return DrawingRef(
        drawing_id=str(drawing.id),
        file_type=drawing.file_type.value if hasattr(drawing.file_type, "value") else str(drawing.file_type),
        version=drawing.version,
        checksum=drawing.checksum,
        storage_key=drawing.storage_key,
        page_count=drawing.page_count or meta.get("page_count"),
        extents=extents,
    )


# ---------------------------------------------------------------------------
# 1) load_context
# ---------------------------------------------------------------------------
async def load_context(state: ArchReviewState) -> dict[str, Any]:
    bus, started = await _begin(state, "load_context")
    _task, drawing, project = await _load_task_drawing(state)

    brief = {
        "name": project.name,
        "building_type": project.building_type,
        "scale": f"图纸 {project.drawing_count} 张",
        "design_stage": project.design_stage,
        "description": project.description,
        "key_requirements": [],
    }
    ref = _drawing_ref(drawing)

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Drawing)
            .where(Drawing.id == drawing.id, Drawing.page_count.is_(None))
            .values(page_count=ref.page_count or 1)
        )
        await db.commit()

    await bus.progress("LOAD_CONTEXT")
    result = {
        "project_brief": brief,
        "drawings": [ref.model_dump(mode="json")],
        "user_prefs": UserPrefs().model_dump(mode="json"),
        "current_node": "load_context",
        "status": "RUNNING",
    }
    return await _end(state, "load_context", result, started, bus)


# ---------------------------------------------------------------------------
# 2) plan（七个规范子任务，缺一不可 prd-06 §5.2）
# ---------------------------------------------------------------------------
async def plan(state: ArchReviewState) -> dict[str, Any]:
    """规划节点：工具清单来自注册表 manifest，解析工具按图纸类型动态选择。

    这里不再硬编码 `("s2", "提取图纸信息", "parse_dxf")`：PDF 图纸拿到
    `extract_pdf`，DXF 图纸拿到 `parse_dxf`；工具名不在注册表里直接失败
    （prd-06 §11.2：「工具名不在注册表中时规划校验直接失败」）。
    """
    bus, started = await _begin(state, "plan")
    engine = engine_mod.get_engine()
    known_calls = {str(c.get("call_id")) for c in (state.get("tool_calls") or [])}

    try:
        result = await engine.plan(state)
    except engine_mod.EngineToolError as exc:
        raise RunFailed(exc.code, exc.message, retriable=False) from exc

    payload = result.data or {}
    steps = payload.get("steps") or []
    rationale = payload.get("rationale") or ""
    calls = merge_tool_calls(state, result.tool_calls)

    await emit_tool_events(bus, result.tool_calls, known_calls)
    await bus.emit(
        "plan_created",
        {
            "percent": 8,
            "rationale": rationale,
            "engine": engine.name,
            "steps": [
                {"step_id": s["step_id"], "name": s["name"], "tool": s.get("tool"),
                 "status": "pending"}
                for s in steps
            ],
        },
    )
    await bus.progress("PLANNING")
    errors = list(state.get("errors") or []) + list(result.errors)
    result_state = {
        "plan": steps,
        "engine": engine.name,
        "current_node": "plan",
        "plan_revision": int(state.get("plan_revision", 0)),
        "tool_calls": calls,
        "errors": errors,
        "budget": bump_budget(state, result.tool_calls),
    }
    return await _end(state, "plan", result_state, started, bus)


# ---------------------------------------------------------------------------
# 3) parse_drawings
# ---------------------------------------------------------------------------
# 不写进 DB / State 的解析字段：文本块最多 2000 条，塞进去会撑爆快照
_PARSE_DROP_KEYS = ("text_blocks",)


def _trim_parse(parsed: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in parsed.items() if k not in _PARSE_DROP_KEYS}


async def parse_drawings(state: ArchReviewState) -> dict[str, Any]:
    bus, started = await _begin(state, "parse_drawings")
    engine = engine_mod.get_engine()
    refs = [DrawingRef(**d) for d in state.get("drawings") or []]
    if not refs:
        raise RunFailed("PARSE_FAILED", "没有可解析的图纸", retriable=False)

    known_calls = {str(c.get("call_id")) for c in (state.get("tool_calls") or [])}
    parsed: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = list(state.get("errors") or [])
    for ref in refs:
        try:
            result = await engine.parse(ref, state)
        except engine_mod.EngineToolError as exc:  # 部分失败不阻断（prd-06 §4.3）
            errors.append(
                {
                    "node": "parse_drawings",
                    "code": "PARTIAL_PARSE",
                    "message": f"{ref.drawing_id}: {exc.message}",
                    "retriable": True,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            )
            calls.extend(_failure_calls(exc, ref))
            continue

        await emit_tool_events(bus, result.tool_calls, known_calls)
        calls.extend(result.tool_calls)
        result_state = _trim_parse(result.data)
        parsed.append(result_state)

        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Drawing)
                .where(Drawing.id == uuid.UUID(ref.drawing_id), Drawing.parse_meta.is_(None))
                .values(parse_meta=result_state, page_count=result_state["page_count"])
            )
            await db.commit()

    if not parsed:
        raise RunFailed("PARSE_FAILED", "全部图纸解析失败，文件可能已损坏")
    if errors:
        await bus.emit(
            "agent_thought",
            {"node": "parse_drawings", "text": "部分图纸解析失败，已按可用部分继续预审",
             "tokens": 0},
        )

    await bus.progress("PARSING")
    all_calls = merge_tool_calls(state, calls)
    result_state = {
        "parsed": parsed,
        "artifacts": {
            **(state.get("artifacts") or {}),
            "parse": {"uri": "artifact://parse", "kind": "json", "size": len(str(parsed))},
        },
        "errors": errors,
        "tool_calls": all_calls,
        "budget": bump_budget(state, calls),
        "current_node": "parse_drawings",
    }
    return await _end(state, "parse_drawings", result_state, started, bus)


def _failure_calls(error: Exception, ref: DrawingRef) -> list[dict[str, Any]]:
    """失败的解析也要留痕：`ok=False` 的调用不会产生可用 `fact_ref`（护栏只认成功的）。"""
    if not isinstance(error, engine_mod.EngineToolError):
        return []
    return [
        engine_mod.make_call_record(
            tool="parse_dxf" if ref.file_type == "dxf" else "extract_pdf",
            call_id=f"call_failed_{ref.drawing_id[-8:]}",
            fields={},
            args_summary=f"storage_key={ref.storage_key}",
            result_summary=error.message,
            ok=False,
        )
    ]


# ---------------------------------------------------------------------------
# 4) retrieve_norms
# ---------------------------------------------------------------------------
async def retrieve_norms(state: ArchReviewState) -> dict[str, Any]:
    bus, started = await _begin(state, "retrieve_norms")
    engine = engine_mod.get_engine()
    ref = DrawingRef(**(state.get("drawings") or [{}])[0]) if state.get("drawings") else None
    if ref is None:
        raise RunFailed("NO_DRAWING", "缺少图纸引用", retriable=False)

    known_calls = {str(c.get("call_id")) for c in (state.get("tool_calls") or [])}
    result = await engine.norms(state)
    hits = result.data or []
    await emit_tool_events(bus, result.tool_calls, known_calls)
    top = f"，最高相关度 {hits[0]['score']:.2f}" if hits else ""
    await bus.emit(
        "agent_thought",
        {"node": "retrieve_norms",
         "text": f"按项目业态与图纸类型检索到 {len(hits)} 条规范条文{top}"
                 + ("（检索降级：静态条文白名单）" if any(h.get("source") == "FALLBACK"
                                                            for h in hits) else ""),
         "tokens": 0},
    )
    await bus.progress("RETRIEVING")
    result_state = {
        "norm_hits": hits,
        "tool_calls": merge_tool_calls(state, result.tool_calls),
        "errors": list(state.get("errors") or []) + list(result.errors),
        "budget": bump_budget(state, result.tool_calls),
        "current_node": "retrieve_norms",
    }
    return await _end(state, "retrieve_norms", result_state, started, bus)


# ---------------------------------------------------------------------------
# 5) validate_metrics
# ---------------------------------------------------------------------------
async def validate_metrics(state: ArchReviewState) -> dict[str, Any]:
    bus, started = await _begin(state, "validate_metrics")
    engine = engine_mod.get_engine()
    known_calls = {str(c.get("call_id")) for c in (state.get("tool_calls") or [])}

    result = await engine.metrics(state)
    calls = merge_tool_calls(state, result.tool_calls)
    await emit_tool_events(bus, result.tool_calls, known_calls)

    # 数值护栏第一道出口：指标是报告里最硬的数据，必须每条都能指回 calc_metrics
    metrics, violations = await apply_numeric_guard(
        bus, list(result.data or []), calls,
        label="metrics",
        scan_text=False,                      # 计算口径由工具自己返回，不做文本扫描
        text_fields=("label", "basis"),
        cap_key=None,                         # 指标没有"置信度"，无出处就只能剔除
    )
    verified = [m for m in metrics if m.get("numeric_verified", True)]
    dropped = len(metrics) - len(verified)

    failed = [m for m in verified if not m.get("pass", m.get("passed"))]
    insufficient = [e for e in result.errors if e.get("code") == "INSUFFICIENT_INPUT"]
    thought = f"{len(verified)} 项硬指标校验完成，{len(failed)} 项不满足限值"
    if failed:
        thought += f"：{'、'.join(m['label'] for m in failed)}"
    if insufficient:
        thought += f"；{len(insufficient)} 项因资料缺失无法核算"
    await bus.emit("agent_thought", {"node": "validate_metrics", "text": thought, "tokens": 0})

    errors = list(state.get("errors") or []) + list(result.errors)
    if violations:
        errors.append(
            {"node": "validate_metrics", "code": "UNVERIFIED_NUMBER",
             "message": f"{len(violations)} 项指标无工具出处，已移出报告", "retriable": False}
        )
    await bus.progress("VALIDATING")
    result_state = {
        "metrics": verified,
        "tool_calls": calls,
        "errors": errors,
        "budget": bump_budget(state, result.tool_calls),
        "current_node": "validate_metrics",
    }
    if dropped:
        await bus.emit(
            "agent_thought",
            {"node": "validate_metrics",
             "text": f"{dropped} 项指标无法指回工具调用，已从报告与问题清单中剔除", "tokens": 0},
        )
    return await _end(state, "validate_metrics", result_state, started, bus)


# ---------------------------------------------------------------------------
# 6) detect_issues（确定性 Mock + 坐标校验 + 落库）
# ---------------------------------------------------------------------------
def _severity_enum(value: str) -> Severity:
    return Severity(value)


async def _persist_confirmed_risks(
    state: ArchReviewState,
    task: ReviewTask,
    drawing: Drawing,
    confirmed: list[dict[str, Any]],
) -> None:
    """把"确认是图纸问题"的风险项落成 ReviewIssue（幂等：按 RISK: 前缀先删后插）。

    节点会在 resume 时从头重放，所以这里不能用"插入后就不再管"的写法，
    否则第二次执行会插出重复问题，前端清单会凭空多出一倍。
    """
    bus = Bus(state, "risk_check")
    engine = engine_mod.get_engine()
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(ReviewIssue).where(
                ReviewIssue.review_task_id == task.id,
                ReviewIssue.rule_code.like("RISK:%"),
            )
        )

        rows: list[tuple[ReviewIssue, dict[str, Any]]] = []
        for item in confirmed:
            risk = item["risk"]
            fallback = engine.fallback_location(_drawing_ref(drawing), state)
            row = ReviewIssue(
                review_task_id=task.id,
                drawing_id=drawing.id,
                project_id=task.project_id,
                category=IssueCategory.CODE,
                severity=_severity_enum(
                    "BLOCKER" if risk.get("level") == "CRITICAL" else "MAJOR"
                ),
                discipline=Discipline.GENERAL,
                title=str(risk.get("title"))[:120],
                description=risk.get("why_suspicious") or item.get("remark"),
                suggestion="按规范/规划条件调整后重新提交。",
                rule_code=f"RISK:{risk.get('metric_key') or risk.get('risk_id')}",
                confidence=90,
                location=risk.get("location") or fallback,
                location_valid=bool(risk.get("location")),
                status=IssueStatus.PENDING,
                # 风险确认时已经落过批注草稿：这里把问题挂回那条批注
                # （同一 risk 重放时由 hitl_service 就地回填，两条路径都幂等）
                annotation_id=(
                    uuid.UUID(item["annotation_id"]) if item.get("annotation_id") else None
                ),
            )
            db.add(row)
            rows.append((row, item))
        await db.flush()

        counts = (
            await db.execute(
                select(ReviewIssue.severity, func.count())
                .where(ReviewIssue.review_task_id == task.id)
                .group_by(ReviewIssue.severity)
            )
        ).all()
        by_severity = {s.value: 0 for s in Severity}
        for severity, count in counts:
            by_severity[severity.value if hasattr(severity, "value") else str(severity)] = count
        total = sum(by_severity.values())

        await db.execute(
            update(ReviewTask)
            .where(ReviewTask.id == task.id)
            .values(
                issue_total=total,
                issue_blocker=by_severity.get("BLOCKER", 0),
                summary={"by_severity": by_severity},
            )
        )
        await db.commit()
        new_ids = [(str(row.id), item) for row, item in rows]

    for issue_id, item in new_ids:
        risk = item["risk"]
        await bus.emit(
            "issue_found",
            {
                "issue_id": issue_id,
                "severity": "BLOCKER" if risk.get("level") == "CRITICAL" else "MAJOR",
                "discipline": "GENERAL",
                "title": risk.get("title"),
                "confidence": 90,
                "location_valid": bool(risk.get("location")),
            },
        )


async def detect_issues(state: ArchReviewState) -> dict[str, Any]:
    bus, started = await _begin(state, "detect_issues")
    task, drawing, _project = await _load_task_drawing(state)
    ref = _drawing_ref(drawing)
    engine = engine_mod.get_engine()
    known_calls = {str(c.get("call_id")) for c in (state.get("tool_calls") or [])}

    outcome = await engine.issues(state)
    calls = merge_tool_calls(state, outcome.tool_calls)
    await emit_tool_events(bus, outcome.tool_calls, known_calls)

    # 数值护栏第二道出口：真实引擎下连正文里的数字一起扫（FULL_SCAN），
    # Mock 保持 FIELD_ONLY —— Mock 的标题是固定语料，扫描只会把演示置信度打下去。
    normalized, code_violations = await apply_numeric_guard(
        bus, list(outcome.data or []), calls,
        label="issues",
        scan_text=(engine.name == "real"),
    )

    # 坐标是"事实"：越界一律裁剪并置 location_valid=False（prd-07 §4 校验规则 1）
    for draft in normalized:
        location_valid = bool(draft.get("location_valid", True))
        try:
            loc, ok = clip_location(Location.model_validate(draft["location"]))
            draft["location"] = loc.model_dump(mode="json")
            location_valid = location_valid and ok
            if ref.file_type == "pdf" and ref.page_count and loc.page_no > ref.page_count:
                location_valid = False
        except Exception:
            location_valid = False
        draft["location_valid"] = location_valid

    summary = report_build.build_issue_summary(normalized)
    blocker = sum(1 for i in normalized if i["severity"] == "BLOCKER")

    persisted: list[ReviewIssue] = []
    async with AsyncSessionLocal() as db:
        # 幂等：重规划/重跑会再次进入本节点，先清掉上一轮草稿再写入
        await db.execute(delete(ReviewIssue).where(ReviewIssue.review_task_id == task.id))
        for draft in normalized:
            row = ReviewIssue(
                review_task_id=task.id,
                drawing_id=drawing.id,
                project_id=task.project_id,
                category=IssueCategory(draft["category"]),
                severity=_severity_enum(draft["severity"]),
                discipline=Discipline(draft["discipline"]),
                title=draft["title"],
                description=draft.get("description"),
                suggestion=draft.get("suggestion"),
                rule_code=draft.get("rule_code"),
                confidence=int(draft["confidence"]),
                location=draft["location"],
                location_valid=bool(draft["location_valid"]),
                status=IssueStatus.PENDING,
            )
            db.add(row)
            persisted.append(row)
        await db.flush()
        await db.execute(
            update(ReviewTask)
            .where(ReviewTask.id == task.id)
            .values(issue_total=len(normalized), issue_blocker=blocker, summary=summary)
        )
        await db.commit()
        ids = [str(r.id) for r in persisted]

    for issue_id, draft in zip(ids, normalized):
        await bus.emit(
            "issue_found",
            {
                "issue_id": issue_id,
                "severity": draft["severity"],
                "discipline": draft["discipline"],
                "title": draft["title"],
                "confidence": draft["confidence"],
                "location_valid": draft["location_valid"],
            },
        )

    # 风险项同样过护栏：`detected_value` 是报告里"标红"的那个数，必须能溯源
    raw_risks = engine.risks(state)
    risks, risk_violations = await apply_numeric_guard(
        bus, list(raw_risks or []), calls,
        label="risks",
        scan_text=(engine.name == "real"),
        text_fields=("title", "why_suspicious", "clause_ref"),
    )
    await bus.progress("DETECTING")
    errors = list(state.get("errors") or []) + list(outcome.errors)
    if code_violations or risk_violations:
        errors.append(
            {"node": "detect_issues", "code": "UNVERIFIED_NUMBER",
             "message": f"{len(code_violations) + len(risk_violations)} 条数值未溯源，"
                        f"置信度已压至 {CONFIDENCE_CAP} 以下", "retriable": False}
        )
    result = {
        "issues": normalized,
        "risks": risks,
        "tool_calls": calls,
        "errors": errors,
        "budget": bump_budget(state, outcome.tool_calls),
        "current_node": "detect_issues",
        "status": "RUNNING",
    }
    if risk_violations:
        await bus.emit(
            "agent_thought",
            {"node": "detect_issues",
             "text": f"{len(risk_violations)} 项风险的数值未溯源，已标注'未经工具核对'",
             "tokens": 0},
        )
    return await _end(state, "detect_issues", result, started, bus)


# ---------------------------------------------------------------------------
# 7) risk_check（卡点一，prd-06 §8.1）
# ---------------------------------------------------------------------------
async def risk_check(state: ArchReviewState) -> dict[str, Any]:
    bus, started = await _begin(state, "risk_check")
    task, drawing, _project = await _load_task_drawing(state)

    risks = [RiskItem(**r) for r in (state.get("risks") or [])]
    blocking = [r for r in risks if r.level in RISK_LEVELS_NEEDING_CONFIRM]
    if not blocking:
        await bus.progress("DETECTING", percent=78,
                           text="未发现需人工确认的高风险项，继续生成报告")
        result = {"risks": [], "interrupt": None, "current_node": "risk_check"}
        return await _end(state, "risk_check", result, started, bus)

    revision = int(state.get("plan_revision", 0))
    it_id = interrupt_uuid(str(state["thread_id"]), "RISK_CONFIRM", revision)
    expires_at = await default_expiry(settings.HITL_TTL_HOURS)
    title = f"发现 {len(blocking)} 项高风险指标，需人工确认"
    payload = {
        "items": [r.model_dump(mode="json") for r in blocking],
        "expected_action": ["CONFIRM_AS_ISSUE", "MARK_EXEMPT"],
    }

    created, expires_at = await open_interrupt(
        interrupt_id=it_id,
        thread_id=str(state["thread_id"]),
        run_id=str(state.get("run_id") or ""),
        project_id=drawing.project_id,
        drawing_id=drawing.id,
        review_task_id=task.id,
        itype=InterruptType.RISK_CONFIRM,
        title=title,
        payload=payload,
        expires_at=expires_at,
        stage=ReviewStage.WAITING_RISK,
        # 站内信：工单与提醒同事务落库（prd-04 §4 HITL_PENDING）
        notify={
            "event_type": NotificationType.HITL_PENDING.value,
            "payload": {
                "interrupt_id": str(it_id),
                "project_id": str(drawing.project_id),
                "drawing_id": str(drawing.id),
                "drawing_name": drawing.name,
                "review_task_id": str(task.id),
                "risk_count": len(blocking),
            },
        },
    )

    interrupt_payload = {
        "interrupt_id": str(it_id),
        "type": "RISK_CONFIRM",
        "title": title,
        "reason": f"{len(blocking)} 项硬指标越界，需判断是图纸问题还是已获豁免",
        "items": payload["items"],
        "expected_action": payload["expected_action"],
        "expires_at": expires_at.isoformat(),
    }

    if created:
        # 只有"首次进入"才推事件：节点恢复后会重放，重复推会让前端弹两次窗
        await bus.emit(
            "risk_found",
            {
                "count": len(blocking),
                "items": [{"risk_id": r.risk_id, "title": r.title, "level": r.level} for r in blocking],
            },
        )
        await bus.emit(
            "risk_interrupt",
            {
                "interrupt_id": str(it_id),
                "type": "RISK_CONFIRM",
                "title": title,
                "expires_at": expires_at.isoformat(),
                "items": payload["items"],
            },
        )
        await bus.progress("WAITING_RISK")

    # 注意：这里**不发 node_finished** —— 节点只是挂起，不是完成。
    # 恢复后本节点会从头重放，只有走完才是真的 finished（避免前端把步骤提前打勾）。
    await _write_checkpoint({**dict(state), "interrupt": interrupt_payload}, "risk_check")

    # ← 真正的挂起点：checkpoint 落盘，进程可以安全退出
    decision = interrupt(interrupt_payload)

    # ===== resume 之后从这里继续（节点从头重放，上面的 DB 写必须是幂等的）=====
    raw_decisions = (decision or {}).get("item_decisions") or []
    # 风险确认时后端已按 CONFIRM_AS_ISSUE 生成批注草稿，这里拿回 risk_id → annotation_id
    annotation_ids = (decision or {}).get("annotation_ids") or {}
    by_risk: dict[str, RiskDecision] = {}
    for item in raw_decisions:
        d = RiskDecision(**item)
        by_risk[d.risk_id] = d

    confirmed: list[dict[str, Any]] = []
    exempt: list[dict[str, Any]] = []
    for risk in blocking:
        d = by_risk.get(risk.risk_id)
        if d is None:
            raise RunFailed("HITL_INCOMPLETE", f"风险项 {risk.risk_id} 未处理")
        if d.decision == "CONFIRM_AS_ISSUE":
            confirmed.append(
                {
                    "risk": risk.model_dump(mode="json"),
                    "remark": d.remark,
                    "annotation_id": annotation_ids.get(risk.risk_id),
                }
            )
        else:
            if not (d.remark and len(d.remark.strip()) >= 5):
                raise RunFailed("VALIDATION_ERROR", "特殊豁免必须填写依据（≥5 字）", retriable=False)
            exempt.append({"risk": risk.model_dump(mode="json"), "remark": d.remark})

    issues = list(state.get("issues") or [])
    for item in confirmed:  # 确认的风险 → 追加为问题草稿
        risk = item["risk"]
        issues.append(
            {
                "category": "CODE",
                "severity": "BLOCKER" if risk["level"] == "CRITICAL" else "MAJOR",
                "discipline": "GENERAL",
                "title": risk["title"],
                "description": risk.get("why_suspicious") or item.get("remark"),
                "suggestion": "按规范/规划条件调整后重新提交。",
                "rule_code": f"RISK:{risk.get('metric_key') or risk.get('risk_id')}",
                "confidence": 90,
                "location": risk.get("location"),
                "location_valid": bool(risk.get("location")),
                # 风险的 detected_value 来自 calc_metrics，ref 必须沿用指标的
                "fact_ref": risk.get("fact_ref"),
                "numeric_verified": bool(risk.get("fact_ref")),
            }
        )

    history = list(state.get("hitl_history") or [])
    history.append(
        {
            "interrupt_id": str(it_id),
            "operator_id": (decision or {}).get("operator_id"),
            "operator_name": (decision or {}).get("operator_name"),
            "resolution": "RISK_CONFIRM",
            "remark": (decision or {}).get("remark"),
            "item_decisions": raw_decisions,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(ReviewTask)
            .where(ReviewTask.id == task.id)
            .values(
                waiting_interrupt_id=None,
                run_status="RUNNING",
                stage=ReviewStage.REPORTING,
                stage_text=STAGE_TEXT["REPORTING"][1],
            )
        )
        await db.commit()

    # 确认的风险必须变成"真问题"落库（否则清单里看不到、批注也无处挂）。
    # 用 RISK: 前缀的 rule_code 标记来源，删除+重建保证节点重放幂等。
    await _persist_confirmed_risks(state, task, drawing, confirmed)

    await bus.progress("REPORTING", percent=80, text="风险已处置，继续生成报告")
    result = {
        "issues": issues[:200],
        "risks": [],
        "exempt_risks": exempt,
        "hitl_history": history,
        "interrupt": None,
        "current_node": "risk_check",
        "status": "RUNNING",
    }
    return await _end(state, "risk_check", result, started, bus)


# ---------------------------------------------------------------------------
# 8) generate_report
# ---------------------------------------------------------------------------
async def generate_report(state: ArchReviewState) -> dict[str, Any]:
    bus, started = await _begin(state, "generate_report")
    task, drawing, _project = await _load_task_drawing(state)
    engine = engine_mod.get_engine()

    revision = int(state.get("plan_revision", 0))
    version = revision + 1
    sections = engine.report_sections(state)
    summary = report_build.build_issue_summary(state.get("issues") or [])

    # 报告正文的数值扫描（prd-06 §7.2 V1.5 FULL_SCAN）：指标表不扫 ——
    # 它由 calc_metrics 结果直接渲染，天然带 fact_ref；扫它等于自己扫自己。
    tool_calls = state.get("tool_calls") or []
    values = known_values(tool_calls)
    report_violations: list[dict[str, Any]] = []
    for section in sections:
        if section.get("key") == "metrics":
            continue
        for hit in scan_text_numbers(str(section.get("body_md") or ""), values):
            report_violations.append({"section": section.get("key"), **hit})
    if report_violations:
        await bus.emit(
            "numeric_guard",
            {"checked": len(sections), "downgraded": len(report_violations),
             "mode": "FULL_SCAN", "label": "report", "violations": report_violations[:10]},
        )

    risk_decisions = [
        {"interrupt_id": h.get("interrupt_id"), "resolution": h.get("resolution"),
         "item_decisions": h.get("item_decisions") or []}
        for h in (state.get("hitl_history") or [])
    ]

    async with AsyncSessionLocal() as db:
        draft = (
            await db.execute(
                select(ReportDraft).where(
                    ReportDraft.review_task_id == task.id, ReportDraft.version == version
                )
            )
        ).scalar_one_or_none()
        title = f"《{state.get('project_brief', {}).get('name') or drawing.name}》预审报告初稿 v{version}"
        if draft is None:
            draft = ReportDraft(
                review_task_id=task.id,
                project_id=task.project_id,
                drawing_id=drawing.id,
                version=version,
                status=ReportStatus.WAITING_CONFIRM,
                title=title,
                summary=summary,
                sections=sections,
                risk_decisions=risk_decisions,
            )
            db.add(draft)
        else:
            draft.status = ReportStatus.WAITING_CONFIRM
            draft.summary = summary
            draft.sections = sections
            draft.risk_decisions = risk_decisions
            draft.reject_reason = None
        await db.flush()
        await db.execute(
            update(ReviewTask)
            .where(ReviewTask.id == task.id)
            .values(report_id=draft.id, stage=ReviewStage.REPORTING,
                    stage_text=STAGE_TEXT["REPORTING"][1])
        )
        await db.commit()
        report_id = str(draft.id)

    report_draft = ReportDraftModel(
        report_id=report_id,
        version=version,
        sections=sections,
        issue_summary=summary,
        risk_decisions=risk_decisions,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )

    await bus.emit(
        "report_ready",
        {"percent": 95, "report_id": report_id, "version": version,
         "issue_total": len(state.get("issues") or [])},
    )
    await bus.progress("REPORTING")
    errors = list(state.get("errors") or [])
    if report_violations:
        errors.append(
            {"node": "generate_report", "code": "UNVERIFIED_NUMBER",
             "message": f"报告正文有 {len(report_violations)} 处数值未溯源，"
                        f"已在报告中声明数据来源", "retriable": False}
        )
    result = {
        "report_draft": report_draft.model_dump(mode="json"),
        "errors": errors,
        "current_node": "generate_report",
    }
    return await _end(state, "generate_report", result, started, bus)


# ---------------------------------------------------------------------------
# 9) report_confirm（卡点二）
# ---------------------------------------------------------------------------
async def report_confirm(state: ArchReviewState) -> dict[str, Any]:
    bus, started = await _begin(state, "report_confirm")
    task, drawing, _project = await _load_task_drawing(state)

    report = state.get("report_draft") or {}
    report_id = report.get("report_id")
    revision = int(state.get("plan_revision", 0))
    it_id = interrupt_uuid(str(state["thread_id"]), "REPORT_CONFIRM", revision)
    expires_at = await default_expiry(settings.HITL_TTL_HOURS)
    title = "报告初稿已生成，请确认是否输出正式文档"

    created, expires_at = await open_interrupt(
        interrupt_id=it_id,
        thread_id=str(state["thread_id"]),
        run_id=str(state.get("run_id") or ""),
        project_id=drawing.project_id,
        drawing_id=drawing.id,
        review_task_id=task.id,
        itype=InterruptType.REPORT_CONFIRM,
        title=title,
        payload={"report_id": report_id, "version": report.get("version"), "issue_total": len(state.get("issues") or [])},
        expires_at=expires_at,
        stage=ReviewStage.WAITING_REPORT,
        # 站内信：报告初稿就绪（prd-04 §4 REPORT_READY），与工单同事务
        notify={
            "event_type": NotificationType.REPORT_READY.value,
            "payload": {
                "interrupt_id": str(it_id),
                "project_id": str(drawing.project_id),
                "drawing_id": str(drawing.id),
                "drawing_name": drawing.name,
                "review_task_id": str(task.id),
                "report_id": report_id,
                "issue_total": len(state.get("issues") or []),
            },
        },
    )

    interrupt_payload = {
        "interrupt_id": str(it_id),
        "type": "REPORT_CONFIRM",
        "title": title,
        "reason": "报告初稿需人工确认后才能输出正式文档",
        "report_id": report_id,
        "items": [],
        "expected_action": ["APPROVED", "REJECTED"],
        "expires_at": expires_at.isoformat(),
    }

    if created:
        await bus.emit(
            "report_interrupt",
            {
                "interrupt_id": str(it_id),
                "type": "REPORT_CONFIRM",
                "report_id": report_id,
                "title": title,
                "expires_at": expires_at.isoformat(),
            },
        )
        await bus.progress("WAITING_REPORT")

    await _write_checkpoint({**dict(state), "interrupt": interrupt_payload}, "report_confirm")

    decision = interrupt(interrupt_payload)
    # ===== resume 之后 =====
    choice = str((decision or {}).get("decision") or "APPROVED").upper()
    remark = (decision or {}).get("remark")
    operator_id = (decision or {}).get("operator_id")
    operator_name = (decision or {}).get("operator_name")

    history = list(state.get("hitl_history") or [])

    if choice == "REJECTED":
        if not (remark and len(remark.strip()) >= 10):
            raise RunFailed("VALIDATION_ERROR", "退回意见不少于 10 字", retriable=False)
        async with AsyncSessionLocal() as db:
            if report_id:
                await db.execute(
                    update(ReportDraft)
                    .where(ReportDraft.id == uuid.UUID(report_id))
                    .values(status=ReportStatus.REJECTED, reject_reason=remark)
                )
            await db.execute(
                update(ReviewTask)
                .where(ReviewTask.id == task.id)
                .values(waiting_interrupt_id=None, run_status="RUNNING",
                        stage=ReviewStage.DETECTING, stage_text=STAGE_TEXT["DETECTING"][1])
            )
            await db.commit()

        history.append(
            {"interrupt_id": str(it_id), "operator_id": operator_id, "operator_name": operator_name,
             "resolution": "REJECTED", "remark": remark, "item_decisions": [],
             "resolved_at": datetime.now(timezone.utc).isoformat()}
        )
        await bus.progress("DETECTING", percent=70, text="报告被退回，重新归纳问题")
        result = {
            "plan_revision": revision + 1,  # 路由据此回到 detect_issues（上限 2 次）
            "hitl_history": history,
            "report_draft": None,
            "interrupt": None,
            "current_node": "report_confirm",
        }
        return await _end(state, "report_confirm", result, started, bus)

    async with AsyncSessionLocal() as db:
        if report_id:
            await db.execute(
                update(ReportDraft)
                .where(ReportDraft.id == uuid.UUID(report_id))
                .values(
                    status=ReportStatus.CONFIRMED,
                    confirmed_by=uuid.UUID(operator_id) if operator_id else None,
                    confirmed_at=datetime.now(timezone.utc),
                )
            )
        await db.execute(
            update(ReviewTask)
            .where(ReviewTask.id == task.id)
            .values(waiting_interrupt_id=None, run_status="RUNNING",
                    stage=ReviewStage.PACKAGING, stage_text=STAGE_TEXT["PACKAGING"][1])
        )
        await db.commit()

    history.append(
        {"interrupt_id": str(it_id), "operator_id": operator_id, "operator_name": operator_name,
         "resolution": "APPROVED", "remark": remark, "item_decisions": [],
         "resolved_at": datetime.now(timezone.utc).isoformat()}
    )
    await bus.progress("PACKAGING", percent=98, text="报告已确认，开始打包资料")
    result = {
        "hitl_history": history,
        "interrupt": None,
        "current_node": "report_confirm",
        "status": "RUNNING",
    }
    return await _end(state, "report_confirm", result, started, bus)


# ---------------------------------------------------------------------------
# 10) package_assets
# ---------------------------------------------------------------------------
async def package_assets(state: ArchReviewState) -> dict[str, Any]:
    bus, started = await _begin(state, "package_assets")
    task, _drawing, _project = await _load_task_drawing(state)

    package_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"arch-harness:pkg:{state['thread_id']}"))
    package = {
        "package_id": package_id,
        "uri": f"artifact://review/{task.id}/package.zip",
        "kind": "review_package",
        "size": 1024 * (len(state.get("issues") or []) + 1),
    }
    await bus.emit(
        "tool_result",
        {"tool": "package_assets", "call_id": "call_package", "ok": True, "duration_ms": 180,
         "result_summary": f"打包 {len(state.get('issues') or [])} 条问题 + 报告初稿", "artifact": package_id},
    )
    await bus.progress("PACKAGING")
    result = {"package": package, "current_node": "package_assets"}
    return await _end(state, "package_assets", result, started, bus)
