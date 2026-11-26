r"""确定性 Mock 引擎（prd-06 §11.1.1 / §12.9）。

唯一 seed 来源是 `drawing.checksum`（文件内容的 SHA256）：
**同一份文件 ⇒ 永远同一批指标/风险/问题**（条数、顺序、标题、坐标、置信度全一致）。
不掺时间、不掺用户、不掺 run_id —— 否则前端"定位偏移/条数/顺序"的断言全部失效。

CI 门禁（prd-06 §12.9）：本包内禁止裸 `random.xxx(...)` 调用，
所有随机都必须经过 `_rng(checksum, salt)` 返回的 Random 实例：
    grep -rnE "\brandom\.(random|choice|randint|shuffle|sample)\(" app/ai/
"""
from __future__ import annotations

import random
from typing import Any

from app.ai.guard import attach_fact_ref
from app.ai.state import (
    DrawingRef,
    IssueDraft,
    MetricResult,
    NormHit,
    ParseResult,
    RiskItem,
)

# 严重度 / 专业分布（与 prd-06 §12.9 一致）
MOCK_SEVERITY_MIX = [("BLOCKER", 0.15), ("MAJOR", 0.35), ("MINOR", 0.40), ("INFO", 0.10)]
MOCK_DISCIPLINE_MIX = [("ARCH", 0.40), ("STRUCT", 0.25), ("MEP", 0.25), ("GENERAL", 0.10)]

# 固定语料：标题必须像真人写的，且与 severity / discipline 匹配
TEMPLATES: dict[str, list[str]] = {
    "BLOCKER": [
        "3 层疏散楼梯净宽不足 1.1m",
        "防火分区面积超过规范限值",
        "消防电梯前室未设防烟设施",
        "地下室安全出口数量不足",
        "袋形走道尽端房间疏散距离超 22m",
    ],
    "MAJOR": [
        "强电井与给水立管碰撞",
        "卫生间排水立管穿越卧室",
        "屋面排水坡度不足 2%",
        "门窗开启方向与立面图不符",
        "核心筒剪力墙洞口未设加强边柱",
    ],
    "MINOR": [
        "图签缺少设计阶段标注",
        "轴线编号在 4~5 轴重复",
        "房间名称与装修表不一致",
        "标高标注精度不统一",
        "楼梯详图未标注踏步防滑条做法",
    ],
    "INFO": [
        "建议补充典型墙身节点索引",
        "建议统一图层配色与线宽",
        "建议补充无障碍坡道剖面说明",
    ],
}

DESCRIPTIONS = {
    "BLOCKER": "按图面量取尺寸与规范限值比对，超出允差范围，存在消防验收风险。",
    "MAJOR": "图面各专业信息之间存在冲突，施工阶段会造成返工，需设计方澄清。",
    "MINOR": "图纸表达不完整或前后不一致，影响施工交底效率，不影响结构安全。",
    "INFO": "非强制项，属于图纸表达优化建议。",
}

SUGGESTIONS = {
    "BLOCKER": "按规范调整尺寸/分区后重新出图，并在图纸说明中补充计算依据。",
    "MAJOR": "核对各专业条件图后统一修改，并在会签栏补充确认。",
    "MINOR": "补充标注并对齐全套图纸的同类问题后重新提交。",
    "INFO": "建议在下一版图纸中优化表达。",
}

RULE_CODES = {
    "BLOCKER": ["GB50016-2014-5.5.18", "GB50016-2014-5.3.1", "GB50016-2014-5.5.17"],
    "MAJOR": ["CLASH-MEP-001", "CONSIST-SECTION-ELEV", "GB50011-2010-6.1.14"],
    "MINOR": ["COMPLETE-TITLEBLOCK", "CONSIST-AXIS", "COMPLETE-STAIR-DETAIL"],
    "INFO": ["SUGGEST-NODE-INDEX", "SUGGEST-LAYER-STYLE"],
}

DEFAULT_PDF_PAGE = {"w": 2384.0, "h": 1684.0}  # A1，真实值可由 parse_meta 覆盖
DEFAULT_DXF_EXTENTS = {"x": 0.0, "y": 0.0, "w": 841000.0, "h": 594000.0}

_SALT_METRICS = 0x9E3779B9
_SALT_ISSUES = 0x85EBCA6B
_SALT_PARSE = 0xC2B2AE35
_SALT_NORMS = 0x27D4EB2F
_SALT_FALLBACK_LOC = 0x5EED

# 确定性的"指标是否越界"概率：越界才会触发卡点一（HITL 风险确认）
_FAR_FAIL_P = 0.80
_GREEN_FAIL_P = 0.30
_FIRE_ZONE_FAIL_P = 0.45
_EGRESS_FAIL_P = 0.40


def derive_seed(checksum: str) -> int:
    """唯一 seed 来源：文件内容。不掺时间/用户/run_id。"""
    cleaned = (checksum or "").strip().lower()
    if len(cleaned) >= 8 and all(c in "0123456789abcdef" for c in cleaned[:8]):
        return int(cleaned[:8], 16)
    # 兼容非 hex 的 checksum（历史数据/Mock 测试）：退化为稳定哈希
    return int.from_bytes(cleaned.encode()[:8].ljust(8, b"\0"), "big")


def _rng(checksum: str, salt: int = 0) -> random.Random:
    """每个产物用独立 salt 的 Random：保证一个函数的调用顺序不影响另一个函数。"""
    return random.Random(derive_seed(checksum) ^ salt)


def _pick_weighted(rng: random.Random, mix: list[tuple[str, float]]) -> str:
    r = rng.random()
    acc = 0.0
    for key, weight in mix:
        acc += weight
        if r <= acc:
            return key
    return mix[-1][0]


# ---------------------------------------------------------------------------
# 坐标（prd-07 §4 契约：bbox 必须落在 page_size 内）
# ---------------------------------------------------------------------------
def _page_size(drawing: DrawingRef) -> dict[str, float]:
    if drawing.file_type == "pdf":
        ext = drawing.extents or {}
        width, height = ext.get("w"), ext.get("h")
        if width and height:
            return {"w": float(width), "h": float(height)}
        return dict(DEFAULT_PDF_PAGE)
    ext = drawing.extents or DEFAULT_DXF_EXTENTS
    return {"w": float(ext.get("w", DEFAULT_DXF_EXTENTS["w"])),
            "h": float(ext.get("h", DEFAULT_DXF_EXTENTS["h"]))}


def make_location(rng: random.Random, drawing: DrawingRef) -> dict[str, Any]:
    """生成合法坐标：PDF 落在页面内，DXF 落在 extents 内（prd-07 §4）。"""
    size = _page_size(drawing)
    if drawing.file_type == "pdf":
        page_no = rng.randint(1, max(1, drawing.page_count or 1))
        w = rng.uniform(80, 300)
        h = rng.uniform(40, 200)
        x = rng.uniform(0, max(1.0, size["w"] - w))
        y = rng.uniform(0, max(1.0, size["h"] - h))
        return {
            "file_type": "pdf",
            "page_no": page_no,
            "bbox": {"x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "h": round(h, 2)},
            "unit": "pt",
            "coord_system": "pdf_user_space",
            "origin": "top-left",
            "page_size": {"w": size["w"], "h": size["h"]},
            "dxf": None,
        }

    ext = drawing.extents or DEFAULT_DXF_EXTENTS
    w = rng.uniform(1000, 8000)
    h = rng.uniform(1000, 6000)
    x = float(ext.get("x", 0.0)) + rng.uniform(0, max(1.0, size["w"] - w))
    y = float(ext.get("y", 0.0)) + rng.uniform(0, max(1.0, size["h"] - h))
    return {
        "file_type": "dxf",
        "page_no": 1,
        "bbox": {"x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "h": round(h, 2)},
        "unit": "mm",
        "coord_system": "dxf_wcs",
        "origin": "bottom-left",
        "page_size": {"w": size["w"], "h": size["h"]},
        "dxf": {
            "layout": "Model",
            "entities": [f"{rng.randrange(0x1000, 0xFFFF):X}"],
            "layers": ["WALL"],
        },
    }


def make_fallback_location(checksum: str, drawing: DrawingRef) -> dict[str, Any]:
    """风险项缺失坐标时的兜底：同样由 checksum 决定，保证可复现。"""
    return make_location(_rng(checksum, _SALT_FALLBACK_LOC), drawing)


# ---------------------------------------------------------------------------
# 各节点产物
# ---------------------------------------------------------------------------
def mock_parse(drawing: DrawingRef) -> dict[str, Any]:
    rng = _rng(drawing.checksum, _SALT_PARSE)
    page_count = max(1, drawing.page_count or (1 if drawing.file_type == "dxf" else rng.randint(1, 12)))
    pages = [_page_size(drawing) for _ in range(1 if drawing.file_type == "dxf" else page_count)]
    layers = (
        ["WALL", "DOOR", "WINDOW", "COLUMN", "STAIR", "AXIS"]
        if drawing.file_type == "dxf"
        else []
    )
    warnings: list[str] = []
    if drawing.file_type == "dxf":
        warnings.append("未发现图框层（TITLE），图签信息取自文件名")
    result = ParseResult(
        drawing_id=drawing.drawing_id,
        page_count=page_count,
        pages=pages,
        title_block={
            "drawing_no": f"建施-{rng.randint(1, 30):02d}",
            "title": "标准层平面图" if drawing.file_type != "pdf" else "首层平面图",
            "scale": "1:100",
            "design_stage": "施工图",
        },
        layers=layers,
        entities_summary={
            "layers": len(layers),
            "entities": rng.randint(2600, 5200),
            "texts": rng.randint(120, 480),
        },
        units="mm" if drawing.file_type == "dxf" else "pt",
        warnings=warnings,
    )
    return result.model_dump(mode="json")


def mock_norms(drawing: DrawingRef) -> list[dict[str, Any]]:
    rng = _rng(drawing.checksum, _SALT_NORMS)
    corpus = [
        ("GB50016-2014", "5.5.18", "高层公共建筑内楼梯间、前室的门应采用乙级防火门；疏散楼梯净宽不应小于 1.10m。"),
        ("GB50016-2014", "5.3.1", "未设置自动喷水灭火系统的多层民用建筑，每个防火分区最大允许建筑面积 2500㎡。"),
        ("GB50016-2014", "5.5.17", "公共建筑内房间疏散门至最近安全出口的直线距离不应大于 40m，袋形走道不应大于 22m。"),
        ("GB50352-2019", "6.7.2", "楼梯梯段净宽不应小于 1.10m，六层及以下住宅一边设有栏杆的梯段净宽不应小于 1.00m。"),
        ("GB50180-2018", "4.0.2", "新区建设的居住区绿地率不应低于 30%，旧区改建不应低于 25%。"),
        ("GB50011-2010", "6.1.14", "地下室顶板作为上部结构嵌固部位时，侧向刚度比不宜小于 2。"),
    ]
    hits = [
        NormHit(
            norm_code=code,
            clause_no=clause,
            clause_text=text,
            score=round(rng.uniform(0.72, 0.96), 3),
            source_url=f"https://norm.example/{code}/{clause}",
            category="CODE",
        ).model_dump(mode="json")
        for code, clause, text in corpus
    ]
    hits.sort(key=lambda h: -h["score"])
    return hits


def mock_metrics(
    drawing: DrawingRef, call_id: str, tool: str = "calc_metrics"
) -> list[dict[str, Any]]:
    """Mock 指标。`call_id` 必传：数值必须有出处，`MetricResult` 会直接拒收空 ref。"""
    rng = _rng(drawing.checksum, _SALT_METRICS)

    far = round(rng.uniform(2.2, 3.6), 2)
    green = round(rng.uniform(26.0, 42.0), 1)
    fire_zone = float(rng.randrange(1500, 2450, 10))
    egress = round(rng.uniform(0.95, 1.35), 2)

    # 概率只决定"是否越界"，数值本身仍然连续，保证同一 checksum 结果稳定
    far = far if rng.random() < _FAR_FAIL_P else min(far, 2.95)
    green = green if rng.random() < _GREEN_FAIL_P else max(green, 35.5)
    fire_zone = fire_zone if rng.random() < _FIRE_ZONE_FAIL_P else min(fire_zone, 1980.0)
    egress = egress if rng.random() < _EGRESS_FAIL_P else max(egress, 1.15)

    rows = [
        MetricResult(
            metric_key="FAR",
            label="容积率",
            value=far,
            unit="",
            limit=3.0,
            limit_source="规划条件通知书-第三条",
            passed=far <= 3.0,
            fact_ref=attach_fact_ref(tool, call_id, "FAR"),
        ),
        MetricResult(
            metric_key="GREEN_RATE",
            label="绿地率",
            value=green,
            unit="%",
            limit=35.0,
            limit_source="规划条件通知书-第五条",
            passed=green >= 35.0,
            fact_ref=attach_fact_ref(tool, call_id, "GREEN_RATE"),
        ),
        MetricResult(
            metric_key="FIRE_ZONE_AREA",
            label="防火分区面积",
            value=fire_zone,
            unit="㎡",
            limit=2000.0,
            limit_source="GB50016-2014-5.3.1",
            passed=fire_zone <= 2000.0,
            fact_ref=attach_fact_ref(tool, call_id, "FIRE_ZONE_AREA"),
        ),
        MetricResult(
            metric_key="EGRESS_WIDTH",
            label="疏散楼梯净宽",
            value=egress,
            unit="m",
            limit=1.10,
            limit_source="GB50016-2014-5.5.18",
            passed=egress >= 1.10,
            fact_ref=attach_fact_ref(tool, call_id, "EGRESS_WIDTH"),
        ),
    ]
    return [r.model_dump(mode="json", by_alias=True) for r in rows]


def _overrun_ratio(metric: dict[str, Any]) -> float:
    value, limit = float(metric["value"]), float(metric["limit"])
    if limit <= 0:
        return 0.0
    return abs(value - limit) / limit


def _risk_title(metric: dict[str, Any]) -> str:
    titles = {
        "FAR": "容积率超出规划条件",
        "GREEN_RATE": "绿地率低于规划条件要求",
        "FIRE_ZONE_AREA": "防火分区面积存疑",
        "EGRESS_WIDTH": "疏散楼梯净宽不足 1.1m",
    }
    return titles.get(metric["metric_key"], f"{metric['label']}超出限值")


def _risk_why(metric: dict[str, Any]) -> str:
    value, limit, unit = metric["value"], metric["limit"], metric["unit"]
    ratio = _overrun_ratio(metric) * 100
    direction = "超限" if float(value) > float(limit) else "低于限值"
    return f"按图表面积推算 {value}{unit}，{direction} {ratio:.0f}%（限值 {limit}{unit}）"


def mock_risks(metrics: list[dict[str, Any]], drawing: DrawingRef) -> list[dict[str, Any]]:
    """指标越界 → 高风险项；同一份图纸的越界项集合恒定。"""
    rng = _rng(drawing.checksum, _SALT_METRICS)
    risks: list[dict[str, Any]] = []
    for metric in metrics:
        if metric.get("pass", metric.get("passed", True)):
            continue
        ratio = _overrun_ratio(metric)
        level = "CRITICAL" if ratio >= 0.08 else "HIGH"
        risks.append(
            RiskItem(
                risk_id=f"r_{metric['metric_key'].lower()}",
                title=_risk_title(metric),
                level=level,
                metric_key=metric["metric_key"],
                detected_value=float(metric["value"]),
                threshold=float(metric["limit"]),
                clause_ref=metric["limit_source"],
                why_suspicious=_risk_why(metric),
                drawing_id=drawing.drawing_id,
                location=make_location(rng, drawing),
                # 风险里的数值同样必须溯源：直接复用指标的 calc_metrics ref
                fact_ref=metric.get("fact_ref"),
            ).model_dump(mode="json")
        )
    return risks


def generate_mock_issues(
    *,
    drawing: DrawingRef,
    prefs: dict[str, Any] | None = None,
    fail_rate: float = 0.0,
) -> list[dict[str, Any]]:
    """同一 `drawing.checksum` ⇒ 永远同一批问题（8~15 条，顺序稳定）。"""
    rng = _rng(drawing.checksum, _SALT_ISSUES)

    # 失败注入也用同一 rng：保证"某次失败"同样可复现
    if fail_rate > 0 and rng.random() < fail_rate:
        raise RuntimeError("MOCK_INJECTED_FAILURE")

    threshold = (prefs or {}).get("severity_threshold", "INFO")
    order = {"BLOCKER": 0, "MAJOR": 1, "MINOR": 2, "INFO": 3}

    issues: list[dict[str, Any]] = []
    for _ in range(rng.randint(8, 15)):
        severity = _pick_weighted(rng, MOCK_SEVERITY_MIX)
        discipline = _pick_weighted(rng, MOCK_DISCIPLINE_MIX)
        title = rng.choice(TEMPLATES[severity])
        rule = rng.choice(RULE_CODES[severity])
        issue = IssueDraft(
            category="CODE" if severity == "BLOCKER" else rng.choice(
                ["CONSISTENCY", "CLASH", "COMPLETENESS"]
            ),
            severity=severity,  # type: ignore[arg-type]
            discipline=discipline,  # type: ignore[arg-type]
            title=title,
            description=f"{DESCRIPTIONS[severity]}依据 {rule} 判定。",
            suggestion=SUGGESTIONS[severity],
            rule_code=rule,
            confidence=rng.randint(55, 98),
            location=make_location(rng, drawing),
            location_valid=True,
            fact_ref=None,  # 由 MockEngine 填 mock_source ref
        ).model_dump(mode="json")
        # 用户偏好里的严重度阈值：低于阈值的问题仍然产出，但置信度打折（可解释降级）
        if order[severity] > order.get(threshold, 3):
            issue["confidence"] = max(40, issue["confidence"] - 10)
        issues.append(issue)

    # 先由 rng 决定相对顺序，再按 (严重度, -置信度) 稳定排序 → 顺序可复现
    rng.shuffle(issues)
    issues.sort(key=lambda i: (order[i["severity"]], -i["confidence"]))
    return issues

# 报告/汇总的渲染是"纯排版"，与数据来源无关：统一放在 report_build 里，
# mock 与 real 两个引擎共用同一份模板（报告指标表只渲染 calc_metrics 的结果）。
from app.ai.report_build import build_issue_summary, build_report_sections  # noqa: E402

__all__ = [
    "build_issue_summary",
    "build_report_sections",
    "derive_seed",
    "generate_mock_issues",
    "make_fallback_location",
    "make_location",
    "mock_metrics",
    "mock_norms",
    "mock_parse",
    "mock_risks",
]
