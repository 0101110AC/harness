"""DAG 骨架（prd-06 §4.2 / §12.3）。

    START → load_context → plan → parse_drawings → retrieve_norms
          → validate_metrics → detect_issues → risk_check
          → generate_report → report_confirm → package_assets → END

两个卡点都用节点内 `interrupt()` 实现，**不配置 `interrupt_before`**：
如果静态停在 `risk_check`，没有高风险的图纸也会逼设计师确认一个"没有问题"的弹窗。

引擎切换在 `build_graph()` 里完成：读 `REVIEW_ENGINE`（`mock` / `real`），
把选中的引擎塞进 `app.ai.engine` 的模块级"当前引擎"，节点通过 `get_engine()` 取。
好处是节点签名保持 `(state) -> dict`，LangGraph 不需要知道引擎的存在；
引擎实例是无状态适配器（不缓存任务数据），因此不违反"恢复只依赖 checkpoint"。
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.ai import engine as engine_mod
from app.ai import nodes
from app.ai.state import ArchReviewState
from app.core.config import settings

log = logging.getLogger(__name__)

NODE_SEQUENCE = [
    "load_context",
    "plan",
    "parse_drawings",
    "retrieve_norms",
    "validate_metrics",
    "detect_issues",
    "risk_check",
    "generate_report",
    "report_confirm",
    "package_assets",
]

MAX_PLAN_REVISION = 2


def route_after_report_confirm(state: ArchReviewState) -> str:
    """确认 → 打包；退回 → 回到归纳问题（上限 2 次，超限强制打包并标注未复核）。"""
    history = state.get("hitl_history") or []
    last = history[-1] if history else {}
    if last.get("resolution") != "REJECTED":
        return "package"
    if int(state.get("plan_revision", 0)) > MAX_PLAN_REVISION:
        return "package"
    return "rework"


def build_graph(checkpointer: Any = None, engine_name: str | None = None):
    """编译图，并确定本轮运行使用的引擎（`engine_name` 优先，其次环境变量）。"""
    engine = engine_mod.set_engine(engine_name or settings.REVIEW_ENGINE)
    log.info("build_graph: review engine = %s", engine.name)

    g = StateGraph(ArchReviewState)

    g.add_node("load_context", nodes.load_context)
    g.add_node("plan", nodes.plan)
    g.add_node("parse_drawings", nodes.parse_drawings)
    g.add_node("retrieve_norms", nodes.retrieve_norms)
    g.add_node("validate_metrics", nodes.validate_metrics)
    g.add_node("detect_issues", nodes.detect_issues)
    g.add_node("risk_check", nodes.risk_check)
    g.add_node("generate_report", nodes.generate_report)
    g.add_node("report_confirm", nodes.report_confirm)
    g.add_node("package_assets", nodes.package_assets)

    g.add_edge(START, "load_context")
    g.add_edge("load_context", "plan")
    g.add_edge("plan", "parse_drawings")
    g.add_edge("parse_drawings", "retrieve_norms")
    g.add_edge("retrieve_norms", "validate_metrics")
    g.add_edge("validate_metrics", "detect_issues")
    g.add_edge("detect_issues", "risk_check")
    g.add_edge("risk_check", "generate_report")
    g.add_edge("generate_report", "report_confirm")
    g.add_conditional_edges(
        "report_confirm",
        route_after_report_confirm,
        {"package": "package_assets", "rework": "detect_issues"},
    )
    g.add_edge("package_assets", END)

    return g.compile(checkpointer=checkpointer, interrupt_before=[])
