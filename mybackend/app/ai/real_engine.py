"""真实引擎（`REVIEW_ENGINE=real`）：用真实解析工具替换 Mock 的假数据。

与 `mock_engine` 的差别只有"数据从哪来"，接口完全一致（见 `app/ai/engine.py`）。
数据来源分三类，每一类都必须能指回一次工具调用：

| 数据 | 来源工具 | 出处形式 |
| :--- | :--- | :--- |
| extents / 图层 / 实体量 / 图签 | `parse_dxf`、`extract_pdf` | `parse_dxf#call_x#entities` |
| 容积率 / 绿地率 / 防火分区 / 疏散净宽 | `calc_metrics` | `calc_metrics#call_y#FAR` |
| 问题坐标合法性 | `validate_location` | `validate_location#call_z#invalid` |
| 审查依据条文 | `query_norm` | 文本（条文不做数值校验） |

**文案纪律（护栏反向约束写法）**：问题的 title/description 里不许出现
"工具没返回过"的数字。所以这里宁可写"缺少 WINDOW、COLUMN 图层"，
也不写"4 层只命中 2 层"——后者里的 4 是不在任何工具返回值里的数字，
数值护栏会把它判成未溯源，问题置信度直接掉到 50，等于自己制造假警报。
`calc_metrics` 的 `basis`（计算口径）只进报告指标表，不进问题正文：
指标表由模板渲染，天然带 `fact_ref`（prd-06 §7.1 第 3 条）。
"""
from __future__ import annotations

import logging
from typing import Any

from app.ai.engine import (
    PARSE_TOOL_BY_TYPE,
    RATIONALE,
    EngineResult,
    EngineToolError,
    build_plan_steps,
    call_id_for,
)
from app.ai.guard import attach_fact_ref
from app.ai.report_build import build_report_sections
from app.ai.state import DrawingRef, IssueDraft, MetricResult, NormHit, RiskItem
from app.core.config import settings
from app.sandbox.registry import ToolRegistry, get_registry
from app.sandbox.tools import TITLE_BLOCK_REQUIRED, parse_brief_numbers

log = logging.getLogger(__name__)

# 指标 → (专业, 优先定位图层)。定位到"构件"而不是"整图中心"，
# 前端点开问题能直接落到那段墙上，这是真实引擎相对 Mock 最直观的收益。
METRIC_META: dict[str, tuple[str, str | None]] = {
    "FAR": ("ARCH", "WALL"),
    "GREEN_RATE": ("ARCH", "WALL"),
    "FIRE_ZONE_AREA": ("ARCH", "WALL"),
    "EGRESS_WIDTH": ("ARCH", "DOOR"),
    "LAYER_COVERAGE": ("ARCH", "WALL"),
    "TITLE_BLOCK_COMPLETENESS": ("GENERAL", None),
    "TEXT_LAYER_RATIO": ("GENERAL", None),
}

METRIC_TITLES = {
    "FAR": "容积率超出规划条件",
    "GREEN_RATE": "绿地率低于规划条件要求",
    "FIRE_ZONE_AREA": "防火分区面积超出规范限值",
    "EGRESS_WIDTH": "疏散楼梯净宽不足",
    "LAYER_COVERAGE": "图纸图层不完整，几何审查覆盖不足",
    "TITLE_BLOCK_COMPLETENESS": "图签信息不完整",
    "TEXT_LAYER_RATIO": "PDF 存在无文本层页面，无法机审",
}

DXF_METRICS = [
    "FAR",
    "GREEN_RATE",
    "FIRE_ZONE_AREA",
    "EGRESS_WIDTH",
    "LAYER_COVERAGE",
    "TITLE_BLOCK_COMPLETENESS",
]
PDF_METRICS = [
    "FAR",
    "GREEN_RATE",
    "FIRE_ZONE_AREA",
    "EGRESS_WIDTH",
    "TITLE_BLOCK_COMPLETENESS",
    "TEXT_LAYER_RATIO",
]

DEFAULT_PDF_PAGE = {"w": 2384.0, "h": 1684.0}  # A1，拿不到真实页幅时的兜底
DEFAULT_DXF_EXTENTS = {"x": 0.0, "y": 0.0, "w": 841000.0, "h": 594000.0}

RISK_CRITICAL_RATIO = 0.08  # 与 mock_engine 同口径，切引擎不改变风险分级体验
# 完整性问题（图签/图层/文本层）不构成消防或结构级阻断：一律 HIGH，
# 但要人确认——"缺图层"意味着本轮审查根本没覆盖到那些构件，必须让设计师知道。
COMPLETENESS_METRICS = {"TITLE_BLOCK_COMPLETENESS", "LAYER_COVERAGE", "TEXT_LAYER_RATIO"}


def _ref(state: dict[str, Any]) -> DrawingRef:
    drawings = state.get("drawings") or []
    return DrawingRef(**(drawings[0] if drawings else {"drawing_id": ""}))


def _parsed(state: dict[str, Any]) -> dict[str, Any]:
    parsed = state.get("parsed") or []
    return parsed[0] if parsed else {}


def _thread(state: dict[str, Any]) -> str:
    return str(state.get("thread_id") or "")


def _overrun(metric: dict[str, Any]) -> float:
    limit = float(metric.get("limit") or 0.0)
    if limit <= 0:
        return 0.0
    return abs(float(metric["value"]) - limit) / limit


def _passed(metric: dict[str, Any]) -> bool:
    return bool(metric.get("pass", metric.get("passed", True)))


# ---------------------------------------------------------------------------
# 坐标
# ---------------------------------------------------------------------------
def _dxf_location(parsed: dict[str, Any], layer: str | None) -> dict[str, Any]:
    ext = parsed.get("extents") or DEFAULT_DXF_EXTENTS
    page = {
        "w": float(ext.get("w") or DEFAULT_DXF_EXTENTS["w"]),
        "h": float(ext.get("h") or DEFAULT_DXF_EXTENTS["h"]),
    }
    geometry = (parsed.get("geometry") or {}).get("bboxes") or {}
    boxes = geometry.get((layer or "").upper()) or [
        box for value in geometry.values() for box in value
    ]
    if boxes:
        x, y, w, h = (float(v) for v in boxes[0][:4])
        bbox = {
            "x": round(x - float(ext.get("x") or 0.0), 2),
            "y": round(y - float(ext.get("y") or 0.0), 2),
            "w": round(max(w, 1.0), 2),
            "h": round(max(h, 1.0), 2),
        }
    else:
        bbox = {"x": round(page["w"] * 0.25, 2), "y": round(page["h"] * 0.25, 2),
                "w": round(min(page["w"] * 0.2, 3000.0), 2),
                "h": round(min(page["h"] * 0.2, 3000.0), 2)}
    return {
        "file_type": "dxf",
        "page_no": 1,
        "bbox": bbox,
        "unit": "mm",
        "coord_system": "dxf_wcs",
        "origin": "bottom-left",
        "page_size": page,
        "dxf": {"layout": "Model", "entities": [], "layers": [layer] if layer else []},
    }


def _pdf_location(parsed: dict[str, Any], page_no: int = 1) -> dict[str, Any]:
    pages = parsed.get("pages") or []
    index = min(max(page_no - 1, 0), max(len(pages) - 1, 0))
    size = pages[index] if pages else DEFAULT_PDF_PAGE
    page = {"w": float(size.get("w") or DEFAULT_PDF_PAGE["w"]),
            "h": float(size.get("h") or DEFAULT_PDF_PAGE["h"])}
    boxes = (parsed.get("geometry") or {}).get("bboxes") or {}
    candidates = boxes.get("TEXT") or []
    if candidates:
        x, y, w, h = (float(v) for v in candidates[min(index, len(candidates) - 1)][:4])
        bbox = {"x": round(x, 2), "y": round(y, 2),
                "w": round(max(w, 1.0), 2), "h": round(max(h, 1.0), 2)}
    else:
        bbox = {"x": round(page["w"] * 0.2, 2), "y": round(page["h"] * 0.2, 2),
                "w": round(page["w"] * 0.2, 2), "h": round(page["h"] * 0.15, 2)}
    return {
        "file_type": "pdf",
        "page_no": index + 1,
        "bbox": bbox,
        "unit": "pt",
        "coord_system": "pdf_user_space",
        "origin": "top-left",
        "page_size": page,
        "dxf": None,
    }


def _location(ref: DrawingRef, parsed: dict[str, Any], layer: str | None = None) -> dict[str, Any]:
    if ref.file_type == "dxf":
        return _dxf_location(parsed, layer)
    return _pdf_location(parsed)


# ---------------------------------------------------------------------------
# 事实提取 + 规则库（每条规则都必须建立在"解析出来的事实"之上）
# ---------------------------------------------------------------------------
def _ref_parts(ref: str | None) -> tuple[str, str]:
    """把 `tool#call#field` 拆成前两段，换字段时不用重新拼 call_id。"""
    from app.ai.guard import parse_fact_ref

    parts = parse_fact_ref(ref)
    return (parts[0], parts[1]) if parts else ("", "")


def _facts(parsed: dict[str, Any], file_type: str) -> dict[str, Any]:
    layers = {str(x).upper() for x in (parsed.get("layers") or [])}
    summary = parsed.get("entities_summary") or {}
    geometry = parsed.get("geometry") or {}
    title_block = parsed.get("title_block") or {}
    coverage = parsed.get("coverage") or {}
    return {
        "layers": layers,
        "layer_count": len(layers),
        "entities": int(summary.get("entities") or 0),
        "texts": int(summary.get("texts") or 0),
        "title_fields": len([v for v in title_block.values() if v]),
        "wall_area_m2": geometry.get("wall_closed_area_m2"),
        "text_pages": coverage.get("text_pages"),
        "page_count": int(parsed.get("page_count") or 0),
        "file_type": file_type,
        # 规则描述里用到的可读值，统一在这里格式化，避免 f-string 里到处写 int()
        "fmt": {
            "entities": int(summary.get("entities") or 0),
            "texts": int(summary.get("texts") or 0),
            "layer_count": len(layers),
            "page_count": int(parsed.get("page_count") or 0),
            "text_pages": int(coverage.get("text_pages") or 0),
        },
    }


def _has_layer(keyword: str):
    return lambda f: any(keyword in layer for layer in f["layers"])


def _risk_detail(key: str, parsed: dict[str, Any]) -> str:
    """风险项的具体缺口：只说事实（缺哪个字段/哪一层），不掺任何新数字。"""
    if key == "LAYER_COVERAGE":
        missing = (parsed.get("coverage") or {}).get("missing") or []
        return f"白名单缺失图层：{'、'.join(missing)}" if missing else ""
    if key == "TITLE_BLOCK_COMPLETENESS":
        title_block = parsed.get("title_block") or {}
        present = {k for k, v in title_block.items() if v}
        missing = [k for k in TITLE_BLOCK_REQUIRED if k not in present]
        return f"图签缺少字段：{'、'.join(missing)}" if missing else ""
    if key == "TEXT_LAYER_RATIO":
        coverage = parsed.get("coverage") or {}
        return (f"{coverage.get('page_count')} 页中 {coverage.get('text_pages')} 页可提取文本层")
    return ""


# 规则表：`code` 用规范/出图标准编号，`field` 决定事实来源（可被 fact_ref 引用）
RULE_CHECKS: list[dict[str, Any]] = [
    {
        "code": "STD-DRAWING-2.4",
        "title": "图纸实体数量异常偏少，可能为简图或占位文件",
        "description": "模型空间仅有 {entities} 个实体、{texts} 条文字，"
                       "不足以支撑构件级审查；本轮结论仅覆盖图层与图签层面。",
        "suggestion": "提交完整施工图（含墙、门窗、轴线、标注）后重新预审。",
        "category": "COMPLETENESS", "severity": "MAJOR",
        "discipline": "ARCH", "confidence": 82, "field": "entities",
        "when": lambda f: f["file_type"] == "dxf" and f["entities"] < 50,
    },
    {
        "code": "STD-DRAWING-2.4",
        "title": "PDF 可提取文本极少，可能为空白页或占位文件",
        "description": "全文件 {page_count} 页仅解析到 {texts} 条文字块，"
                       "不满足机审对文字层的基本要求。",
        "suggestion": "确认是否误传空白/占位 PDF，或重新导出带文字层的图纸。",
        "category": "COMPLETENESS", "severity": "MAJOR",
        "discipline": "GENERAL", "confidence": 82, "field": "texts",
        "when": lambda f: f["file_type"] == "pdf" and 0 < f["texts"] < 20,
    },
    {
        "code": "STD-DRAWING-2.5",
        "title": "DXF 无文字实体，房间名称与门窗编号无从核对",
        "description": "解析到 {texts} 条文字实体；缺文字层会导致名称一致性、"
                       "门窗编号、做法说明类问题全部漏检。",
        "suggestion": "确认文字是否被打散为线条，或改用带文本层的 PDF 提交。",
        "category": "COMPLETENESS", "severity": "MAJOR",
        "discipline": "ARCH", "confidence": 86, "field": "texts",
        "when": lambda f: f["file_type"] == "dxf" and f["texts"] == 0,
    },
    {
        "code": "STD-DRAWING-2.6",
        "title": "未发现墙体闭合轮廓，建筑面积无法核算",
        "description": "WALL 图层未解析到闭合多段线，建筑面积只能依赖项目简介里的手填值，"
                       "容积率类指标的可信度会显著下降。",
        "suggestion": "用闭合多段线绘制外墙轮廓，或直接在项目简介中补充总建筑面积。",
        "category": "COMPLETENESS", "severity": "MAJOR",
        "discipline": "ARCH", "confidence": 80, "field": "wall_area_m2",
        "when": lambda f: f["file_type"] == "dxf" and f["wall_area_m2"] is None,
    },
    {
        "code": "STD-DRAWING-2.7",
        "title": "未发现轴网图层，定位与尺寸核对受限",
        "description": "已解析 {layer_count} 个图层，其中没有轴网（AXIS/轴线）图层；"
                       "问题定位只能落在构件包围盒上，无法引用轴号。",
        "suggestion": "按出图标准设置轴网图层（建议命名 AXIS）。",
        "category": "COMPLETENESS", "severity": "MINOR",
        "discipline": "ARCH", "confidence": 72, "field": "layers",
        "when": lambda f: f["file_type"] == "dxf"
                          and bool(f["layers"])
                          and not _has_layer("AXIS")(f),
    },
    {
        "code": "GB50016-2014-5.5.18",
        "title": "未发现楼梯图层，疏散距离与梯段净宽无法复核",
        "description": "已解析 {layer_count} 个图层，未发现楼梯（STAIR/楼梯）图层，"
                       "疏散类条文只能核对简介里手填的净宽。",
        "suggestion": "提交含楼梯平面/详图的图纸，或在简介中说明疏散设计参数。",
        "category": "COMPLETENESS", "severity": "MINOR",
        "discipline": "ARCH", "confidence": 70, "field": "layers",
        "when": lambda f: f["file_type"] == "dxf" and bool(f["layers"])
                          and not _has_layer("STAIR")(f),
    },
    {
        "code": "STD-DRAWING-3.3",
        "title": "PDF 文本层质量提示",
        "description": "解析告警：{page_count} 页中 {text_pages} 页抽到文本层，"
                       "其余页面按图形处理（OCR 属 V1.5 能力）。",
        "suggestion": "检查 PDF 字体嵌入或重新导出带文本层的版本。",
        "category": "CONSISTENCY", "severity": "INFO",
        "discipline": "GENERAL", "confidence": 70, "field": "text_pages",
        "when": lambda f: f["file_type"] == "pdf"
                          and f["text_pages"] is not None
                          and f["text_pages"] < f["page_count"],
    },
]


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
class RealEngine:
    """真实工具驱动的审查引擎。所有数据都来自注册表里的工具调用。"""

    name = "real"

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or get_registry()

    # ---------------- plan ----------------
    async def plan(self, state: dict[str, Any]) -> EngineResult:
        """规划只做两件事：验证原件可读 + 按图纸类型挑解析工具。

        为什么在规划阶段就 `hash_file`：原件不可读是**最贵**的失败
        （要等解析超时才暴露），一次几十毫秒的哈希能把它提前到 8% 进度；
        顺带比对 DB 里的 checksum，能发现"上传后被替换"的图纸。
        """
        ref = _ref(state)
        call_id = call_id_for(_thread(state), f"hash:{ref.drawing_id}")
        res = await self.registry.call(
            "hash_file", {"storage_key": ref.storage_key}, call_id=call_id
        )
        warnings: list[str] = []
        errors: list[dict[str, Any]] = []
        if not res.ok:
            errors.append(
                {
                    "node": "plan",
                    "code": res.code or "TOOL_ERROR",
                    "message": f"图纸原件不可读：{res.error}",
                    "retriable": True,
                }
            )
        else:
            actual = str(res.data.get("sha256") or "")
            if ref.checksum and actual and actual != ref.checksum:
                warnings.append("图纸原件 SHA256 与上传记录不一致，可能被替换或损坏")
                log.warning("checksum mismatch drawing=%s", ref.drawing_id)

        steps, missing = build_plan_steps(ref.file_type, self.registry.names)
        if missing:
            raise EngineToolError("PLAN_INVALID_TOOL", f"规划引用了未注册工具：{missing}")
        return EngineResult(
            data={"steps": steps, "rationale": RATIONALE.get(ref.file_type, "")},
            tool_calls=[res.to_call_record()],
            warnings=warnings,
            errors=errors,
        )

    # ---------------- parse ----------------
    async def parse(self, ref: DrawingRef, state: dict[str, Any]) -> EngineResult:
        tool = PARSE_TOOL_BY_TYPE.get(ref.file_type, "parse_dxf")
        call_id = call_id_for(_thread(state), f"parse:{ref.drawing_id}")
        args: dict[str, Any] = {"storage_key": ref.storage_key}
        if tool == "parse_dxf":
            args["layers_whitelist"] = settings.dxf_layers

        res = await self.registry.call(tool, args, call_id=call_id)
        if not res.ok:
            raise EngineToolError(res.code or "TOOL_ERROR", res.error or "解析失败")

        parsed = self._to_parse_result(ref, tool, call_id, res.data)
        return EngineResult(
            data=parsed,
            tool_calls=[res.to_call_record()],
            warnings=list(parsed.get("warnings") or []),
        )

    def _to_parse_result(
        self, ref: DrawingRef, tool: str, call_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        summary = {
            key: int(value)
            for key, value in (data.get("entities_summary") or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        fact_field = "entities" if tool == "parse_dxf" else "texts"
        coverage = dict(data.get("coverage") or {})
        coverage["by_layer"] = (data.get("entities_summary") or {}).get("by_layer") or {}
        result = {
            "drawing_id": ref.drawing_id,
            "page_count": int(data.get("page_count") or 0),
            "pages": data.get("pages") or [],
            "title_block": data.get("title_block") or {},
            "layers": data.get("layers") or [],
            "entities_summary": summary,
            "units": data.get("units") or ("mm" if ref.file_type == "dxf" else "pt"),
            "warnings": list(data.get("warnings") or []),
            "extents": data.get("extents"),
            "coverage": coverage,
            "geometry": data.get("geometry") or {},
            "stats": data.get("stats") or {},
            "fact_ref": attach_fact_ref(tool, call_id, fact_field),
        }
        return result

    # ---------------- norms ----------------
    async def norms(self, state: dict[str, Any]) -> EngineResult:
        brief = state.get("project_brief") or {}
        parsed = _parsed(state)
        coverage = parsed.get("coverage") or {}
        query = " ".join(
            filter(
                None,
                [
                    str(brief.get("building_type") or ""),
                    str(brief.get("description") or ""),
                    "图层" if coverage.get("missing") else "",
                    "图签" if not parsed.get("title_block") else "",
                ],
            )
        )
        call_id = call_id_for(_thread(state), "norm")
        res = await self.registry.call(
            "query_norm", {"query": query, "top_k": 8}, call_id=call_id
        )
        raw_hits = (res.data.get("hits") if res.ok else []) or []
        hits = [
            NormHit(
                norm_code=str(hit.get("norm_code") or ""),
                clause_no=str(hit.get("clause_no") or ""),
                clause_text=str(hit.get("clause_text") or ""),
                score=float(hit.get("score") or 0.0),
                category=str(hit.get("category") or "CODE"),
                source=str(hit.get("source") or "FALLBACK"),
            ).model_dump(mode="json")
            for hit in raw_hits
        ]
        return EngineResult(
            data=hits,
            tool_calls=[res.to_call_record()],
            errors=[] if res.ok else [
                {"node": "retrieve_norms", "code": res.code or "TOOL_ERROR",
                 "message": f"规范检索失败，已降级：{res.error}", "retriable": True}
            ],
        )

    # ---------------- metrics ----------------
    async def metrics(self, state: dict[str, Any]) -> EngineResult:
        ref = _ref(state)
        parsed = _parsed(state)
        brief = state.get("project_brief") or {}
        prefs = state.get("user_prefs") or {}

        brief_text = "；".join(
            filter(
                None,
                [brief.get("description"), brief.get("scale"),
                 *(brief.get("key_requirements") or [])],
            )
        )
        numbers = parse_brief_numbers(brief_text)
        egress = numbers.get("egress_width")
        if egress is not None and egress > 20:  # 写成 1050 的按 mm 处理
            egress = egress / 1000.0

        geometry = parsed.get("geometry") or {}
        coverage = parsed.get("coverage") or {}
        limits = prefs.get("risk_thresholds") or {}
        inputs: dict[str, Any] = {
            "land_area": numbers.get("land_area"),
            "total_floor_area": numbers.get("total_floor_area"),
            "green_area": numbers.get("green_area"),
            "green_rate": numbers.get("green_rate"),
            "fire_zone_area": numbers.get("fire_zone_area"),
            "floors": numbers.get("floors"),
            "egress_width_m": egress,
            "footprint_area_m2": geometry.get("wall_closed_area_m2"),
            "door_max_width_mm": geometry.get("door_max_width_mm"),
            "layers_found": parsed.get("layers") or [],
            "layers_whitelist": settings.dxf_layers,
            "title_block": parsed.get("title_block") or {},
            "page_count": parsed.get("page_count") or 0,
            "text_pages": coverage.get("text_pages"),
            "limits": {
                "FAR": limits.get("FAR"),
                "GREEN_RATE": limits.get("GREEN_RATE"),
                "FIRE_ZONE_AREA": limits.get("FIRE_ZONE_AREA"),
                "EGRESS_WIDTH": limits.get("EGRESS_WIDTH"),
            }
            if limits
            else {},
            "limit_sources": {
                "FAR": "规划条件通知书-容积率上限",
                "GREEN_RATE": "GB50180-2018-4.0.2",
                "FIRE_ZONE_AREA": "GB50016-2014-5.3.1",
                "EGRESS_WIDTH": "GB50016-2014-5.5.18",
            },
        }
        # 显式传 None 的上限不进 inputs：让工具走规范默认值，而不是拿 None 去比
        inputs["limits"] = {k: v for k, v in (inputs.get("limits") or {}).items() if v is not None}

        keys = DXF_METRICS if ref.file_type == "dxf" else PDF_METRICS
        call_id = call_id_for(_thread(state), "metrics")
        res = await self.registry.call(
            "calc_metrics", {"metric_keys": keys, "inputs": inputs}, call_id=call_id
        )
        if not res.ok:
            return EngineResult(
                data=[],
                tool_calls=[res.to_call_record()],
                errors=[
                    {"node": "validate_metrics", "code": res.code or "TOOL_ERROR",
                     "message": f"指标计算失败：{res.error}", "retriable": True}
                ],
            )

        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for item in res.data.get("results") or []:
            if item.get("status") != "OK":
                missing = item.get("missing") or [item.get("status")]
                errors.append(
                    {
                        "node": "validate_metrics",
                        "code": str(item.get("status") or "INSUFFICIENT_INPUT"),
                        "message": f"{item.get('label')} 无法核算，缺少：{'、'.join(map(str, missing))}",
                        "retriable": False,
                        "metric_key": item.get("metric_key"),
                    }
                )
                continue
            row = MetricResult(
                metric_key=str(item["metric_key"]),
                label=str(item["label"]),
                value=float(item["value"]),
                unit=str(item["unit"]),
                limit=float(item["limit"]),
                limit_source=str(item["limit_source"]),
                passed=bool(item["passed"]),
                fact_ref=attach_fact_ref("calc_metrics", call_id, str(item["metric_key"])),
                basis=str(item.get("basis") or ""),
                status="OK",
                direction=str(item.get("direction") or "max"),
                derived=bool(item.get("derived")),
                deviation_pct=item.get("deviation_pct"),
            )
            rows.append(row.model_dump(mode="json", by_alias=True))

        return EngineResult(data=rows, tool_calls=[res.to_call_record()], errors=errors)

    # ---------------- issues ----------------
    async def issues(self, state: dict[str, Any]) -> EngineResult:
        ref = _ref(state)
        parsed = _parsed(state)
        drafts: list[dict[str, Any]] = []
        parse_ref = parsed.get("fact_ref")

        # 注意：**指标越界不在这里出问题**。每一个越界指标都会进 `risks`，
        # 由卡点一人工判定"是图纸问题还是已获豁免"后再落成问题（prd-06 §8.1）。
        # 两层都出问题会让同一条越界在清单里出现两遍（实测如此），
        # 设计师看到"疏散净宽不足"和"疏散净宽不足，需人工确认"两条会直接失去信任。

        # 指标算不出来 = 资料不完整，这是问题，不是"跳过"。
        # 三条缺失合并成一条：同因问题不该刷屏。
        missing_metrics: list[str] = []
        for error in state.get("errors") or []:
            if error.get("code") not in ("INSUFFICIENT_INPUT", "UNSUPPORTED_METRIC"):
                continue
            message = str(error.get("message") or "")
            if message and message not in missing_metrics:
                missing_metrics.append(message)
        if missing_metrics:
            drafts.append(
                self._draft(
                    ref=ref, parsed=parsed, category="COMPLETENESS", severity="MAJOR",
                    discipline="GENERAL", layer=None,
                    title="项目资料不完整，部分硬指标无法核算",
                    description="；".join(missing_metrics)
                                + "。请在项目简介中补充用地面积、建筑面积等基础数据后重新预审。",
                    suggestion="补充项目指标（用地面积、总建筑面积、绿地面积、防火分区面积）。",
                    rule_code="STD-BRIEF-1.0", confidence=80,
                    fact_ref=parse_ref,
                )
            )

        # ---- 规则库驱动的问题（每条都以解析事实为前提，没有事实就不出问题）----
        # 缺图层 / 图签不完整 / PDF 无文本层这三类已经由 `calc_metrics` 算成指标、
        # 再由卡点一人工确认后落成问题；这里再出一次会让同一条缺陷在清单里出现两遍。
        # 所以本段只出"指标表达不了、但解析事实明确"的检查项。
        facts = _facts(parsed, ref.file_type)
        tool, call = _ref_parts(parse_ref)

        def _fact(field_name: str) -> str | None:
            return attach_fact_ref(tool, call, field_name) if tool and call else parse_ref

        for rule in RULE_CHECKS:
            if not rule["when"](facts):
                continue
            drafts.append(
                self._draft(
                    ref=ref, parsed=parsed, category=rule["category"],
                    severity=rule["severity"], discipline=rule["discipline"],
                    layer=rule.get("layer"),
                    title=rule["title"],
                    description=rule["description"].format(**facts["fmt"]),
                    suggestion=rule["suggestion"],
                    rule_code=rule["code"],
                    confidence=rule["confidence"],
                    fact_ref=_fact(rule["field"]),
                )
            )

        # 解析告警转 INFO：不阻断，但必须让设计师看见"引擎自己知道哪里有坑"
        for warning in parsed.get("warnings") or []:
            if "无可提取文本层" in warning or "异常码位" in warning:
                drafts.append(
                    self._draft(
                        ref=ref, parsed=parsed, category="CONSISTENCY", severity="INFO",
                        discipline="GENERAL", layer=None,
                        title="图纸文本层质量提示",
                        description=warning,
                        suggestion="检查 PDF 字体嵌入或重新导出。",
                        rule_code="STD-DRAWING-3.3", confidence=70, fact_ref=parse_ref,
                    )
                )
                break

        drafts, location_calls = await self._validate_locations(state, drafts)
        return EngineResult(
            data=drafts,
            tool_calls=location_calls,
            warnings=list(parsed.get("warnings") or []),
        )

    def _draft(
        self,
        *,
        ref: DrawingRef,
        parsed: dict[str, Any],
        category: str,
        severity: str,
        discipline: str,
        layer: str | None,
        title: str,
        description: str,
        suggestion: str,
        rule_code: str,
        confidence: int,
        fact_ref: str | None,
    ) -> dict[str, Any]:
        return IssueDraft(
            category=category,  # type: ignore[arg-type]
            severity=severity,  # type: ignore[arg-type]
            discipline=discipline,  # type: ignore[arg-type]
            title=title[:120],
            description=description,
            suggestion=suggestion,
            rule_code=rule_code or None,
            confidence=confidence,
            location=_location(ref, parsed, layer),
            location_valid=True,
            fact_ref=fact_ref,
        ).model_dump(mode="json")

    async def _validate_locations(
        self, state: dict[str, Any], drafts: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """一次批量调用 `validate_location`：越界裁剪 + 打 valid 标记（prd-07 §4）。"""
        if not drafts:
            return drafts, []
        call_id = call_id_for(_thread(state), "locations")
        payload = [
            {"bbox": draft["location"]["bbox"], "page_size": draft["location"]["page_size"]}
            for draft in drafts
        ]
        res = await self.registry.call(
            "validate_location", {"locations": payload}, call_id=call_id
        )
        if not res.ok:
            log.warning("validate_location failed: %s", res.error)
            return drafts, [res.to_call_record()]
        for draft, checked in zip(drafts, res.data.get("results") or []):
            draft["location_valid"] = bool(checked.get("valid"))
            if checked.get("clamped"):
                draft["location"]["bbox"] = checked["clamped"]
        return drafts, [res.to_call_record()]

    # ---------------- risks ----------------
    def risks(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        ref = _ref(state)
        parsed = _parsed(state)
        risks: list[dict[str, Any]] = []
        for metric in state.get("metrics") or []:
            if _passed(metric):
                continue
            key = str(metric["metric_key"])
            discipline, layer = METRIC_META.get(key, ("ARCH", None))
            ratio = _overrun(metric)
            level = "CRITICAL" if ratio >= RISK_CRITICAL_RATIO else "HIGH"
            if key in COMPLETENESS_METRICS:
                level = "HIGH"
            deviation = metric.get("deviation_pct")
            if deviation is None:  # Mock/历史数据没有这个字段时才回退
                deviation = round(ratio * 100, 1)
            detail = _risk_detail(key, parsed)
            risks.append(
                RiskItem(
                    risk_id=f"r_{key.lower()}",
                    title=f"{METRIC_TITLES.get(key, metric['label'])}，需人工确认",
                    level=level,  # type: ignore[arg-type]
                    metric_key=key,
                    detected_value=float(metric["value"]),
                    threshold=float(metric["limit"]),
                    clause_ref=str(metric.get("limit_source") or ""),
                    why_suspicious=(
                        f"实测 {metric['value']}{metric['unit']}，限值 {metric['limit']}{metric['unit']}，"
                        f"偏差 {deviation}%（出处 {metric.get('fact_ref')}）"
                        + (f"；{detail}" if detail else "")
                    ),
                    drawing_id=ref.drawing_id,
                    location=_location(ref, parsed, layer),
                    fact_ref=metric.get("fact_ref"),
                ).model_dump(mode="json")
            )
        return risks

    # ---------------- report ----------------
    def report_sections(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        warnings: list[str] = []
        for parsed in state.get("parsed") or []:
            warnings.extend(parsed.get("warnings") or [])
        return build_report_sections(
            brief=state.get("project_brief") or {},
            metrics=state.get("metrics") or [],
            issues=state.get("issues") or [],
            risks=[],
            exempt_risks=state.get("exempt_risks") or [],
            norm_hits=state.get("norm_hits") or [],
            engine=self.name,
            tool_calls=state.get("tool_calls") or [],
            warnings=warnings,
            errors=state.get("errors") or [],
        )

    def fallback_location(self, ref: DrawingRef, state: dict[str, Any]) -> dict[str, Any]:
        return _location(ref, _parsed(state), None)
