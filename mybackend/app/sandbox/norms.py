"""规范条文库（V1.0 硬编码 20 条，prd-06 §4.3 降级策略的"静态条文白名单"）。

为什么先硬编码而不是接向量库：
1. 预审的**核对口径**必须先稳定 —— 条文变了，历史问题的 `rule_code` 就失去可比性；
2. 引擎的验收目标是"每个数值都能溯源"，条文检索只要**可复现**就够，
   向量库的召回率优化留到 V1.5，接进来只需替换 `search()` 的实现。

`source="FALLBACK"` 是全表统一语义：这 20 条不是检索结果，是降级白名单。
真接上向量库后，只有库里搜不到的类别才回落本表，届时本表继续保留。
"""
from __future__ import annotations

from typing import Any

# (norm_code, clause_no, clause_text, category, disciplines, keywords)
NORM_CORPUS: list[tuple[str, str, str, str, tuple[str, ...], tuple[str, ...]]] = [
    ("GB50016-2014", "5.5.18", "高层公共建筑内楼梯间、前室的门应采用乙级防火门；疏散楼梯净宽不应小于 1.10m。",
     "FIRE", ("ARCH",), ("疏散", "楼梯", "净宽", "防火门")),
    ("GB50016-2014", "5.3.1", "未设置自动喷水灭火系统的多层民用建筑，每个防火分区最大允许建筑面积 2500㎡。",
     "FIRE", ("ARCH",), ("防火分区", "面积", "喷淋")),
    ("GB50016-2014", "5.5.17", "公共建筑内房间疏散门至最近安全出口的直线距离不应大于 40m，袋形走道不应大于 22m。",
     "FIRE", ("ARCH",), ("疏散距离", "走道", "安全出口")),
    ("GB50016-2014", "6.4.2", "封闭楼梯间、防烟楼梯间应在首层采用耐火极限不低于 2.00h 的防火隔墙与其他部位分隔。",
     "FIRE", ("ARCH",), ("楼梯间", "首层", "防火隔墙")),
    ("GB50016-2014", "7.3.1", "建筑高度大于 33m 的住宅建筑应设置消防电梯。",
     "FIRE", ("ARCH", "MEP"), ("消防电梯", "建筑高度")),
    ("GB50016-2014", "8.3.4", "高层民用建筑的下列部位应设置自动灭火系统：公共活动用房、走道、可燃物品库房。",
     "FIRE", ("MEP",), ("自动喷水", "灭火系统")),
    ("GB50352-2019", "6.7.2", "楼梯梯段净宽不应小于 1.10m，六层及以下住宅一边设有栏杆的梯段净宽不应小于 1.00m。",
     "ARCH", ("ARCH",), ("梯段", "净宽")),
    ("GB50352-2019", "6.5.1", "每个梯段的踏步不应超过 18 级，亦不应少于 3 级。",
     "ARCH", ("ARCH",), ("踏步", "梯段")),
    ("GB50352-2019", "8.3.1", "建筑物的无障碍出入口应为无障碍出入口，并应设置无障碍坡道。",
     "ARCH", ("ARCH",), ("无障碍", "坡道", "出入口")),
    ("GB50180-2018", "4.0.2", "新区建设的居住区绿地率不应低于 30%，旧区改建不应低于 25%。",
     "PLANNING", ("ARCH",), ("绿地率", "居住区")),
    ("GB50180-2018", "4.0.3", "居住区道路边缘至建筑物、构筑物的最小距离应满足消防、日照与管线敷设要求。",
     "PLANNING", ("ARCH", "MEP"), ("道路", "退界", "间距")),
    ("GB50180-2018", "6.0.5", "住宅建筑日照标准应满足大寒日日照时数不低于 2h 的要求。",
     "PLANNING", ("ARCH",), ("日照", "间距")),
    ("GB50011-2010", "6.1.14", "地下室顶板作为上部结构嵌固部位时，侧向刚度比不宜小于 2。",
     "STRUCT", ("STRUCT",), ("地下室", "顶板", "嵌固", "刚度比")),
    ("GB50011-2010", "3.4.3", "建筑形体及其构件布置的平面、竖向不规则性应满足抗震设计要求。",
     "STRUCT", ("STRUCT",), ("不规则", "抗震")),
    ("GB50011-2010", "6.3.3", "框架柱的截面尺寸应满足轴压比限值要求，剪跨比不宜小于 2。",
     "STRUCT", ("STRUCT",), ("框架柱", "轴压比")),
    ("GB50009-2012", "3.2.3", "住宅、办公等楼面均布活荷载标准值应按规范取值，不得随意折减。",
     "STRUCT", ("STRUCT",), ("活荷载", "楼面")),
    ("GB50015-2019", "3.3.2", "给水立管不宜穿越卧室、客厅等居住空间。",
     "MEP", ("MEP",), ("给水立管", "立管", "穿越")),
    ("GB50015-2019", "4.4.1", "排水立管不宜布置在卧室内，且应设置伸顶通气管。",
     "MEP", ("MEP",), ("排水立管", "通气管")),
    ("GB51348-2019", "8.1.2", "民用建筑内电气竖井应独立设置，井壁耐火极限不应低于 1.00h。",
     "MEP", ("MEP",), ("电气竖井", "强电井", "耐火极限")),
    ("GB50763-2012", "3.4.2", "无障碍通道的净宽不应小于 1.20m，坡度不应大于 1:20。",
     "ARCH", ("ARCH",), ("无障碍通道", "净宽", "坡度")),
]

# 出图质量类"机构内部规则"（不是国标，但真实审图一定会查）
DRAWING_STANDARD_CORPUS: list[tuple[str, str, str, str, tuple[str, ...], tuple[str, ...]]] = [
    ("STD-DRAWING", "1.1", "施工图图签应包含工程名称、图名、设计阶段、比例、设计号五项信息。",
     "STANDARD", ("ARCH", "STRUCT", "MEP"), ("图签", "图名", "比例")),
    ("STD-DRAWING", "2.3", "建筑专业图纸应至少包含 WALL/DOOR/WINDOW/COLUMN 四个基本图层。",
     "STANDARD", ("ARCH",), ("图层", "WALL", "COLUMN")),
    ("STD-DRAWING", "3.2", "提交预审的 PDF 图纸应保留可提取文本层，扫描件需附带 OCR 文本。",
     "STANDARD", ("ARCH",), ("文本层", "扫描件", "OCR")),
]

_ALL = NORM_CORPUS + DRAWING_STANDARD_CORPUS


def all_clauses() -> list[dict[str, Any]]:
    return [
        {
            "norm_code": code,
            "clause_no": clause,
            "clause_text": text,
            "category": category,
            "disciplines": list(disciplines),
            "keywords": list(keywords),
        }
        for code, clause, text, category, disciplines, keywords in _ALL
    ]


def clause(code: str, clause_no: str) -> dict[str, Any] | None:
    for item in all_clauses():
        if item["norm_code"] == code and item["clause_no"] == clause_no:
            return item
    return None


def _score(query: str, keywords: tuple[str, ...], text: str) -> float:
    """极简关键词命中打分：命中 keyword 记满分，命中正文片段记半分。

    不做 TF-IDF：20 条语料上任何统计模型都是过度工程，只要能稳定排序即可。
    """
    if not query:
        return 0.0
    hits = sum(1 for kw in keywords if kw in query)
    if hits:
        return min(0.6 + 0.1 * hits, 0.98)
    if any(ch in text for ch in query[:2] if ch.strip()):
        return 0.5
    return 0.0


def search(
    query: str,
    *,
    top_k: int = 8,
    category: str | None = None,
    discipline: str | None = None,
) -> list[dict[str, Any]]:
    """静态条文检索：返回按相关度降序的命中，全部标 `source=FALLBACK`。"""
    hits: list[dict[str, Any]] = []
    for code, clause_no, text, cat, disciplines, keywords in _ALL:
        if category and cat != category:
            continue
        if discipline and discipline not in disciplines:
            continue
        score = _score(query, keywords, text)
        if score <= 0:
            continue
        hits.append(
            {
                "norm_code": code,
                "clause_no": clause_no,
                "clause_text": text,
                "score": round(score, 3),
                "category": cat,
                "disciplines": list(disciplines),
                "source": "FALLBACK",
            }
        )
    hits.sort(key=lambda h: (-h["score"], h["norm_code"], h["clause_no"]))
    return hits[: max(1, top_k)]


def default_hits(top_k: int = 8) -> list[dict[str, Any]]:
    """查询词为空时的兜底：给出按类别覆盖的条文，保证审查依据章节不空。"""
    picked: list[dict[str, Any]] = []
    for target in ("FIRE", "ARCH", "PLANNING", "STRUCT", "MEP", "STANDARD"):
        for item in all_clauses():
            if item["category"] == target:
                picked.append(
                    {
                        "norm_code": item["norm_code"],
                        "clause_no": item["clause_no"],
                        "clause_text": item["clause_text"],
                        "score": 0.8,
                        "category": item["category"],
                        "disciplines": item["disciplines"],
                        "source": "FALLBACK",
                    }
                )
                break
    return picked[:top_k]
