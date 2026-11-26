"""数值护栏（prd-06 §7）：任何数字都必须能指回一次真实的工具调用。

三个层次，**V1.0 全做**：

1. `attach_fact_ref` —— ref 只由工具返回值生成，引擎/节点不许手写（§7.3 照抄）。
2. **存在性校验** `verify_fact_refs` —— `fact_ref` 必须能在本次运行的 `tool_calls`
   里找到（O(1) 查表）；找不到 → 置信度压到 ≤50 + 追加"该数值未经工具核对"。
3. **文本数值比对** `scan_text_numbers` —— 结构外的自由文本里的数字
   （"容积率约 3.2"）必须与本次工具返回的某个数值对得上（±2% 容差）。
   这一条对应 §7.2 的 V1.5 `FULL_SCAN`，但本项目的解读文字由模板渲染，
   先把机制建起来并默认在 real 引擎启用，mock 引擎保持 `FIELD_ONLY`。

为什么不做"全文正则一拦就阻断"：`3 层`、`2 处`、`GB50016-2014-5.5.18` 全会被误伤，
误伤的代价是设计师看到一条"数值未溯源"的假问题——比漏拦更伤信任。
所以文本扫描只认**带单位或带指标关键词**的数字，命中即降级而不是阻断。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

FACT_REF_SEP = "#"
UNVERIFIED_SUFFIX = "（该数值未经工具核对）"
CONFIDENCE_CAP = 50

# 带单位 / 带指标关键词的数字才算"数值断言"；纯计数与条文编号一律放过
_UNIT_NUM_RE = re.compile(
    r"(?<![\w.\-])(\d+(?:\.\d+)?)\s*(m|mm|cm|㎡|m²|m2|平方米|米|%)"
)
_KEYWORD_NUM_RE = re.compile(
    r"(容积率|绿地率|净宽|疏散距离|防火分区面积|建筑面积|用地面积|建筑高度)"
    r"[^\d]{0,6}(\d+(?:\.\d+)?)"
)
_IGNORED_CONTEXT = ("第", "条", "GB", "JGJ", "DBJ", "轴")


def attach_fact_ref(tool: str, call_id: str, field: str) -> str:
    """工具返回时立刻生成 `fact_ref`（形如 `calc_metrics#call_8f21#FAR`）。"""
    if not tool or not call_id or not field:
        raise ValueError("fact_ref 三段缺一不可：tool#call_id#field")
    return FACT_REF_SEP.join((tool, call_id, field))


def parse_fact_ref(ref: str | None) -> tuple[str, str, str] | None:
    if not ref or ref.count(FACT_REF_SEP) < 2:
        return None
    tool, call_id, field = ref.split(FACT_REF_SEP, 2)
    if not (tool and call_id and field):
        return None
    return tool, call_id, field


def known_fact_refs(tool_calls: Iterable[dict[str, Any]] | None) -> set[str]:
    """本次运行可用的 ref 全集：只认 `ok=True` 的调用（失败调用不得支撑数值）。"""
    refs: set[str] = set()
    for call in tool_calls or []:
        if not call.get("ok"):
            continue
        tool = str(call.get("tool") or "")
        call_id = str(call.get("call_id") or "")
        for field_name in (call.get("fields") or {}):
            try:
                refs.add(attach_fact_ref(tool, call_id, str(field_name)))
            except ValueError:
                continue
    return refs


def known_values(tool_calls: Iterable[dict[str, Any]] | None) -> list[tuple[float, str]]:
    """本次运行工具返回过的所有数值 + 出处，用于文本数值比对。

    包含两类：
    1. 标量字段（`calc_metrics#call#FAR = 3.42`）；
    2. **工具返回的文本里出现的数字**（规范条文原文、告警文案）。
       报告引用条文原文时必须能溯源到 `query_norm` 这次调用，
       否则"引用规范"会被误判成"编数字"——这类误伤比漏拦更伤信任。
    """
    out: list[tuple[float, str]] = []
    for call in tool_calls or []:
        if not call.get("ok"):
            continue
        for name, value in (call.get("fields") or {}).items():
            ref = f"{call.get('tool')}#{call.get('call_id')}#{name}"
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                out.append((float(value), ref))
            elif isinstance(value, str):
                for token in re.findall(r"\d+(?:\.\d+)?", value):
                    out.append((float(token), ref))
    return out


def _value_supported(token: str, candidates: list[tuple[float, str]]) -> bool:
    """数值断言能否被工具返回值解释：**按显示精度比对**。

    为什么不用"±2% 容差"：百分比类指标上 2% 太松 —— "99.1%" 会被
    100.0 的限值当成"差不多"而放过，护栏等于失效。改成"同精度四舍五入相等"：

        show("3.5", value=3.456)  → round(3.456, 1) == 3.5   ✅ 展示精度一致
        show("99.1", value=100.0) → round(100.0, 1) == 100.0 ❌ 不是同一个数
        show("3.2", value=3.42)   → round(3.42, 1) == 3.4    ❌ 编的数字

    额外放一个 1e-9 的绝对容差，吸收浮点表示误差（0.1+0.2 那一类）。
    """
    try:
        value = float(token)
    except ValueError:
        return False
    decimals = len(token.split(".", 1)[1]) if "." in token else 0
    for candidate, _ref in candidates:
        if abs(candidate - value) <= 1e-9:
            return True
        if round(candidate, decimals) == round(value, decimals):
            return True
    return False


def scan_text_numbers(
    text: str, candidates: list[tuple[float, str]]
) -> list[dict[str, Any]]:
    """扫出文本里"没工具来源"的数值断言，返回违规明细（空列表 = 干净）。"""
    found: list[dict[str, Any]] = []
    for match in _UNIT_NUM_RE.finditer(text or ""):
        raw, unit = match.group(1), match.group(2)
        start = max(0, match.start() - 8)
        context = (text or "")[start : match.start()]
        if any(token in context for token in _IGNORED_CONTEXT):
            # 上下文是"第 3 条/GB50016-2014"这类引用，不是数值断言
            continue
        if not _value_supported(raw, candidates):
            found.append({"token": f"{raw}{unit}", "reason": "UNIT_NUMBER_UNVERIFIED"})
    for match in _KEYWORD_NUM_RE.finditer(text or ""):
        raw = match.group(2)
        if not _value_supported(raw, candidates):
            found.append({"token": f"{match.group(1)}={raw}", "reason": "METRIC_CLAIM_UNVERIFIED"})
    return found


def _item_numbers(item: dict[str, Any], text_fields: Iterable[str]) -> list[str]:
    """条目里出现过的数字（字段级判定，不需要上下文语义）。"""
    numbers: list[str] = []
    for key in text_fields:
        value = item.get(key)
        if not isinstance(value, str):
            continue
        numbers.extend(re.findall(r"\d+(?:\.\d+)?", value))
    return numbers


def verify_fact_refs(
    items: list[dict[str, Any]],
    tool_calls: Iterable[dict[str, Any]] | None,
    *,
    text_fields: tuple[str, ...] = ("title", "description", "suggestion"),
    scan_text: bool = False,
    label: str = "items",
    cap_key: str | None = "confidence",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (降级后的条目, 违规明细)。空违规列表 = 全部可溯源。

    - 字段级：条目靠 `fact_ref` 支撑，ref 不存在/格式错 → 降级；
    - 文本级（`scan_text=True`）：正文里带单位的数字要能在工具返回值里找到；
    - `cap_key`：`IssueDraft` 压 `confidence`，`MetricResult` 没有这个字段，
      传 `None` 只打标不压分，由调用方决定丢弃还是降级（指标不能"降信心"，
      它要么有出处，要么不该出现在报告里）。
    """
    refs = known_fact_refs(tool_calls)
    values = known_values(tool_calls)
    guarded: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []

    for source in items:
        item = dict(source)
        reasons: list[str] = []
        ref = item.get("fact_ref")
        # 数值可能出现在正文里（问题描述），也可能本身就是数值字段（指标 value）
        has_number = bool(_item_numbers(item, text_fields)) or isinstance(
            item.get("value"), (int, float)
        )
        if has_number and (not ref or ref not in refs):
            reasons.append("FACT_REF_MISSING" if not ref else "FACT_REF_UNKNOWN")
        if scan_text:
            text = " ".join(str(item.get(key) or "") for key in text_fields)
            for hit in scan_text_numbers(text, values):
                reasons.append(hit["reason"])

        if reasons:
            if cap_key:
                item[cap_key] = min(int(item.get(cap_key, 0) or 0), CONFIDENCE_CAP)
            for key in ("title", "description"):
                value = item.get(key)
                if isinstance(value, str) and not value.endswith(UNVERIFIED_SUFFIX):
                    item[key] = value + UNVERIFIED_SUFFIX
            item["numeric_verified"] = False
            violations.append(
                {
                    "label": label,
                    "title": item.get("title") or item.get("label") or item.get("metric_key"),
                    "fact_ref": ref or "",
                    "reasons": sorted(set(reasons)),
                }
            )
        else:
            item["numeric_verified"] = True
        guarded.append(item)

    return guarded, violations
