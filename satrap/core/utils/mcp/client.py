from __future__ import annotations
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from contextlib import AsyncExitStack
import threading
import asyncio
from asyncio import AbstractEventLoop
from typing import Any, Awaitable, Dict, List, Optional
from types import TracebackType
from mcp import ClientSession
from satrap.core.utils.TCBuilder import AsyncToolsManager, ToolsManager
from satrap.core.log import logger
from .utils import _consume, MCPSessionProtocol
from .adapters import MCPToolAdapter, SyncMCPToolAdapter


class MCPClient:
    """
    MCP 客户端管理器; 负责连接 MCP Server 并将远端工具注册进 AsyncToolsManager

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
            raise ValueError(
                "MCPClient 必须提供 command (stdio) 或 url (streamable HTTP) 之一"
            )
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
        """
        连接 MCP Server 并初始化会话 (幂等, 重复调用返回同一会话)

        返回:
        - MCPSessionProtocol: 同一会话)
        """
        if self._connected and self.session is not None:
            return self.session

        stack = AsyncExitStack()
        try:
            if self.command:  # stdio 传输
                params = StdioServerParameters(
                    command=self.command, args=self.args, env=self.env
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            else:  # streamable HTTP 传输
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
        """
        为 streamable HTTP 传输构建带请求头的客户端 (尽力而为, 不可用时返回 None)

        返回:
        - Optional[Any]:  None)
        """
        if not self.headers:
            return None
        try:
            import httpx2 as httpx_module  # 优先按需加载可选的 httpx2 兼容依赖
        except ImportError:
            try:
                import httpx as httpx_module  # httpx2 不可用时按需加载标准 httpx
            except ImportError:
                logger.warning("[MCP客户端] 未找到 httpx/httpx2, 无法设置自定义请求头")
                return None
        return httpx_module.AsyncClient(headers=self.headers)

    async def list_tools(self) -> list[Any]:
        """
        获取 MCP Server 暴露的所有工具

        返回:
        - list[Any]:  MCP Server 暴露的所有工具
        """
        session = await self.connect()
        result = await session.list_tools()
        return list(result.tools)

    async def register_tools(
        self,
        tools_manager: AsyncToolsManager,
        name_prefix: Optional[str] = None,
    ) -> List[MCPToolAdapter]:
        """
        连接 MCP Server 并将全部远端工具注册进 AsyncToolsManager

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

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ):
        await self.close()

    # ---------- 同步模式 (SimpleSession) ----------

    def _ensure_sync_loop(self) -> AbstractEventLoop:
        """
        懒启动后台事件循环线程 (daemon, 不阻塞进程退出)

        返回:
        - AbstractEventLoop: 懒启动后台事件循环线程 (daemon, 不阻塞进程退出)
        """
        if self._sync_loop is not None and self._sync_loop.is_running():
            return self._sync_loop
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=loop.run_forever, name=f"mcp-sync-{self.name}", daemon=True
        )
        thread.start()
        self._sync_loop = loop
        self._sync_thread = thread
        return loop

    def _sync_call(self, coro: Awaitable[Any], timeout: float) -> Any:
        """
        将协程提交到后台循环并同步等待结果

        参数:
        - coro: 协程对象
        - timeout: 超时时间

        返回:
        - Any: 将协程提交到后台循环并同步等待结果
        """
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
        """
        同步注册 MCP 工具 (SimpleSession 用): 后台循环连接, 远端工具以同步适配器注册

        参数:
        - tools_manager: 同步工具管理器
        - name_prefix: 工具名前缀, 默认使用客户端 tool_prefix

        返回:
        - List[SyncMCPToolAdapter]: 同步注册 MCP 工具 (SimpleSession 用): 后台循环连接, 远端工具以同步适配器注册
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
