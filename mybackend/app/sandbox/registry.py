"""工具注册表（prd-06 §6.3）：名字 → 入参契约 + 超时 + 幂等 + 可达字段。

为什么要有注册表而不是直接 `from app.sandbox import tools` 调函数：

1. **工具名进 Planning 的 `tools_manifest`**：模型只能从注册表里挑工具，
   名字打错在规划校验阶段就失败，不会跑到运行期才炸（prd-06 §5.1）。
2. **超时与隔离集中在一处**：所有同步库都在 `asyncio.to_thread` 的线程里跑，
   超时由 `asyncio.wait_for` 兜住 —— 事件循环永远不会被 `ezdxf` 钉死。
3. **`fact_ref` 的字段表只有一份**：`fields` 决定"这次工具调用能支撑哪些数值"，
   数值护栏按它做存在性校验（prd-06 §7.2），引擎不许自己手写 ref。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.sandbox import tools

log = logging.getLogger(__name__)

SANDBOX_LABEL = "inline-thread"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    func: Callable[..., dict[str, Any]]
    description: str
    timeout_s: float
    idempotent: bool = True
    args: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class ToolResult:
    """一次工具调用的完整记录：既给节点用，也是 `fact_ref` 的唯一来源。"""

    ok: bool
    tool: str
    call_id: str
    duration_ms: int
    args_summary: str
    result_summary: str
    data: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    code: str | None = None
    sandbox: str = SANDBOX_LABEL

    @property
    def fact_refs(self) -> list[str]:
        return [f"{self.tool}#{self.call_id}#{name}" for name in self.fields]

    def to_call_record(self) -> dict[str, Any]:
        """写进 State.tool_calls 的形态（必须可 JSON 序列化）。"""
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "fields": dict(self.fields),
            "args_summary": self.args_summary,
            "result_summary": self.result_summary,
            "code": self.code,
            "sandbox": self.sandbox,
        }


def _scalar(value: Any) -> Any:
    return value if isinstance(value, (int, float, str, bool)) or value is None else None


def extract_fields(tool: str, data: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """从工具返回值里挑出"可以被 `fact_ref` 引用的标量字段"。

    只挑标量与计数：数组/嵌套结构不能当数值出处 —— `fact_ref` 要能一眼核对。
    """
    fields: dict[str, Any] = {}
    if tool == "hash_file":
        fields["sha256"] = data.get("sha256")
        fields["size"] = data.get("size")
    elif tool == "parse_dxf":
        summary = data.get("entities_summary") or {}
        extents = data.get("extents") or {}
        coverage = data.get("coverage") or {}
        geometry = data.get("geometry") or {}
        fields.update(
            {
                "entities": summary.get("entities"),
                "texts": summary.get("texts"),
                "extents_w": extents.get("w"),
                "extents_h": extents.get("h"),
                "layers": len(data.get("layers") or []),
                "missing_layers": len(coverage.get("missing") or []),
                "wall_area_m2": geometry.get("wall_closed_area_m2"),
                "door_max_width_mm": geometry.get("door_max_width_mm"),
                "title_block_fields": len(
                    [v for v in (data.get("title_block") or {}).values() if v]
                ),
            }
        )
    elif tool == "extract_pdf":
        summary = data.get("entities_summary") or {}
        fields.update(
            {
                "page_count": data.get("page_count"),
                "texts": summary.get("texts"),
                "text_pages": summary.get("text_pages"),
                "garbled_chars": summary.get("garbled_chars"),
            }
        )
    elif tool == "calc_metrics":
        for item in data.get("results") or []:
            key = str(item.get("metric_key"))
            if item.get("status") == "OK":
                fields[key] = item.get("value")
                fields[f"{key}.limit"] = item.get("limit")
                fields[f"{key}.deviation_pct"] = item.get("deviation_pct")
            else:
                fields[f"{key}.status"] = item.get("status")
    elif tool == "query_norm":
        hits = data.get("hits") or []
        fields["hits"] = len(hits)
        # 条文原文里的数字（"2500㎡"、"22m"、"30%"）本身就是工具返回的事实：
        # 报告引用条文时必须能溯源到这次调用，否则护栏会把"引用规范"误判成"编数字"。
        fields["clause_text"] = "；".join(
            str(item.get("clause_text") or "") for item in hits
        )[:4000]
    elif tool == "validate_location":
        results = data.get("results") or []
        fields["checked"] = len(results)
        fields["invalid"] = sum(1 for item in results if not item.get("valid"))
    return {k: v for k, v in fields.items() if _scalar(v) is not None or v is None}


def _args_summary(args: dict[str, Any]) -> str:
    parts = []
    for key, value in args.items():
        if isinstance(value, (list, dict)):
            parts.append(f"{key}=<{type(value).__name__}:{len(value)}>")
        else:
            parts.append(f"{key}={value}")
    return ",".join(parts)[:200]


def _result_summary(tool: str, data: dict[str, Any]) -> str:
    if tool == "parse_dxf":
        summary = data.get("entities_summary") or {}
        return (f"图层 {len(data.get('layers') or [])} 个，"
                f"实体 {summary.get('entities')} 个，文本 {summary.get('texts')} 条")
    if tool == "extract_pdf":
        return (f"{data.get('page_count')} 页，"
                f"文本块 {(data.get('entities_summary') or {}).get('texts')} 个")
    if tool == "calc_metrics":
        ok = [r for r in data.get("results") or [] if r.get("status") == "OK"]
        failed = [r for r in ok if not r.get("passed")]
        return f"{len(ok)} 项指标算出，{len(failed)} 项不满足限值"
    if tool == "query_norm":
        return f"命中 {len(data.get('hits') or [])} 条条文"
    if tool == "hash_file":
        return f"sha256={str(data.get('sha256'))[:12]}… size={data.get('size')}"
    if tool == "validate_location":
        return f"校验坐标 {len(data.get('results') or [])} 个"
    return "ok"


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._register_defaults()

    # ---------------- 注册 ----------------
    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def _register_defaults(self) -> None:
        self.register(
            ToolSpec(
                name="hash_file",
                func=tools.hash_file,
                description="计算图纸 SHA256 与字节数，用于校验原件一致性",
                timeout_s=30,
                args=("storage_key",),
            )
        )
        self.register(
            ToolSpec(
                name="parse_dxf",
                func=tools.parse_dxf,
                description="只读白名单图层解析 DXF：extents/图层/实体量/图签",
                timeout_s=settings.SANDBOX_DXF_TIMEOUT,
                args=("storage_key", "layers_whitelist"),
            )
        )
        self.register(
            ToolSpec(
                name="extract_pdf",
                func=tools.extract_pdf,
                description="解析 PDF：页数/页面尺寸/文本块/图签（不做 OCR）",
                timeout_s=settings.SANDBOX_PDF_TIMEOUT,
                args=("storage_key", "page_range"),
            )
        )
        self.register(
            ToolSpec(
                name="calc_metrics",
                func=tools.calc_metrics,
                description="硬指标唯一出口：容积率/绿地率/防火分区/疏散净宽/图层完整性",
                timeout_s=settings.SANDBOX_TOOL_TIMEOUT,
                args=("metric_keys", "inputs"),
            )
        )
        self.register(
            ToolSpec(
                name="query_norm",
                func=tools.query_norm,
                description="规范条文检索（V1.0 为静态白名单，命中标 FALLBACK）",
                timeout_s=30,
                args=("query", "top_k", "category", "discipline"),
            )
        )
        self.register(
            ToolSpec(
                name="validate_location",
                func=tools.validate_location,
                description="校验/裁剪问题坐标，越界返回 clamped 与 invalid 标记",
                timeout_s=10,
                args=("locations", "page_size", "extents"),
            )
        )

    # ---------------- 元信息 ----------------
    @property
    def names(self) -> list[str]:
        return sorted(self._specs)

    def manifest(self) -> list[dict[str, Any]]:
        """Planning prompt 用的工具清单：动态生成，加工具不用改 prompt。"""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "args": list(spec.args),
                "timeout_s": spec.timeout_s,
                "idempotent": spec.idempotent,
            }
            for spec in sorted(self._specs.values(), key=lambda s: s.name)
        ]

    def has(self, name: str) -> bool:
        return name in self._specs

    # ---------------- 调用 ----------------
    async def call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        call_id: str | None = None,
        timeout_s: float | None = None,
    ) -> ToolResult:
        spec = self._specs.get(name)
        cid = call_id or f"call_{uuid.uuid4().hex[:8]}"
        payload = dict(args or {})
        if spec is None:
            return ToolResult(
                ok=False, tool=name, call_id=cid, duration_ms=0,
                args_summary=_args_summary(payload), result_summary="",
                error=f"未注册的工具: {name}", code="TOOL_NOT_REGISTERED",
            )

        started = time.perf_counter()
        limit = float(timeout_s or spec.timeout_s)
        try:
            # 同步重活必须离开事件循环：ezdxf / PyMuPDF 会阻塞几秒到几十秒
            data = await asyncio.wait_for(
                asyncio.to_thread(spec.func, **payload), timeout=limit
            )
        except (asyncio.TimeoutError, TimeoutError):
            return ToolResult(
                ok=False, tool=name, call_id=cid, duration_ms=int((time.perf_counter() - started) * 1000),
                args_summary=_args_summary(payload), result_summary="",
                error=f"工具超时({limit:.0f}s)", code="TOOL_TIMEOUT",
            )
        except Exception as exc:  # 工具异常不重试，交上层决定降级还是失败
            log.warning("tool %s failed: %s", name, exc, exc_info=True)
            return ToolResult(
                ok=False, tool=name, call_id=cid, duration_ms=int((time.perf_counter() - started) * 1000),
                args_summary=_args_summary(payload), result_summary="",
                error=f"{type(exc).__name__}: {exc}", code="TOOL_ERROR",
            )

        return ToolResult(
            ok=True,
            tool=name,
            call_id=cid,
            duration_ms=int((time.perf_counter() - started) * 1000),
            args_summary=_args_summary(payload),
            result_summary=_result_summary(name, data),
            data=data,
            fields=extract_fields(name, data, payload),
        )


tool_registry = ToolRegistry()


def get_registry() -> ToolRegistry:
    return tool_registry
