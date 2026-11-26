"""报告初稿的模板渲染（prd-06 §7.1 第 3 条：指标表由 `calc_metrics` 结果直接渲染）。

这里的唯一职责是"把已经算好的事实排版成人能读的东西"：

- **指标数字只来自 `metrics`**（每个都带 `fact_ref`），模板里不做任何算术；
- **问题条数只来自 `issues`**，不让"模型总结"有机会改数；
- 第六节"数据来源"把引擎名、工具调用数、未溯源条数摊开写，
  报告一旦落到客户手里，这一节就是"这个结论怎么来的"的答案。
"""
from __future__ import annotations

from typing import Any

SEVERITY_ORDER = {"BLOCKER": 0, "MAJOR": 1, "MINOR": 2, "INFO": 3}


def build_issue_summary(issues: list[dict[str, Any]]) -> dict[str, Any]:
    by_severity = {k: 0 for k in ("BLOCKER", "MAJOR", "MINOR", "INFO")}
    by_discipline: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for issue in issues:
        by_severity[issue["severity"]] = by_severity.get(issue["severity"], 0) + 1
        by_discipline[issue["discipline"]] = by_discipline.get(issue["discipline"], 0) + 1
        by_category[issue["category"]] = by_category.get(issue["category"], 0) + 1
    return {
        "by_severity": by_severity,
        "by_discipline": by_discipline,
        "by_category": by_category,
    }


def _metric_line(metric: dict[str, Any]) -> str:
    passed = metric.get("pass", metric.get("passed"))
    basis = metric.get("basis")
    line = (
        f"- {metric['label']}：{metric['value']}{metric['unit']}"
        f"（限值 {metric['limit']}{metric['unit']}，{'通过' if passed else '不通过'}，"
        f"依据 {metric['limit_source']}）"
    )
    if basis:
        line += f"\n  - 计算口径：{basis}"
    if metric.get("fact_ref"):
        line += f"\n  - 数据出处：`{metric['fact_ref']}`"
    return line


def build_report_sections(
    *,
    brief: dict[str, Any],
    metrics: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    risks: list[dict[str, Any]],
    exempt_risks: list[dict[str, Any]],
    norm_hits: list[dict[str, Any]],
    engine: str = "mock",
    tool_calls: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    summary = build_issue_summary(issues)
    sev = summary["by_severity"]
    metric_lines = "\n".join(_metric_line(m) for m in metrics) or "- 本轮未算出可校验的硬指标"
    norm_lines = "\n".join(
        f"- {h['norm_code']} 第 {h['clause_no']} 条：{h['clause_text']}" for h in norm_hits
    ) or "- 未命中规范条文（检索降级）"
    risk_lines = "\n".join(
        f"- 【已确认】{r.get('title')}：{r.get('why_suspicious')}" for r in risks
    ) or "- 无"
    exempt_lines = "\n".join(
        f"- 【豁免】{r.get('risk', {}).get('title')}：{r.get('remark')}" for r in exempt_risks
    ) or "- 无"

    calls = list(tool_calls or [])
    unverified = [i for i in issues if i.get("numeric_verified") is False]
    source_lines = [
        f"- 审查引擎：{engine}",
        f"- 工具调用：{len(calls)} 次"
        + ("（" + "、".join(sorted({str(c.get('tool')) for c in calls})) + "）" if calls else ""),
        "- 指标出处："
        + ("；".join(f"{m['metric_key']}←`{m.get('fact_ref')}`" for m in metrics) or "无"),
        f"- 未溯源数值：{len(unverified)} 条"
        + ("（已按数值护栏降级为置信度 ≤50）" if unverified else ""),
    ]
    if warnings:
        source_lines.append("- 解析告警：" + "；".join(warnings[:8]))
    if errors:
        source_lines.append(
            "- 降级/异常：" + "；".join(f"{e.get('code')}:{e.get('message')}" for e in errors[:8])
        )

    return [
        {
            "key": "overview",
            "title": "1. 项目概况",
            "body_md": f"项目：{brief.get('name')}；业态：{brief.get('building_type') or '未填写'}；"
            f"规模：{brief.get('scale') or '未填写'}；设计阶段：{brief.get('design_stage') or '未填写'}。",
            "metrics": [],
            "issue_refs": [],
        },
        {
            "key": "basis",
            "title": "2. 审查依据",
            "body_md": norm_lines,
            "metrics": [],
            "issue_refs": [],
        },
        {
            "key": "metrics",
            "title": "3. 硬指标校验",
            "body_md": metric_lines,
            "metrics": metrics,
            "issue_refs": [m.get("fact_ref") for m in metrics if m.get("fact_ref")],
        },
        {
            "key": "issues",
            "title": "4. 问题与建议",
            "body_md": f"本轮共检出 {len(issues)} 条问题：严重 {sev['BLOCKER']}、主要 {sev['MAJOR']}、"
            f"一般 {sev['MINOR']}、提示 {sev['INFO']}。明细见问题清单。",
            "metrics": [],
            "issue_refs": [i.get("fact_ref") for i in issues if i.get("fact_ref")],
        },
        {
            "key": "risk",
            "title": "5. 风险处置",
            "body_md": f"需人工确认：\n{risk_lines}\n\n豁免说明：\n{exempt_lines}",
            "metrics": [],
            "issue_refs": [],
        },
        {
            "key": "source",
            "title": "6. 数据来源与可溯源性",
            "body_md": "\n".join(source_lines),
            "metrics": [],
            "issue_refs": [],
        },
    ]
