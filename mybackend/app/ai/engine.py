"""引擎抽象层：`mock` 与 `real` 共用一套接口，靠 `REVIEW_ENGINE` 切换（prd-06 §12.7）。

为什么需要这一层，而不是在节点里 `if settings.REVIEW_ENGINE == "real"`：

1. **切换必须是一行配置、零代码改动**：回退到 Mock 是线上事故时的急救手段，
   如果回退要改代码、重新发版，那它就不算回退手段。
2. **节点只认识语义，不认识实现**：`await engine.parse(ref, state)` 之后，
   不管解析来自 `ezdxf` 还是 checksum 派生的假数据，节点代码完全一样。
3. **`tool_calls` 是护栏的事实底座**：两种引擎都必须交回"这次我调了哪些工具、
   每个工具返回了哪些字段"，否则 `fact_ref` 无法校验（prd-06 §7.2）。

引擎实例是**无状态适配器**：只有配置与注册表引用，不缓存任何任务数据，
因此放进模块级单例不违反"恢复只依赖 checkpoint"的铁律（prd-06 §3.3 铁律 3）。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable

from app.ai import mock_engine
from app.ai.guard import attach_fact_ref
from app.ai.report_build import build_report_sections
from app.ai.state import DrawingRef

log = logging.getLogger(__name__)

# 走沙箱注册表的工具（名字打错必须在规划阶段就失败）
SANDBOX_STEP_TOOLS = {"parse_dxf", "extract_pdf", "query_norm", "calc_metrics", "validate_location"}
# 节点内直接完成的步骤，不对应沙箱工具（prd-06 §5.2 里它们仍是"规范子任务"）
NODE_LEVEL_TOOLS = {"read_brief", "generate_report", "package_assets"}

PARSE_TOOL_BY_TYPE = {"dxf": "parse_dxf", "pdf": "extract_pdf"}

PLAN_TEMPLATE: list[tuple[str, str, str]] = [
    ("s1", "解析项目简介", "read_brief"),
    ("s2", "提取图纸信息", "{parse_tool}"),
    ("s3", "检索规范条文", "query_norm"),
    ("s4", "指标校验", "calc_metrics"),
    ("s5", "生成问题清单", "validate_location"),
    ("s6", "生成报告初稿", "generate_report"),
    ("s7", "打包资料", "package_assets"),
]

RATIONALE = {
    "pdf": "该图纸为 PDF 平面图，需先抽取文本层与页幅，再核对疏散宽度、防火分区与图签完整性",
    "dxf": "该图纸为 DXF 矢量图，需按白名单图层提取几何，再核对图层完整性、图签与指标口径",
}


@dataclass(slots=True)
class EngineResult:
    """引擎单步产物：数据 + 工具调用记录 + 告警/降级信息。"""

    data: Any
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


class EngineToolError(Exception):
    """引擎内部工具失败 → 节点据此决定 PARTIAL_PARSE / PARSE_FAILED。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@runtime_checkable
class ReviewEngine(Protocol):
    name: str

    async def plan(self, state: dict[str, Any]) -> EngineResult: ...
    async def parse(self, ref: DrawingRef, state: dict[str, Any]) -> EngineResult: ...
    async def norms(self, state: dict[str, Any]) -> EngineResult: ...
    async def metrics(self, state: dict[str, Any]) -> EngineResult: ...
    async def issues(self, state: dict[str, Any]) -> EngineResult: ...
    def risks(self, state: dict[str, Any]) -> list[dict[str, Any]]: ...
    def report_sections(self, state: dict[str, Any]) -> list[dict[str, Any]]: ...
    def fallback_location(self, ref: DrawingRef, state: dict[str, Any]) -> dict[str, Any]: ...


def build_plan_steps(
    file_type: str, available_tools: Iterable[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """按图纸类型生成 7 个规范子任务，并校验工具名是否在注册表里。

    返回 (steps, missing_tools)。`missing_tools` 非空时调用方必须让规划失败，
    不能"跑到运行期才发现工具不存在"（prd-06 §11.2 验收项）。
    """
    parse_tool = PARSE_TOOL_BY_TYPE.get(file_type, "parse_dxf")
    available = set(available_tools)
    steps: list[dict[str, Any]] = []
    missing: list[str] = []
    for index, (step_id, name, tool_tpl) in enumerate(PLAN_TEMPLATE):
        tool = tool_tpl.format(parse_tool=parse_tool) if "{" in tool_tpl else tool_tpl
        if tool in SANDBOX_STEP_TOOLS and tool not in available:
            missing.append(tool)
        steps.append(
            {
                "step_id": step_id,
                "name": name,
                "tool": tool,
                "status": "pending",
                "args_hint": _args_hint(tool),
                "depends_on": [] if index == 0 else [PLAN_TEMPLATE[index - 1][0]],
                "parallel_group": None,
                "expected_artifact": _artifact(tool),
                "required": True,
            }
        )
    return steps, missing


def _args_hint(tool: str) -> dict[str, Any]:
    if tool == "parse_dxf":
        return {"layers_whitelist": ["WALL", "DOOR", "WINDOW", "COLUMN"]}
    if tool == "extract_pdf":
        return {"page_range": None}
    if tool == "calc_metrics":
        return {"metric_keys": ["FAR", "GREEN_RATE", "FIRE_ZONE_AREA", "EGRESS_WIDTH"]}
    return {}


def _artifact(tool: str) -> str | None:
    return {
        "parse_dxf": "drawing_parse",
        "extract_pdf": "drawing_parse",
        "query_norm": "norm_hits",
        "calc_metrics": "metrics",
        "validate_location": "issues",
    }.get(tool)


def call_id_for(thread_id: str, tag: str) -> str:
    """确定性 call_id：节点重放（HITL resume）时必须拿到同一个 ref，否则护栏会误报。"""
    return f"call_{uuid.uuid5(uuid.NAMESPACE_URL, f'{thread_id}:{tag}').hex[:8]}"


def make_call_record(
    *,
    tool: str,
    call_id: str,
    fields: dict[str, Any],
    args_summary: str = "",
    result_summary: str = "",
    duration_ms: int = 0,
    ok: bool = True,
    sandbox: str = "inline-thread",
) -> dict[str, Any]:
    return {
        "call_id": call_id,
        "tool": tool,
        "ok": ok,
        "duration_ms": duration_ms,
        "fields": dict(fields),
        "args_summary": args_summary,
        "result_summary": result_summary,
        "code": None,
        "sandbox": sandbox,
    }


# ---------------------------------------------------------------------------
# Mock 引擎适配器：把确定性 Mock 包装成与 real 同一套接口
# ---------------------------------------------------------------------------
class MockEngine:
    """`REVIEW_ENGINE=mock`：数据全部来自 `mock_engine`（checksum 派生，可复现）。

    Mock 的工具调用记录是**结构性**的：`fields` 与 `fact_ref` 一一对应，
    目的是让前端契约、护栏、报告模板在 Mock 下走同一条代码路径；
    数值本身由 checksum 派生，因此报告"数据来源"章节会明确标注 `engine=mock`。
    """

    name = "mock"

    async def plan(self, state: dict[str, Any]) -> EngineResult:
        file_type = _file_type(state)
        steps, missing = build_plan_steps(file_type, SANDBOX_STEP_TOOLS | NODE_LEVEL_TOOLS)
        if missing:
            raise EngineToolError("PLAN_INVALID_TOOL", f"规划引用了未注册工具：{missing}")
        return EngineResult(data={"steps": steps, "rationale": RATIONALE.get(file_type, "")})

    async def parse(self, ref: DrawingRef, state: dict[str, Any]) -> EngineResult:
        tool = PARSE_TOOL_BY_TYPE.get(ref.file_type, "parse_dxf")
        call_id = call_id_for(str(state.get("thread_id") or ""), f"parse:{ref.drawing_id}")
        parsed = mock_engine.mock_parse(ref)
        summary = parsed.get("entities_summary") or {}
        fields = (
            {"entities": summary.get("entities"), "texts": summary.get("texts"),
             "layers": len(parsed.get("layers") or [])}
            if tool == "parse_dxf"
            else {"page_count": parsed.get("page_count"), "texts": summary.get("texts")}
        )
        parsed["fact_ref"] = attach_fact_ref(
            tool, call_id, "entities" if tool == "parse_dxf" else "page_count"
        )
        parsed.setdefault("coverage", {})
        parsed.setdefault("geometry", {})
        return EngineResult(
            data=parsed,
            tool_calls=[
                make_call_record(
                    tool=tool,
                    call_id=call_id,
                    fields=fields,
                    args_summary=f"file_type={ref.file_type},version={ref.version}",
                    result_summary=f"{parsed['page_count']} 页 / 实体 {summary.get('entities')} 个",
                )
            ],
        )

    async def norms(self, state: dict[str, Any]) -> EngineResult:
        ref = _drawing_ref(state)
        hits = mock_engine.mock_norms(ref)
        return EngineResult(
            data=hits,
            tool_calls=[
                make_call_record(
                    tool="query_norm",
                    call_id=call_id_for(str(state.get("thread_id") or ""), "norm"),
                    fields={
                        "hits": len(hits),
                        # 与真实引擎同口径：条文原文里的数字也算工具返回的事实
                        "clause_text": "；".join(
                            str(h.get("clause_text") or "") for h in hits
                        )[:4000],
                    },
                    args_summary="query=<mock corpus>",
                    result_summary=f"命中 {len(hits)} 条条文",
                )
            ],
        )

    async def metrics(self, state: dict[str, Any]) -> EngineResult:
        ref = _drawing_ref(state)
        call_id = call_id_for(str(state.get("thread_id") or ""), "metrics")
        rows = mock_engine.mock_metrics(ref, call_id)
        fields: dict[str, Any] = {}
        for row in rows:
            fields[row["metric_key"]] = row["value"]
            fields[f"{row['metric_key']}.limit"] = row["limit"]
            if row.get("limit"):
                row["deviation_pct"] = round(
                    abs(float(row["value"]) - float(row["limit"])) / float(row["limit"]) * 100, 2
                )
                fields[f"{row['metric_key']}.deviation_pct"] = row["deviation_pct"]
            row.setdefault("status", "OK")
        return EngineResult(
            data=rows,
            tool_calls=[
                make_call_record(
                    tool="calc_metrics",
                    call_id=call_id,
                    fields=fields,
                    args_summary=f"metric_keys={[r['metric_key'] for r in rows]}",
                    result_summary=f"{len(rows)} 项指标（Mock）",
                )
            ],
        )

    async def issues(self, state: dict[str, Any]) -> EngineResult:
        ref = _drawing_ref(state)
        call_id = call_id_for(str(state.get("thread_id") or ""), "issues")
        drafts = mock_engine.generate_mock_issues(
            drawing=ref, prefs=state.get("user_prefs") or {}, fail_rate=0.0
        )
        for draft in drafts:
            draft["fact_ref"] = attach_fact_ref("mock_source", call_id, "issues")
        return EngineResult(
            data=drafts,
            tool_calls=[
                make_call_record(
                    tool="mock_source",
                    call_id=call_id,
                    fields={"issues": len(drafts)},
                    args_summary=f"checksum={ref.checksum[:12]}",
                    result_summary=f"由 checksum 派生 {len(drafts)} 条问题（Mock）",
                )
            ],
        )

    def risks(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        metrics = state.get("metrics") or []
        ref = _drawing_ref(state)
        return mock_engine.mock_risks(metrics, ref)

    def report_sections(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        return build_report_sections(
            brief=state.get("project_brief") or {},
            metrics=state.get("metrics") or [],
            issues=state.get("issues") or [],
            risks=[],
            exempt_risks=state.get("exempt_risks") or [],
            norm_hits=state.get("norm_hits") or [],
            engine=self.name,
            tool_calls=state.get("tool_calls") or [],
            warnings=_parse_warnings(state),
            errors=state.get("errors") or [],
        )

    def fallback_location(self, ref: DrawingRef, state: dict[str, Any]) -> dict[str, Any]:
        return mock_engine.make_fallback_location(ref.checksum, ref)


# ---------------------------------------------------------------------------
# 选择与注册
# ---------------------------------------------------------------------------
_ENGINES: dict[str, ReviewEngine] = {}
_active: ReviewEngine | None = None


def select_engine(name: str | None = None) -> ReviewEngine:
    """按名字取引擎实例（懒加载 real，避免没装 ezdxf 时连 import 都失败）。"""
    from app.core.config import settings

    key = (name or settings.REVIEW_ENGINE or "mock").strip().lower()
    if key not in ("mock", "real"):
        log.warning("未知 REVIEW_ENGINE=%s，回退 mock", key)
        key = "mock"
    engine = _ENGINES.get(key)
    if engine is None:
        if key == "real":
            from app.ai.real_engine import RealEngine

            engine = RealEngine()
        else:
            engine = MockEngine()
        _ENGINES[key] = engine
    return engine


def set_engine(engine: ReviewEngine | str) -> ReviewEngine:
    """由 `build_graph` 调用：图编译时确定本轮运行用哪个引擎。"""
    global _active
    _active = select_engine(engine) if isinstance(engine, str) else engine
    log.info("review engine = %s", _active.name)
    return _active


def get_engine() -> ReviewEngine:
    return _active or select_engine()


def _parse_warnings(state: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for parsed in state.get("parsed") or []:
        warnings.extend(parsed.get("warnings") or [])
    return warnings


def _file_type(state: dict[str, Any]) -> str:
    drawings = state.get("drawings") or []
    return str((drawings[0] if drawings else {}).get("file_type") or "pdf")


def _drawing_ref(state: dict[str, Any]) -> DrawingRef:
    drawings = state.get("drawings") or []
    return DrawingRef(**(drawings[0] if drawings else {"drawing_id": ""}))


__all__ = [
    "EngineResult",
    "EngineToolError",
    "MockEngine",
    "ReviewEngine",
    "build_plan_steps",
    "build_report_sections",
    "call_id_for",
    "get_engine",
    "make_call_record",
    "select_engine",
    "set_engine",
]
