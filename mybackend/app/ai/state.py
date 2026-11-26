"""LangGraph State 定义（prd-06 §3.1）。

三条铁律（写进 Code Review Checklist，本文件是它们的第一道闸）：
1. State 必须可 JSON 序列化：禁止 datetime / Path / ORM 对象 / bytes。
   所以所有时间一律 ISO 字符串，子结构一律 `model_dump()` 成 dict 再入 State。
2. 单次快照 ≤ 256 KB：`issues` 超过 200 条只留前 200 条并打 `truncated`。
3. 恢复只依赖 checkpoint：节点不得依赖进程内单例；签名固定 `(state) -> dict`。
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_ISSUES_IN_STATE = 200
MAX_STATE_BYTES = 256 * 1024


# ---------------------------------------------------------------------------
# 子结构（prd-06 §3.2）
# ---------------------------------------------------------------------------
class BaseStruct(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class ProjectBrief(BaseStruct):
    name: str
    building_type: str | None = None
    scale: str | None = None
    design_stage: str | None = None
    description: str | None = None
    key_requirements: list[str] = Field(default_factory=list)


class DrawingRef(BaseStruct):
    """只放引用，不放开文件字节。"""

    drawing_id: str
    file_type: Literal["pdf", "dxf"]
    version: int
    checksum: str
    storage_key: str
    page_count: int | None = None
    extents: dict[str, Any] | None = None


class UserPrefs(BaseStruct):
    disciplines: list[str] = Field(default_factory=lambda: ["ARCH", "STRUCT", "MEP"])
    rule_sets: list[str] = Field(default_factory=lambda: ["GB50016", "GB50352"])
    severity_threshold: str = "INFO"


class PlanStep(BaseStruct):
    step_id: str
    name: str
    tool: str
    status: Literal["pending", "running", "done", "failed"] = "pending"
    args_hint: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    parallel_group: str | None = None
    expected_artifact: str | None = None
    required: bool = True


class ParseResult(BaseStruct):
    drawing_id: str
    page_count: int
    pages: list[dict[str, float]] = Field(default_factory=list)
    title_block: dict[str, Any] = Field(default_factory=dict)
    layers: list[str] = Field(default_factory=list)
    entities_summary: dict[str, int] = Field(default_factory=dict)
    units: str = "pt"
    warnings: list[str] = Field(default_factory=list)
    # 真实引擎补充：extents/图层覆盖/几何采样（有硬上限，不会撑爆 State）
    extents: dict[str, Any] | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    geometry: dict[str, Any] = Field(default_factory=dict)
    stats: dict[str, Any] = Field(default_factory=dict)
    fact_ref: str | None = None


class NormHit(BaseStruct):
    norm_code: str
    clause_no: str
    clause_text: str
    score: float
    source_url: str | None = None
    category: str = "CODE"
    source: str = "VECTOR"


class MetricResult(BaseStruct):
    metric_key: str
    label: str
    value: float
    unit: str
    limit: float
    limit_source: str
    # `pass` 是 Python 关键字，内部用 passed，序列化时仍写回 "pass"（对齐 prd-06 §3.2）
    passed: bool = Field(alias="pass")
    fact_ref: str | None = None
    # 计算口径：报告与排障都要能回答"这个数是怎么来的"
    basis: str | None = None
    status: str = "OK"
    direction: str = "max"
    derived: bool = False
    # 偏差百分比由 calc_metrics 计算（不是节点自己算的），否则它会成为"未溯源数字"
    deviation_pct: float | None = None

    @model_validator(mode="after")
    def _fact_ref_required(self) -> "MetricResult":
        """prd-06 §7.4 N1：算出数值却没有工具出处 → 模型层直接拒收。

        为什么放在 Pydantic 而不是护栏里：护栏是"事后降级"，模型校验是
        "根本进不来"。指标一旦没有出处，报告里那一行就是不可追溯的假结论。
        """
        if self.status == "OK" and not self.fact_ref:
            raise ValueError("MetricResult.fact_ref 必填：数值必须有工具出处（prd-06 §7.4 N1）")
        return self


class IssueDraft(BaseStruct):
    category: Literal["CODE", "CONSISTENCY", "CLASH", "COMPLETENESS"]
    severity: Literal["BLOCKER", "MAJOR", "MINOR", "INFO"]
    discipline: Literal["ARCH", "STRUCT", "MEP", "GENERAL"]
    title: str
    description: str | None = None
    suggestion: str | None = None
    rule_code: str | None = None
    confidence: int = Field(ge=0, le=100)
    location: dict[str, Any]
    location_valid: bool = True
    fact_ref: str | None = None
    numeric_verified: bool = True


class RiskItem(BaseStruct):
    risk_id: str
    title: str
    level: Literal["HIGH", "CRITICAL"]
    metric_key: str | None = None
    detected_value: float | None = None
    threshold: float | None = None
    clause_ref: str | None = None
    why_suspicious: str | None = None
    drawing_id: str | None = None
    location: dict[str, Any] | None = None
    # 数值护栏：风险里的 detected_value 同样必须能溯源（prd-06 §7.2）
    fact_ref: str | None = None


class RiskDecision(BaseStruct):
    risk_id: str
    decision: Literal["CONFIRM_AS_ISSUE", "MARK_EXEMPT"]
    remark: str | None = None


class InterruptPayload(BaseStruct):
    interrupt_id: str
    type: Literal["RISK_CONFIRM", "REPORT_CONFIRM", "SCHOOL_REVIEW"]
    title: str
    reason: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    expected_action: list[str] = Field(default_factory=list)
    created_at: str | None = None
    expires_at: str | None = None


class HitlRecord(BaseStruct):
    interrupt_id: str
    operator_id: str | None = None
    operator_name: str | None = None
    resolution: str
    remark: str | None = None
    item_decisions: list[dict[str, Any]] = Field(default_factory=list)
    resolved_at: str = ""


class ReportSection(BaseStruct):
    key: str
    title: str
    body_md: str
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    issue_refs: list[str] = Field(default_factory=list)


class ReportDraftModel(BaseStruct):
    report_id: str
    version: int
    sections: list[dict[str, Any]] = Field(default_factory=list)
    issue_summary: dict[str, Any] = Field(default_factory=dict)
    risk_decisions: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: str = ""


class PackageRef(BaseStruct):
    package_id: str
    uri: str
    kind: str = "review_package"
    size: int = 0


class StepError(BaseStruct):
    node: str
    code: str
    message: str
    retriable: bool = False
    at: str = ""


class Budget(BaseStruct):
    max_steps: int = 40
    max_tool_calls: int = 60
    max_seconds: int = 900
    used_steps: int = 0
    used_tool_calls: int = 0
    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------------------
# 主 State（prd-06 §3.1，逐字段对齐）
# ---------------------------------------------------------------------------
class ArchReviewState(TypedDict, total=False):
    # ========== 身份与运行控制 ==========
    thread_id: str  # 等于 review_task.id，checkpoint 的主键维度
    run_id: str
    project_id: str
    drawing_id: str
    review_mode: Literal["PROFESSIONAL", "SCHOOL"]
    status: str  # RunStatus
    current_node: str
    step_seq: int

    # ========== 输入（运行期间只读） ==========
    project_brief: dict[str, Any]
    drawings: list[dict[str, Any]]
    user_prefs: dict[str, Any]

    # ========== 规划与执行 ==========
    plan: list[dict[str, Any]]
    plan_revision: int
    artifacts: dict[str, dict[str, Any]]
    parsed: list[dict[str, Any]]
    norm_hits: list[dict[str, Any]]

    # ========== 业务产物 ==========
    metrics: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    risks: list[dict[str, Any]]
    human_annotations: list[dict[str, Any]]
    report_draft: dict[str, Any] | None
    package: dict[str, Any] | None
    exempt_risks: list[dict[str, Any]]

    # ========== HITL ==========
    interrupt: dict[str, Any] | None
    hitl_history: list[dict[str, Any]]
    school_action: Literal["APPROVE", "RETURN"] | None

    # ========== 异常与预算 ==========
    errors: list[dict[str, Any]]
    budget: dict[str, Any]
    truncated: bool

    # ========== 工具溯源（数值护栏的事实底座，prd-06 §7） ==========
    tool_calls: list[dict[str, Any]]
    engine: str

    # ========== 消息 ==========
    messages: Annotated[list, add_messages]


# ---------------------------------------------------------------------------
# 工厂与工具
# ---------------------------------------------------------------------------
def initial_state(
    *,
    thread_id: str,
    run_id: str,
    project_id: str,
    drawing_id: str,
    review_mode: str = "PROFESSIONAL",
) -> ArchReviewState:
    return ArchReviewState(
        thread_id=thread_id,
        run_id=run_id,
        project_id=project_id,
        drawing_id=drawing_id,
        review_mode=review_mode,  # type: ignore[typeddict-item]
        status="RUNNING",
        current_node="load_context",
        step_seq=0,
        artifacts={},
        plan=[],
        plan_revision=0,
        parsed=[],
        norm_hits=[],
        metrics=[],
        issues=[],
        risks=[],
        human_annotations=[],
        report_draft=None,
        package=None,
        exempt_risks=[],
        interrupt=None,
        hitl_history=[],
        school_action=None,
        errors=[],
        budget=Budget().model_dump(),
        truncated=False,
        tool_calls=[],
        engine="",
    )


def _jsonable(value: Any) -> Any:
    """把 pydantic 模型 / 枚举压成纯 JSON（State 只接受这些）。"""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return value.value
    return value


def prune_state(state: dict[str, Any]) -> dict[str, Any]:
    """裁剪快照：issues 上限 200、去掉 messages（对话不需要回放）。

    State 单次快照 ≤ 256 KB（prd-06 §3.3 铁律 2）；超限时优先裁 issues 的
    description/suggestion —— 这两个字段是最大的字符串，且完整内容已落库。
    """
    snapshot = {k: _jsonable(v) for k, v in state.items() if k != "messages"}
    issues = list(snapshot.get("issues") or [])
    if len(issues) > MAX_ISSUES_IN_STATE:
        snapshot["issues"] = issues[:MAX_ISSUES_IN_STATE]
        snapshot["truncated"] = True

    if len(canonical_json(snapshot).encode()) <= MAX_STATE_BYTES:
        return snapshot

    for issue in snapshot.get("issues") or []:
        issue["description"] = None
        issue["suggestion"] = None
    return snapshot


def canonical_json(value: Any) -> str:
    """规范化 JSON：排序键 + 紧凑分隔符，保证同一状态永远同一串字节。"""
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def state_digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(prune_state(state)).encode()).hexdigest()


def truncate_issues(issues: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    if len(issues) > MAX_ISSUES_IN_STATE:
        return issues[:MAX_ISSUES_IN_STATE], True
    return issues, False
