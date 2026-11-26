"""真实解析 / 计算工具（prd-06 §6.3 的 V1.0 子集）。

四条硬约束（写在这里是因为每个工具都要遵守，散在函数里会忘）：

1. **纯同步、无状态**：这些函数在 `asyncio.to_thread` 的线程里跑，
   不碰事件循环、不碰数据库、不缓存任何任务数据（同一进程连续服务不同任务）。
2. **只读**：只用 `storage.resolve_key()` 拿到的绝对路径读文件，不写回上传目录。
3. **输出可截断**：实体的 bbox、文本块都有硬上限，`truncated` 标记必须透出，
   否则 State 快照会被一张 50MB 的 DXF 撑爆（prd-06 §3.3 铁律 2）。
4. **算不出就说算不出**：`calc_metrics` 缺输入时返回 `status=INSUFFICIENT_INPUT`，
   **绝不猜一个数字**——猜出来的数字一旦进报告，就是"看起来专业"的假结论。
"""
from __future__ import annotations

import hashlib
import math
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from app.core import storage
from app.core.config import settings

MAX_ENTITIES_COLLECTED = 5000
MAX_SAMPLES_PER_LAYER = 8
MAX_TEXT_BLOCKS = 2000
MAX_TEXTS_SCANNED = 400
MAX_BY_LAYER_KEYS = 40

# DXF $INSUNITS → 语义单位（0 = 未定义，按 mm 解释并告警）
INSUNITS_MAP = {0: "mm", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}

# 图签关键字：命中即认为是图签文本（matching 用大写比较，值保留原文）
TITLE_BLOCK_KEYS = {
    "工程名称": "project_name",
    "图名": "title",
    "设计阶段": "design_stage",
    "比例": "scale",
    "设计号": "design_no",
    "图纸编号": "drawing_no",
}
TITLE_BLOCK_REQUIRED = ("project_name", "title", "design_stage", "scale", "design_no")


class InsufficientInput(Exception):
    """指标算不出来的唯一表达方式：缺哪一个输入，说清楚，不返回瞎编的数字。"""

    def __init__(self, missing: list[str], basis: str = "") -> None:
        super().__init__(",".join(missing))
        self.missing = missing
        self.basis = basis


def _resolve(storage_key: str) -> Path:
    """storage_key → 绝对路径；路径穿越由 `storage.resolve_key` 兜住。"""
    path = storage.resolve_key(storage_key)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {storage_key}")
    return path


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


# ---------------------------------------------------------------------------
# hash_file：校验图纸原件是否与上传时一致（也是 Mock 引擎 seed 的来源）
# ---------------------------------------------------------------------------
def hash_file(storage_key: str) -> dict[str, Any]:
    path = _resolve(storage_key)
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return {"sha256": h.hexdigest(), "size": size}


# ---------------------------------------------------------------------------
# parse_dxf
# ---------------------------------------------------------------------------
def _polyline_area(entity: Any) -> float:
    """闭合多段线的鞋带公式面积（mm²）。非闭合或点数不足返回 0。"""
    try:
        if not getattr(entity, "closed", False):
            return 0.0
        points = [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]
    except Exception:
        return 0.0
    if len(points) < 3:
        return 0.0
    acc = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        acc += x1 * y2 - x2 * y1
    return abs(acc) / 2.0


def _entity_bbox(entity: Any) -> list[float] | None:
    """单实体 bbox → [x, y, w, h]；任何异常一律 None（不阻断整体解析）。"""
    from ezdxf import bbox as ezbbox

    try:
        ext = ezbbox.extents([entity], fast=True)
    except Exception:
        try:
            ext = ezbbox.extents([entity])
        except Exception:
            return None
    if not ext.has_data:
        return None
    return [
        round(ext.extmin.x, 2),
        round(ext.extmin.y, 2),
        round(ext.size.x, 2),
        round(ext.size.y, 2),
    ]


def _doc_extents(msp: Any, collected: list[tuple[str, Any]]) -> dict[str, float] | None:
    """整图 extents：先试 fast 模式（控制点包围盒），失败再退到逐实体精确计算。"""
    from ezdxf import bbox as ezbbox

    for kwargs in ({"fast": True}, {}):
        try:
            ext = ezbbox.extents(msp, **kwargs)
        except Exception:
            continue
        if ext.has_data:
            return {
                "x": round(ext.extmin.x, 2),
                "y": round(ext.extmin.y, 2),
                "w": round(ext.size.x, 2),
                "h": round(ext.size.y, 2),
            }
    # 兜底：只用白名单实体的包围盒并集（大图 fast 失败时不至于整图没坐标）
    xs: list[float] = []
    ys: list[float] = []
    for _layer, entity in collected[:500]:
        bb = _entity_bbox(entity)
        if bb:
            xs.extend([bb[0], bb[0] + bb[2]])
            ys.extend([bb[1], bb[1] + bb[3]])
    if not xs:
        return None
    return {
        "x": round(min(xs), 2),
        "y": round(min(ys), 2),
        "w": round(max(xs) - min(xs), 2),
        "h": round(max(ys) - min(ys), 2),
    }


def _guess_title_block(texts: list[dict[str, str]]) -> dict[str, Any]:
    """从文本实体里认图签：命中关键字就记一个字段，值取关键字之后的内容。"""
    found: dict[str, Any] = {}
    for item in texts:
        text = item["text"]
        for keyword, field in TITLE_BLOCK_KEYS.items():
            if field in found or keyword not in text:
                continue
            tail = text.split(keyword, 1)[1]
            value = tail.lstrip(" :：=：").strip()
            if value:
                found[field] = value[:120]
    if found:
        found["raw_sample"] = texts[0]["text"][:120] if texts else ""
    return found


def parse_dxf(
    storage_key: str,
    layers_whitelist: list[str] | None = None,
    max_entities: int = MAX_ENTITIES_COLLECTED,
) -> dict[str, Any]:
    """只读白名单图层的 DXF 解析（extents / 图层 / 实体量 / 图签 / 几何采样）。"""
    import ezdxf

    started = time.perf_counter()
    path = _resolve(storage_key)
    file_size = path.stat().st_size
    if file_size > settings.MAX_DXF_SIZE * 4:
        raise ValueError(f"DXF 文件 {file_size} 字节超过解析上限")

    whitelist = [str(x).upper() for x in (layers_whitelist or settings.dxf_layers)]
    wanted = set(whitelist)

    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()

    layers: set[str] = set()
    by_layer: dict[str, int] = {}
    collected: list[tuple[str, Any]] = []
    texts: list[dict[str, str]] = []
    total = 0
    texts_scanned = 0

    for entity in msp:
        total += 1
        layer = str(getattr(entity.dxf, "layer", "") or "")
        layers.add(layer)
        by_layer[layer] = by_layer.get(layer, 0) + 1

        dxftype = entity.dxftype()
        if dxftype in ("TEXT", "MTEXT", "ATTRIB") and texts_scanned < MAX_TEXTS_SCANNED:
            texts_scanned += 1
            raw = entity.dxf.text if dxftype == "TEXT" else entity.text
            if raw:
                texts.append({"layer": layer, "text": str(raw).strip()[:200]})

        if layer.upper() not in wanted:
            continue
        if len(collected) < max_entities:
            collected.append((layer, entity))

    # 单图层几何采样：给 detect_issues 生成"落到构件上"的问题坐标
    samples: dict[str, list[list[float]]] = {}
    areas_mm2 = 0.0
    door_widths: list[float] = []
    for layer, entity in collected:
        key = layer.upper()
        bucket = samples.setdefault(key, [])
        if len(bucket) < MAX_SAMPLES_PER_LAYER:
            bb = _entity_bbox(entity)
            if bb:
                bucket.append(bb)
                if key == "DOOR":
                    door_widths.append(max(bb[2], bb[3]))
        if key == "WALL" and entity.dxftype() in ("LWPOLYLINE", "POLYLINE"):
            areas_mm2 += _polyline_area(entity)

    extents = _doc_extents(msp, collected)
    insunits = int(doc.header.get("$INSUNITS", 0) or 0)
    unit = INSUNITS_MAP.get(insunits, "mm")

    found = sorted({layer.upper() for layer in layers} & wanted)
    missing = [layer for layer in whitelist if layer not in found]
    warnings: list[str] = []
    if insunits == 0:
        warnings.append("图纸未设置 $INSUNITS，坐标按 mm 解释")
    if missing:
        warnings.append(f"白名单图层缺失：{'、'.join(missing)}")
    if total > len(collected):
        warnings.append(f"实体总数 {total}，按白名单仅采集 {len(collected)} 个")
    if not collected:
        warnings.append("白名单图层内未找到实体，无法机审几何")

    doc = None  # noqa: F841 —— 显式解引用，尽早释放 ezdxf 的整棵对象树

    return {
        "file_type": "dxf",
        "page_count": 1,
        "pages": [{"w": float(extents["w"]) if extents else 0.0,
                   "h": float(extents["h"]) if extents else 0.0}],
        "extents": {**(extents or {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}), "unit": unit},
        "layers": sorted(layers),
        "entities_summary": {
            "entities": total,
            "texts": len(texts),
            "by_layer": dict(sorted(by_layer.items(), key=lambda kv: -kv[1])[:MAX_BY_LAYER_KEYS]),
        },
        "title_block": _guess_title_block(texts),
        "units": unit,
        "warnings": warnings,
        "coverage": {"whitelist": whitelist, "found": found, "missing": missing},
        "geometry": {
            "bboxes": samples,
            "wall_closed_area_m2": round(areas_mm2 / 1_000_000, 2) if areas_mm2 else None,
            "door_max_width_mm": round(max(door_widths), 2) if door_widths else None,
        },
        "stats": {
            "file_size": file_size,
            "parse_ms": _ms(started),
            "truncated": total > len(collected),
        },
    }


# ---------------------------------------------------------------------------
# extract_pdf
# ---------------------------------------------------------------------------
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REPLACEMENT_RE = re.compile(r"[\ufffd\uf0b7]")


def _clean_text(raw: str) -> tuple[str, float]:
    """清洗 PyMuPDF 抽出的文本，返回 (清洗后文本, 可疑字符占比)。

    PyMuPDF 抽到的是 PDF 内的字节序列按字体编码映射的结果：字体不带 ToUnicode
    CMap 时，中文会变成 `\ufffd` / 私有区码位 / 控制字符 —— 这些必须剔掉，
    但要**把比例带出去当告警**，不能悄悄清洗后当成"解析成功"。
    """
    text = _CONTROL_RE.sub("", raw or "")
    text = _REPLACEMENT_RE.sub("", text)
    stripped = text.strip()
    if not stripped:
        return "", 0.0
    suspicious = 0
    for ch in stripped:
        if ch.isspace():
            continue
        category = unicodedata.category(ch)
        if category in ("Co", "Cn", "Cs") or (category == "Cc"):
            suspicious += 1
    ratio = suspicious / max(1, len(stripped))
    return text.replace("\n", " ").strip(), round(ratio, 3)


def extract_pdf(
    storage_key: str,
    page_range: list[int] | None = None,
    max_blocks: int = MAX_TEXT_BLOCKS,
) -> dict[str, Any]:
    """PDF 解析：页数 / 页面尺寸 / 文本块 / 图签（V1.0 不做 OCR）。"""
    try:  # PyMuPDF ≥1.24 推荐 `pymupdf`，`fitz` 是兼容别名（已标记弃用）
        import pymupdf as fitz
    except ImportError:  # pragma: no cover - 老版本
        import fitz

    started = time.perf_counter()
    path = _resolve(storage_key)
    file_size = path.stat().st_size

    doc = fitz.open(str(path))
    try:
        page_count = doc.page_count
        indexes = (
            [i - 1 for i in page_range if 1 <= i <= page_count]
            if page_range
            else list(range(page_count))
        )

        pages: list[dict[str, float]] = []
        blocks: list[dict[str, Any]] = []
        geometry: list[list[float]] = []
        texts: list[dict[str, str]] = []
        text_pages = 0
        garbled_chars = 0
        warnings: list[str] = []

        for i in range(page_count):
            page = doc[i]
            pages.append({"w": round(page.rect.width, 2), "h": round(page.rect.height, 2)})
            if i not in indexes:
                continue

            page_chars = 0
            for block in page.get_text("blocks"):
                if len(block) < 5 or (len(block) > 6 and block[6] == 1):
                    continue  # 图片块
                x0, y0, x1, y1 = (float(block[0]), float(block[1]),
                                  float(block[2]), float(block[3]))
                text, ratio = _clean_text(str(block[4]))
                if not text:
                    continue
                page_chars += len(text)
                garbled_chars += int(ratio * len(text))
                if len(blocks) < max_blocks:
                    blocks.append(
                        {
                            "page_no": i + 1,
                            "text": text[:400],
                            "bbox": {"x": round(x0, 2), "y": round(y0, 2),
                                     "w": round(x1 - x0, 2), "h": round(y1 - y0, 2)},
                        }
                    )
                if len(geometry) < 40:
                    geometry.append([round(x0, 2), round(y0, 2),
                                     round(x1 - x0, 2), round(y1 - y0, 2)])
                if len(texts) < MAX_TEXTS_SCANNED:
                    texts.append({"layer": "", "text": text[:200]})
            if page_chars >= 10:
                text_pages += 1
            else:
                warnings.append(f"第 {i + 1} 页无可提取文本层（疑似扫描图，OCR 属 V1.5）")

        truncated = len(blocks) >= max_blocks
        if truncated:
            warnings.append(f"文本块超过 {max_blocks} 个，已截断")
        if garbled_chars > 0:
            warnings.append(f"检出 {garbled_chars} 个异常码位，可能为字体缺少 ToUnicode 映射")

        return {
            "file_type": "pdf",
            "page_count": page_count,
            "pages": pages,
            "extents": {
                "x": 0.0,
                "y": 0.0,
                "w": pages[0]["w"] if pages else 0.0,
                "h": pages[0]["h"] if pages else 0.0,
                "unit": "pt",
            },
            "layers": [],
            "entities_summary": {
                "entities": len(blocks),
                "texts": len(blocks),
                "text_pages": text_pages,
                "garbled_chars": garbled_chars,
            },
            "title_block": _guess_title_block(texts),
            "units": "pt",
            "warnings": warnings,
            "text_blocks": blocks,
            "coverage": {
                "whitelist": [],
                "found": [],
                "missing": [],
                "text_pages": text_pages,
                "page_count": page_count,
            },
            "geometry": {"bboxes": {"TEXT": geometry}, "garbled_chars": garbled_chars},
            "stats": {
                "file_size": file_size,
                "parse_ms": _ms(started),
                "truncated": truncated,
            },
        }
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# calc_metrics：所有数值的唯一出口（prd-06 §7.1 第 3 条）
# ---------------------------------------------------------------------------
_NUM = r"(\d+(?:\.\d+)?)"
_AREA_UNIT = r"(万㎡|万m2|万m²|㎡|平方米|m2|m²)?"

_BRIEF_PATTERNS: dict[str, list[str]] = {
    "land_area": [rf"用地面积[^\d]{{0,8}}{_NUM}\s*{_AREA_UNIT}"],
    "total_floor_area": [rf"(?:总建筑面积|地上建筑面积|建筑面积)[^\d]{{0,8}}{_NUM}\s*{_AREA_UNIT}"],
    "green_area": [rf"绿地面积[^\d]{{0,8}}{_NUM}\s*{_AREA_UNIT}"],
    "fire_zone_area": [rf"防火分区[^\d]{{0,8}}{_NUM}\s*{_AREA_UNIT}"],
    "green_rate": [rf"绿地率[^\d]{{0,8}}{_NUM}\s*%"],
    "floors": [r"(\d+)\s*层"],
    "egress_width": [rf"(?:梯段净宽|疏散宽度|净宽)[^\d]{{0,8}}{_NUM}\s*(?:m|米|mm)"],
}


def _to_sqm(value: float, unit: str | None) -> float:
    if unit and unit.startswith("万"):
        return value * 10000.0
    return value


def parse_brief_numbers(text: str) -> dict[str, float]:
    """从项目简介里抽关键指标。抽不到就是抽不到，不填默认值。"""
    found: dict[str, float] = {}
    for field, patterns in _BRIEF_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, text or "")
            if not match:
                continue
            value = float(match.group(1))
            unit = match.group(2) if match.lastindex and match.lastindex >= 2 else None
            if field in ("land_area", "total_floor_area", "green_area", "fire_zone_area"):
                found[field] = _to_sqm(value, unit)
            else:
                found[field] = value
            break
    return found


def _limit(name: str, inputs: dict[str, Any], default: float) -> tuple[float, str]:
    limits = inputs.get("limits") or {}
    if name in limits:
        return float(limits[name]), str((inputs.get("limit_sources") or {}).get(
            name, f"user_prefs.risk_thresholds[{name}]"))
    return default, str((inputs.get("limit_sources") or {}).get(name, "规范默认限值"))


def _m_far(inputs: dict[str, Any]) -> dict[str, Any]:
    land = inputs.get("land_area")
    floor_area = inputs.get("total_floor_area")
    basis_parts: list[str] = []
    derived = False
    if not floor_area:
        footprint = inputs.get("footprint_area_m2")
        floors = inputs.get("floors")
        if footprint and floors:
            floor_area = float(footprint) * float(floors)
            derived = True
            basis_parts.append(
                f"DXF 墙体闭合多段线面积 {footprint}㎡ × {int(floors)} 层（估算）"
            )
    missing = []
    if not land:
        missing.append("land_area")
    if not floor_area:
        missing.append("total_floor_area")
    if missing:
        raise InsufficientInput(missing, "；".join(basis_parts))

    value = float(floor_area) / float(land)
    limit, source = _limit("FAR", inputs, 3.0)
    if not basis_parts:
        basis_parts.append(f"简介提取：用地 {land}㎡ / 建筑面积 {floor_area}㎡")
    return {
        "value": round(value, 3),
        "unit": "",
        "limit": limit,
        "limit_source": source,
        "direction": "max",
        "basis": "；".join(basis_parts),
        "derived": derived,
    }


def _m_green_rate(inputs: dict[str, Any]) -> dict[str, Any]:
    rate = inputs.get("green_rate")
    basis = ""
    if rate is None:
        green = inputs.get("green_area")
        land = inputs.get("land_area")
        missing = [n for n, v in (("green_area", green), ("land_area", land)) if not v]
        if missing:
            raise InsufficientInput(missing)
        rate = float(green) / float(land) * 100.0
        basis = f"简介提取：绿地 {green}㎡ / 用地 {land}㎡"
    else:
        basis = f"简介提取：绿地率 {rate}%"
    limit, source = _limit("GREEN_RATE", inputs, 30.0)
    return {
        "value": round(float(rate), 2),
        "unit": "%",
        "limit": limit,
        "limit_source": source,
        "direction": "min",
        "basis": basis,
        "derived": False,
    }


def _m_fire_zone(inputs: dict[str, Any]) -> dict[str, Any]:
    area = inputs.get("fire_zone_area")
    if not area:
        raise InsufficientInput(["fire_zone_area"])
    limit, source = _limit("FIRE_ZONE_AREA", inputs, 2000.0)
    return {
        "value": round(float(area), 2),
        "unit": "㎡",
        "limit": limit,
        "limit_source": source,
        "direction": "max",
        "basis": f"简介提取：防火分区面积 {area}㎡",
        "derived": False,
    }


def _m_egress_width(inputs: dict[str, Any]) -> dict[str, Any]:
    width = inputs.get("egress_width_m")
    basis = ""
    if width is None:
        door_mm = inputs.get("door_max_width_mm")
        if not door_mm:
            raise InsufficientInput(["egress_width_m", "door_max_width_mm"])
        width = float(door_mm) / 1000.0
        basis = f"DXF DOOR 图层实体实测最大净宽 {door_mm}mm"
    else:
        basis = f"简介提取：疏散净宽 {width}m"
    limit, source = _limit("EGRESS_WIDTH", inputs, 1.10)
    return {
        "value": round(float(width), 3),
        "unit": "m",
        "limit": limit,
        "limit_source": source,
        "direction": "min",
        "basis": basis,
        "derived": False,
    }


def _m_layer_coverage(inputs: dict[str, Any]) -> dict[str, Any]:
    whitelist = inputs.get("layers_whitelist") or []
    found = {str(x).upper() for x in (inputs.get("layers_found") or [])}
    if not whitelist:
        raise InsufficientInput(["layers_whitelist"])
    hit = [layer for layer in whitelist if str(layer).upper() in found]
    value = len(hit) / len(whitelist) * 100.0
    missing = [layer for layer in whitelist if str(layer).upper() not in found]
    return {
        "value": round(value, 1),
        "unit": "%",
        "limit": 100.0,
        "limit_source": "STD-DRAWING-2.3",
        "direction": "min",
        "basis": f"白名单 {len(whitelist)} 层命中 {len(hit)} 层"
                 + (f"（缺 {'、'.join(missing)}）" if missing else ""),
        "derived": False,
    }


def _m_title_block(inputs: dict[str, Any]) -> dict[str, Any]:
    title_block = inputs.get("title_block") or {}
    present = [key for key in TITLE_BLOCK_REQUIRED if title_block.get(key)]
    value = len(present) / len(TITLE_BLOCK_REQUIRED) * 100.0
    missing = [key for key in TITLE_BLOCK_REQUIRED if key not in present]
    return {
        "value": round(value, 1),
        "unit": "%",
        "limit": 100.0,
        "limit_source": "STD-DRAWING-1.1",
        "direction": "min",
        "basis": f"图签必填 {len(TITLE_BLOCK_REQUIRED)} 项命中 {len(present)} 项"
                 + (f"（缺 {'、'.join(missing)}）" if missing else ""),
        "derived": False,
    }


def _m_text_layer(inputs: dict[str, Any]) -> dict[str, Any]:
    page_count = inputs.get("page_count")
    text_pages = inputs.get("text_pages")
    if not page_count:
        raise InsufficientInput(["page_count"])
    if text_pages is None:
        raise InsufficientInput(["text_pages"])
    value = float(text_pages) / float(page_count) * 100.0
    return {
        "value": round(value, 1),
        "unit": "%",
        "limit": 90.0,
        "limit_source": "STD-DRAWING-3.2",
        "direction": "min",
        "basis": f"{text_pages}/{page_count} 页抽到文本层",
        "derived": False,
    }


METRIC_LABELS = {
    "FAR": "容积率",
    "GREEN_RATE": "绿地率",
    "FIRE_ZONE_AREA": "防火分区面积",
    "EGRESS_WIDTH": "疏散楼梯净宽",
    "LAYER_COVERAGE": "白名单图层覆盖率",
    "TITLE_BLOCK_COMPLETENESS": "图签必填字段完整率",
    "TEXT_LAYER_RATIO": "PDF 文本层覆盖率",
}

_METRIC_CALCS: dict[str, Any] = {
    "FAR": _m_far,
    "GREEN_RATE": _m_green_rate,
    "FIRE_ZONE_AREA": _m_fire_zone,
    "EGRESS_WIDTH": _m_egress_width,
    "LAYER_COVERAGE": _m_layer_coverage,
    "TITLE_BLOCK_COMPLETENESS": _m_title_block,
    "TEXT_LAYER_RATIO": _m_text_layer,
}


def calc_metrics(metric_keys: list[str], inputs: dict[str, Any]) -> dict[str, Any]:
    """按 `metric_keys` 逐个真实计算；缺输入 → `INSUFFICIENT_INPUT`，不猜数字。"""
    results: list[dict[str, Any]] = []
    for key in metric_keys:
        calc = _METRIC_CALCS.get(key)
        if calc is None:
            results.append(
                {"metric_key": key, "status": "UNSUPPORTED_METRIC",
                 "label": METRIC_LABELS.get(key, key)}
            )
            continue
        try:
            payload = calc(inputs)
        except InsufficientInput as exc:
            results.append(
                {
                    "metric_key": key,
                    "label": METRIC_LABELS.get(key, key),
                    "status": "INSUFFICIENT_INPUT",
                    "missing": exc.missing,
                    "basis": exc.basis,
                }
            )
            continue

        value = float(payload["value"])
        limit = float(payload["limit"])
        passed = value <= limit if payload["direction"] == "max" else value >= limit
        deviation = round(abs(value - limit) / limit * 100, 2) if limit > 0 else None
        results.append(
            {
                "metric_key": key,
                "label": METRIC_LABELS.get(key, key),
                "status": "OK",
                "value": value,
                "unit": payload["unit"],
                "limit": limit,
                "limit_source": payload["limit_source"],
                "direction": payload["direction"],
                "passed": passed,
                "deviation_pct": deviation,
                "basis": payload["basis"],
                "derived": payload["derived"],
            }
        )
    return {"results": results}


# ---------------------------------------------------------------------------
# validate_location
# ---------------------------------------------------------------------------
def _clip(bbox: dict[str, float], size: dict[str, float]) -> tuple[dict[str, float], bool]:
    x1 = min(max(float(bbox.get("x", 0.0)), 0.0), float(size["w"]))
    y1 = min(max(float(bbox.get("y", 0.0)), 0.0), float(size["h"]))
    x2 = min(max(float(bbox.get("x", 0.0)) + float(bbox.get("w", 0.0)), 0.0), float(size["w"]))
    y2 = min(max(float(bbox.get("y", 0.0)) + float(bbox.get("h", 0.0)), 0.0), float(size["h"]))
    clipped = {"x": round(x1, 2), "y": round(y1, 2),
               "w": round(max(x2 - x1, 0.1), 2), "h": round(max(y2 - y1, 0.1), 2)}
    valid = (
        math.isclose(clipped["x"], float(bbox.get("x", 0.0)), abs_tol=1e-6)
        and math.isclose(clipped["y"], float(bbox.get("y", 0.0)), abs_tol=1e-6)
        and math.isclose(clipped["w"], float(bbox.get("w", 0.0)), abs_tol=1e-6)
        and math.isclose(clipped["h"], float(bbox.get("h", 0.0)), abs_tol=1e-6)
    )
    return clipped, valid


def validate_location(
    locations: list[dict[str, Any]] | None = None,
    location: dict[str, Any] | None = None,
    page_size: dict[str, float] | None = None,
    extents: dict[str, float] | None = None,
) -> dict[str, Any]:
    """坐标越界裁剪（prd-07 §4 校验规则 1）。支持单条 `location` 与批量 `locations`。"""
    items = list(locations or ([] if location is None else [location]))
    out: list[dict[str, Any]] = []
    for item in items:
        size = item.get("page_size") or page_size or extents or {"w": 0.0, "h": 0.0}
        if not size.get("w") or not size.get("h"):
            out.append({"valid": False, "clamped": None, "reason": "MISSING_PAGE_SIZE"})
            continue
        clipped, valid = _clip(item.get("bbox") or {}, size)
        out.append(
            {
                "valid": valid,
                "clamped": clipped,
                "page_size": {"w": float(size["w"]), "h": float(size["h"])},
                "reason": "OK" if valid else "OUT_OF_BOUNDS",
            }
        )
    if location is not None and not locations:
        return out[0] if out else {"valid": False, "clamped": None, "reason": "EMPTY"}
    return {"results": out}


# ---------------------------------------------------------------------------
# query_norm：静态条文白名单（降级策略，prd-06 §4.3）
# ---------------------------------------------------------------------------
def query_norm(
    query: str = "",
    top_k: int = 8,
    category: str | None = None,
    discipline: str | None = None,
) -> dict[str, Any]:
    from app.sandbox import norms

    hits = norms.search(query, top_k=top_k, category=category, discipline=discipline)
    if not hits:
        hits = norms.default_hits(top_k)
    return {"hits": hits, "total": len(hits), "query": query[:200]}
