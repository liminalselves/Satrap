"""
MCP (Model Context Protocol) 扩展

提供两类能力:
1. `MCPClient` / `MCPToolAdapter`: 作为 MCP 客户端, 将远程 MCP Server (stdio 或
   streamable HTTP 传输) 暴露的工具包装为 `AsyncTool` 注册进 `AsyncToolsManager`,
   模型即可通过 function calling 直接调用远端工具;
2. `MCPServerExporter` / `export_tools_to_mcp`: 作为 MCP Server, 将本地
   `ToolsManager` 中的工具导出给其他 MCP 客户端 (基于 mcp 2.x 的 `MCPServer`).

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

import asyncio
import inspect
import threading
from asyncio import AbstractEventLoop
from contextlib import AsyncExitStack
from types import TracebackType
from typing import Any, Awaitable, Dict, List, Literal, Optional, Protocol, Tuple, cast

from satrap.core.log import logger
from satrap.core.type import safe_getattr, safe_getattr_str
from satrap.core.utils.TCBuilder import AsyncTool, AsyncToolsManager, Tool, ToolsManager

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer

TYPE_MAP: dict[str, type] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def content_to_text(content: Any) -> str:
    """将 MCP CallToolResult.content 内容块列表转换为纯文本

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
    """从 MCP 工具对象提取 input_schema, 兼容 mcp 1.x (inputSchema) 与 2.x (input_schema)"""
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
    """从 JSON Schema 提取参数名 -> (类型, 描述) 字典, 用于 Tool 基类的完整性校验"""
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
    """创建 MCP 工具错误结果 (与 ToolsManager 的错误字典格式一致)"""
    return {
        "error": message,
        "ok": False,
        "error_type": "mcp_error",
        "tool_name": tool_name,
    }


async def _consume(awaitable: Awaitable[Any]) -> Any:
    """将任意 Awaitable 包装为协程 (run_coroutine_threadsafe 只接受 Coroutine) """
    return await awaitable


class MCPSessionProtocol(Protocol):
    """MCP 会话的结构化协议; 仅声明实际使用的方法, 便于测试注入假会话"""

    async def call_tool(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Any: ...

    async def list_tools(self) -> Any: ...


class MCPToolAdapter(AsyncTool):
    """MCP 远端工具适配器; 将 MCP Server 的工具包装为 AsyncTool 供 ToolsManager 使用

    - `get_tool_defined()` 直接透传 MCP 的 JSON Schema, 保留可选参数 / 枚举 / 嵌套结构
    - `execute()` 通过 MCP 会话调用远端工具, 并把内容块转为文本结果
    """

    def __init__(self, session: MCPSessionProtocol, mcp_tool: Any, name_prefix: Optional[str] = None):
        """
        参数:
        - session: 已连接的 MCP ClientSession
        - mcp_tool: MCP 工具对象 (mcp.types.Tool), 包含 name / description / input_schema
        - name_prefix: 工具名前缀, 用于避免多来源工具名冲突; None 表示不加前缀
        """
        self.session = session
        self.mcp_tool = mcp_tool
        raw_name = safe_getattr_str(mcp_tool, "name")
        tool_name = f"{name_prefix}_{raw_name}" if name_prefix else raw_name
        self._schema = _input_schema_of(mcp_tool)
        params_dict = _params_from_schema(self._schema)
        super().__init__(
            tool_name=tool_name,
            description=safe_getattr_str(mcp_tool, "description"),
            params_dict=params_dict,
        )

    def get_tool_defined(self) -> Dict[str, Any]:
        """获取 OpenAI function calling 格式的工具定义 (透传 MCP JSON Schema)"""
        if not self.assert_tool():
            return {}
        return {
            "type": "function",
            "function": {
                "name": self.get_tool_name(),
                "description": self.description,
                "parameters": self._schema,
            },
        }

    async def execute(self, **kwargs: Any) -> Any:
        """执行远端 MCP 工具"""
        try:
            result = await self.session.call_tool(self.mcp_tool.name, arguments=kwargs or None)
        except Exception as e:
            logger.error(f"[MCP适配器] 工具 {self.get_tool_name()} 调用失败: {e}")
            return _mcp_error(self.get_tool_name(), f"MCP 工具调用异常: {str(e)}")

        if safe_getattr(result, "is_error") or safe_getattr(result, "isError"):
            return _mcp_error(self.get_tool_name(), content_to_text(safe_getattr(result, "content")))
        return content_to_text(safe_getattr(result, "content"))


class SyncMCPToolAdapter(Tool):
    """MCP 远端工具同步适配器; 通过后台事件循环线程桥接异步 MCP 调用

    - 内部持有 MCPToolAdapter (异步) 与后台事件循环, execute() 将协程提交到
      该循环并同步等待结果, 供同步 SimpleSession (ToolsManager) 使用
    """

    def __init__(self, inner: MCPToolAdapter, loop: AbstractEventLoop, timeout: float = 300.0):
        """
        参数:
        - inner: 异步 MCP 适配器 (持有 MCP 会话与远端工具定义)
        - loop: 后台事件循环 (MCP 连接与调用均在循环线程执行)
        - timeout: 单次调用同步等待超时秒数
        """
        self._inner = inner
        self._loop = loop
        self._timeout = timeout
        super().__init__(
            tool_name=inner.get_tool_name(),
            description=inner.description,
            params_dict=inner.params_dict,
        )

    def get_tool_defined(self) -> Dict[str, Any]:
        """工具定义 (透传 MCP 的完整 JSON Schema)"""
        if not self.assert_tool():
            return {}
        return self._inner.get_tool_defined()

    def execute(self, *input: Any, **kwargs: Any) -> Any:
        """同步执行远端 MCP 工具: 提交到后台循环并等待结果"""
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._inner.execute(*input, **kwargs), self._loop
            )
            return future.result(timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.error(f"[MCP同步适配器] 工具 {self.get_tool_name()} 调用超时 ({self._timeout}s)")
            return _mcp_error(self.get_tool_name(), f"MCP 工具调用超时 ({self._timeout}s)")
        except Exception as e:
            logger.error(f"[MCP同步适配器] 工具 {self.get_tool_name()} 调用失败: {e}")
            return _mcp_error(self.get_tool_name(), f"MCP 工具调用异常: {str(e)}")


class MCPClient:
    """MCP 客户端管理器; 负责连接 MCP Server 并将远端工具注册进 AsyncToolsManager

    支持两种传输:
    - stdio: `command` + `args` + `env` (本地子进程)
    - streamable HTTP: `url` (+ `headers`)

    用法:
    ``` python
    mcp = MCPClient(command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "."])
    adapters = await mcp.register_tools(tools_manager)
    ...
    await mcp.close()
    ```
    """

    def __init__(
        self,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        name: str = "mcp",
        tool_prefix: Optional[str] = None,
    ):
        """
        参数:
        - command: stdio 传输的启动命令 (如 "npx", "python"); 与 url 二选一
        - args: stdio 传输的命令参数列表
        - env: 传递给子进程的额外环境变量
        - url: streamable HTTP 传输的 MCP Server 地址; 与 command 二选一
        - headers: streamable HTTP 传输的自定义请求头 (需环境可导入 httpx/httpx2)
        - name: 客户端名称, 用作默认工具名前缀
        - tool_prefix: 工具名前缀; 默认使用 name; 传空字符串可禁用前缀
        """
        if not command and not url:
            raise ValueError("MCPClient 必须提供 command (stdio) 或 url (streamable HTTP) 之一")
        self.command = command
        self.args = list(args or [])
        self.env = env
        self.url = url
        self.headers = headers
        self.name = name
        self.tool_prefix = tool_prefix if tool_prefix is not None else name
        self.session: Optional[MCPSessionProtocol] = None
        self.adapters: List[MCPToolAdapter] = []
        self._stack: Optional[AsyncExitStack] = None
        self._connected = False
        self._tools_manager: Optional[AsyncToolsManager] = None
        # 同步模式 (SimpleSession) 专用: 后台事件循环线程 + 同步适配器
        self._sync_loop: Optional[AbstractEventLoop] = None
        self._sync_thread: Optional[threading.Thread] = None
        self._sync_adapters: List[SyncMCPToolAdapter] = []
        self._sync_tools_manager: Optional[ToolsManager] = None

    async def connect(self) -> MCPSessionProtocol:
        """连接 MCP Server 并初始化会话 (幂等, 重复调用返回同一会话)"""
        if self._connected and self.session is not None:
            return self.session

        stack = AsyncExitStack()
        try:
            if self.command:   # stdio 传输
                params = StdioServerParameters(command=self.command, args=self.args, env=self.env)
                read, write = await stack.enter_async_context(stdio_client(params))
            else:   # streamable HTTP 传输
                url = self.url
                if url is None:
                    raise ConnectionError("MCPClient 未提供 url")
                http_client = self._build_http_client()
                read, write = await stack.enter_async_context(
                    streamable_http_client(url, http_client=http_client)
                )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception as e:
            await stack.aclose()
            raise ConnectionError(f"MCP Server 连接失败 ({self.name}): {e}") from e

        self._stack = stack
        self.session = session
        self._connected = True
        logger.info(f"[MCP客户端] 已连接: {self.name}")
        return session

    def _build_http_client(self) -> Optional[Any]:
        """为 streamable HTTP 传输构建带请求头的客户端 (尽力而为, 不可用时返回 None)"""
        if not self.headers:
            return None
        try:
            import httpx2 as httpx_module
        except ImportError:
            try:
                import httpx as httpx_module
            except ImportError:
                logger.warning("[MCP客户端] 未找到 httpx/httpx2, 无法设置自定义请求头")
                return None
        return httpx_module.AsyncClient(headers=self.headers)

    async def list_tools(self) -> list[Any]:
        """获取 MCP Server 暴露的所有工具"""
        session = await self.connect()
        result = await session.list_tools()
        return list(result.tools)

    async def register_tools(
        self,
        tools_manager: AsyncToolsManager,
        name_prefix: Optional[str] = None,
    ) -> List[MCPToolAdapter]:
        """连接 MCP Server 并将全部远端工具注册进 AsyncToolsManager

        参数:
        - tools_manager: 异步工具管理器
        - name_prefix: 工具名前缀, 默认使用客户端 tool_prefix

        返回:
        - 注册的适配器列表
        """
        session = await self.connect()
        self._tools_manager = tools_manager
        prefix = self.tool_prefix if name_prefix is None else name_prefix
        for mcp_tool in await self.list_tools():
            adapter = MCPToolAdapter(session, mcp_tool, name_prefix=prefix)
            tools_manager.register_tool(adapter)
            self.adapters.append(adapter)
        logger.info(
            f"[MCP客户端] 已注册 {len(self.adapters)} 个远端工具: "
            f"{[a.get_tool_name() for a in self.adapters]}"
        )
        return list(self.adapters)

    async def close(self):
        """断开连接, 并从工具管理器注销所有已注册适配器"""
        if self._tools_manager is not None:
            for adapter in self.adapters:
                self._tools_manager.unregister_tool(adapter.get_tool_name())
            self.adapters.clear()
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self.session = None
        self._connected = False
        logger.info(f"[MCP客户端] 已断开: {self.name}")

    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None):
        await self.close()

    # ---------------- 同步模式 (SimpleSession) ----------------

    def _ensure_sync_loop(self) -> AbstractEventLoop:
        """懒启动后台事件循环线程 (daemon, 不阻塞进程退出)"""
        if self._sync_loop is not None and self._sync_loop.is_running():
            return self._sync_loop
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name=f"mcp-sync-{self.name}", daemon=True)
        thread.start()
        self._sync_loop = loop
        self._sync_thread = thread
        return loop

    def _sync_call(self, coro: Awaitable[Any], timeout: float) -> Any:
        """将协程提交到后台循环并同步等待结果"""
        loop = self._sync_loop
        if loop is None or not loop.is_running():
            raise RuntimeError(f"MCP 后台事件循环未运行 ({self.name})")
        future = asyncio.run_coroutine_threadsafe(_consume(coro), loop)
        return future.result(timeout=timeout)

    def sync_register_tools(
        self,
        tools_manager: ToolsManager,
        name_prefix: Optional[str] = None,
    ) -> List[SyncMCPToolAdapter]:
        """同步注册 MCP 工具 (SimpleSession 用): 后台循环连接, 远端工具以同步适配器注册

        参数:
        - tools_manager: 同步工具管理器
        - name_prefix: 工具名前缀, 默认使用客户端 tool_prefix
        """
        loop = self._ensure_sync_loop()
        session = self._sync_call(self.connect(), 30)
        self._sync_tools_manager = tools_manager
        prefix = self.tool_prefix if name_prefix is None else name_prefix
        for mcp_tool in self._sync_call(self.list_tools(), 60):
            inner = MCPToolAdapter(session, mcp_tool, name_prefix=prefix)
            outer = SyncMCPToolAdapter(inner, loop)
            tools_manager.register_tool(outer)
            self._sync_adapters.append(outer)
        logger.info(
            f"[MCP客户端] 已同步注册 {len(self._sync_adapters)} 个远端工具: "
            f"{[a.get_tool_name() for a in self._sync_adapters]}"
        )
        return list(self._sync_adapters)

    def sync_close(self) -> None:
        """同步断开: 注销同步适配器, 关闭连接, 停止后台事件循环"""
        manager = self._sync_tools_manager
        if manager is not None:
            for adapter in self._sync_adapters:
                manager.unregister_tool(adapter.get_tool_name())
            self._sync_adapters.clear()
        if self._stack is not None:
            try:
                self._sync_call(self._stack.aclose(), 30)
            except Exception as e:
                logger.warning(f"[MCP客户端] 同步断开连接异常: {e}")
        self._stack = None
        self.session = None
        self._connected = False
        loop, thread = self._sync_loop, self._sync_thread
        self._sync_loop = None
        self._sync_thread = None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
        logger.info(f"[MCP客户端] 已同步断开: {self.name}")


class MCPServerExporter:
    """MCP Server 导出器; 将本地 ToolsManager 中的工具导出为 MCP Server

    基于 mcp 2.x 的 `MCPServer`, 供其他 MCP 客户端 (如 Claude Desktop) 调用本地工具

    用法:
    ``` python
    exporter = MCPServerExporter(tools_manager, name="satrap")
    server = exporter.export()
    server.run(transport="stdio")
    ```
    """

    def __init__(self, tools_manager: ToolsManager, name: str = "satrap", description: str = ""):
        self.tools_manager = tools_manager
        self.name = name
        self.description = description

    def export(self) -> MCPServer:
        """注册所有已启用工具并返回一个新的 MCPServer 实例"""
        server = MCPServer(name=self.name, description=self.description)
        for tool_name, tool in self.tools_manager.tools.items():
            if not (tool.assert_tool() and tool.is_enabled()):
                continue
            if inspect.iscoroutinefunction(safe_getattr(tool, "execute")):
                logger.warning(f"[MCP导出] 异步工具 {tool_name} 不支持导出, 已跳过")
                continue
            try:
                handler = self._build_handler(tool)
                server.add_tool(handler, name=tool_name, description=tool.description or "")
            except Exception as e:
                logger.error(f"[MCP导出] 工具 {tool_name} 导出失败: {e}")
        return server

    def _build_handler(self, tool: Tool):
        """根据 params_dict 构建带类型化签名的处理函数 (供 MCPServer 生成 JSON Schema)"""
        params_dict = tool.params_dict or {}

        def handler(**kwargs: Any) -> Any:
            return self.tools_manager.execute_tool(tool.get_tool_name(), kwargs)

        parameters = [
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=TYPE_MAP.get(ptype, str),
            )
            for name, (ptype, _desc) in params_dict.items()
        ]
        setattr(handler, "__signature__", inspect.Signature(parameters))
        return handler

    def run(
        self,
        transport: Literal["stdio", "sse", "streamable-http"] = "stdio",
        **kwargs: Any,
    ):
        """构建并运行 MCP Server (阻塞)"""
        self.export().run(transport=transport, **kwargs)


def export_tools_to_mcp(
    tools_manager: ToolsManager,
    name: str = "satrap",
    description: str = "",
) -> MCPServer:
    """快捷函数: 将本地 ToolsManager 的工具导出为 MCPServer 实例"""
    return MCPServerExporter(tools_manager, name=name, description=description).export()


__all__ = [
    "MCPClient",
    "MCPToolAdapter",
    "SyncMCPToolAdapter",
    "MCPServerExporter",
    "export_tools_to_mcp",
    "content_to_text",
]
