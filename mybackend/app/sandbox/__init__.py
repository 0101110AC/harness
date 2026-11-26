"""沙箱层：真实解析工具的进程内实现 + 工具注册表（prd-06 §6）。

V1.0 的隔离手段是 `asyncio.to_thread` + 参数白名单 + 硬截断，不启独立子进程：

1. **同步库一律不出事件循环**：`ezdxf` / `PyMuPDF` 是纯同步的重 IO 库，
   直接在协程里调用会把整个事件循环（含 SSE 推送）钉死几秒钟。
2. **工具必须无状态**：同一进程可连续服务不同任务，禁止缓存任务数据。
3. **输出必须结构化且可截断**：实体/文本块都有硬上限，避免 State 与响应爆炸。

`mcp_server.py` 在装了 `fastmcp` 时把同一批函数注册成 MCP 工具（写时兼容），
未装则只有进程内注册表这一条路径 —— 两条路径的工具实现是同一份代码。
"""
from app.sandbox.registry import ToolRegistry, get_registry, tool_registry

__all__ = ["ToolRegistry", "get_registry", "tool_registry"]
