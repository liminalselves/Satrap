"""
MCP (Model Context Protocol) 扩展

提供两类能力:
1. `MCPClient` / `MCPToolAdapter`: 作为 MCP 客户端, 将远程 MCP Server (stdio 或
   streamable HTTP 传输) 暴露的工具包装为 `AsyncTool` 注册进 `AsyncToolsManager`,
   模型即可通过 function calling 直接调用远端工具;
2. `MCPServerExporter` / `export_tools_to_mcp`: 作为 MCP Server, 将本地
   `ToolsManager` 中的工具导出给其他 MCP 客户端 (基于 mcp 2.x 的 `MCPServer`)

客户端用法示例:
``` python
from satrap import MCPClient   # 或 from satrap.core.utils.mcp import MCPClient

# stdio 传输 (本地子进程, 如 npx 启动的 server)
mcp = MCPClient(
    command="npx",
    args=["-y", "@modelcontextprotocol/server-filesystem", "."],
)
adapters = await mcp.register_tools(tools_manager)
# 之后模型可以通过 function calling 调用远端工具

# 或 streamable HTTP 传输 (远程 server)
mcp = MCPClient(url="https://example.com/mcp", headers={"Authorization": "Bearer xxx"})

await mcp.close()   # 断开连接并自动注销已注册工具
```
"""

from __future__ import annotations
from typing import Any, Awaitable, Dict, List, Optional, Protocol, Tuple, cast
from satrap.core.type import safe_getattr, safe_getattr_str


TYPE_MAP: dict[str, type] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def content_to_text(content: Any) -> str:
    """
    将 MCP CallToolResult.content 内容块列表转换为纯文本

    参数:
    - content: MCP 工具调用结果的内容块列表 (TextContent / ImageContent / ...)

    返回:
    - str: 拼接后的文本; 空内容返回 "OK"
    """
    if content is None:
        return "OK"
    blocks = cast(list[Any], content if isinstance(content, list) else [content])
    parts: List[str] = []
    for block in blocks:
        text = safe_getattr(block, "text")
        if text is not None:
            parts.append(str(text))
            continue
        if safe_getattr(block, "type") == "image":
            mime = safe_getattr_str(block, "mimeType") or "image"
            data = safe_getattr_str(block, "data")
            parts.append(f"[image: {mime}, data {len(str(data))} chars]")
            continue
        parts.append(str(block))
    return "\n".join(p for p in parts if p) or "OK"


def _input_schema_of(mcp_tool: Any) -> dict[str, Any]:
    """
    从 MCP 工具对象提取 input_schema, 兼容 mcp 1.x (inputSchema) 与 2.x (input_schema)

    参数:
    - mcp_tool: mcp工具

    返回:
    - dict[str, Any]: 从 MCP 工具对象提取 input_schema, 兼容 mcp 1.x (inputSchema) 与 2.x (input_schema)
    """
    schema = safe_getattr(mcp_tool, "input_schema")
    if schema is None:
        schema = safe_getattr(mcp_tool, "inputSchema")
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    schema = cast(dict[str, Any], schema)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def _params_from_schema(schema: dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    """
    从 JSON Schema 提取参数名 -> (类型, 描述) 字典, 用于 Tool 基类的完整性校验

    参数:
    - schema: 数据结构定义

    返回:
    - Dict[str, Tuple[str, str]]: 从 JSON Schema 提取参数名 -> (类型, 描述) 字典, 用于 Tool 基类的完整性校验
    """
    params: Dict[str, Tuple[str, str]] = {}
    for name, prop in cast(dict[str, Any], schema.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        prop = cast(dict[str, Any], prop)
        ptype = prop.get("type", "string")
        if isinstance(ptype, list):
            ptype = next((t for t in cast(list[str], ptype) if t != "null"), "string")
        params[str(name)] = (str(ptype), str(prop.get("description", "")))
    return params


def _mcp_error(tool_name: str, message: str) -> Dict[str, Any]:
    """
    创建 MCP 工具错误结果 (与 ToolsManager 的错误字典格式一致)

    参数:
    - tool_name: 工具名称
    - message: 消息内容

    返回:
    - Dict[str, Any]: 创建 MCP 工具错误结果 (与 ToolsManager 的错误字典格式一致)
    """
    return {
        "error": message,
        "ok": False,
        "error_type": "mcp_error",
        "tool_name": tool_name,
    }


async def _consume(awaitable: Awaitable[Any]) -> Any:
    """
    将任意 Awaitable 包装为协程 (run_coroutine_threadsafe 只接受 Coroutine)

    参数:
    - awaitable: 可等待对象

    返回:
    - Any: 将任意 Awaitable 包装为协程 (run_coroutine_threadsafe 只接受 Coroutine)
    """
    return await awaitable


class MCPSessionProtocol(Protocol):
    """MCP 会话的结构化协议; 仅声明实际使用的方法, 便于测试注入假会话"""

    async def call_tool(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Any: ...

    async def list_tools(self) -> Any: ...
