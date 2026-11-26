"""FastMCP 桥（prd-06 §6.3）：把注册表里的工具原样暴露成 MCP 工具。

V1.0 的主路径是**进程内注册表**（`app/sandbox/registry.py` + `asyncio.to_thread`），
本文件只是给 V1.5 的"独立沙箱进程 / 容器"预留同一份工具实现的出口：

    pip install "fastmcp>=2.0,<3"
    python -m app.sandbox.mcp_server          # stdio 传输，供宿主侧 pool.py 连接

关键约束：**这里不许出现第二份工具逻辑**。每个 `@mcp.tool()` 都直接转调
`app.sandbox.tools` 的同一函数，否则进程内与 MCP 两条路径会慢慢跑偏
（改了一边忘了另一边，是最难查的一类线上问题）。
"""
from __future__ import annotations

import sys
from typing import Any

from app.sandbox import tools as tool_impl
from app.sandbox.registry import get_registry


def build_server() -> Any:
    """构造 FastMCP Server；未安装 fastmcp 时抛 ImportError（由 __main__ 兜底提示）。"""
    from fastmcp import FastMCP

    mcp = FastMCP("arch-harness-sandbox")

    @mcp.tool()
    async def ping() -> dict:
        """健康检查：池化命中判据（prd-06 §6.2）。"""
        return {"ok": True, "version": "1.0.0", "tools": get_registry().names}

    @mcp.tool()
    async def hash_file(storage_key: str) -> dict:
        """计算图纸 SHA256 与字节数。"""
        return tool_impl.hash_file(storage_key)

    @mcp.tool()
    async def parse_dxf(
        storage_key: str, layers_whitelist: list[str] | None = None
    ) -> dict:
        """只读白名单图层解析 DXF。"""
        return tool_impl.parse_dxf(storage_key, layers_whitelist)

    @mcp.tool()
    async def extract_pdf(storage_key: str, page_range: list[int] | None = None) -> dict:
        """解析 PDF 文本层与页幅（不含 OCR）。"""
        return tool_impl.extract_pdf(storage_key, page_range)

    @mcp.tool()
    async def calc_metrics(metric_keys: list[str], inputs: dict) -> dict:
        """硬指标计算（数值的唯一出口）。"""
        return tool_impl.calc_metrics(metric_keys, inputs)

    @mcp.tool()
    async def query_norm(
        query: str = "", top_k: int = 8,
        category: str | None = None, discipline: str | None = None,
    ) -> dict:
        """规范条文检索（V1.0 为静态白名单）。"""
        return tool_impl.query_norm(query, top_k, category, discipline)

    @mcp.tool()
    async def validate_location(
        locations: list[dict] | None = None,
        location: dict | None = None,
        page_size: dict | None = None,
        extents: dict | None = None,
    ) -> dict:
        """坐标越界裁剪。"""
        return tool_impl.validate_location(locations, location, page_size, extents)

    return mcp


def main() -> None:
    try:
        server = build_server()
    except ImportError:
        sys.stderr.write(
            "未安装 fastmcp。V1.0 使用进程内注册表（app.sandbox.registry）即可，\n"
            "需要独立沙箱进程时再 `pip install \"fastmcp>=2.0,<3\"`。\n"
        )
        raise SystemExit(2)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
