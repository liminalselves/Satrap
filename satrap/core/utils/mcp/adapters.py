from __future__ import annotations
import asyncio
from asyncio import AbstractEventLoop
from typing import Any, Dict, Optional
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.core.type import safe_getattr, safe_getattr_str
from satrap.core.log import logger
from .utils import (
    content_to_text,
    _input_schema_of,
    _params_from_schema,
    _mcp_error,
    MCPSessionProtocol,
)


class MCPToolAdapter(AsyncTool):
    """
    MCP 远端工具适配器; 将 MCP Server 的工具包装为 AsyncTool 供 ToolsManager 使用

    - `get_tool_defined()` 直接透传 MCP 的 JSON Schema, 保留可选参数 / 枚举 / 嵌套结构
    - `execute()` 通过 MCP 会话调用远端工具, 并把内容块转为文本结果
    """

    def __init__(
        self,
        session: MCPSessionProtocol,
        mcp_tool: Any,
        name_prefix: Optional[str] = None,
    ):
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
        """
        获取 OpenAI function calling 格式的工具定义 (透传 MCP JSON Schema)

        返回:
        - Dict[str, Any]:  OpenAI function calling 格式的工具定义 (透传 MCP JSON Schema)
        """
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
        """
        执行远端 MCP 工具

        参数:
        - kwargs: 额外关键字参数

        返回:
        - Any: 执行远端 MCP 工具
        """
        try:
            result = await self.session.call_tool(
                self.mcp_tool.name, arguments=kwargs or None
            )
        except Exception as e:
            logger.error(f"[MCP适配器] 工具 {self.get_tool_name()} 调用失败: {e}")
            return _mcp_error(self.get_tool_name(), f"MCP 工具调用异常: {str(e)}")

        if safe_getattr(result, "is_error") or safe_getattr(result, "isError"):
            return _mcp_error(
                self.get_tool_name(), content_to_text(safe_getattr(result, "content"))
            )
        return content_to_text(safe_getattr(result, "content"))


class SyncMCPToolAdapter(Tool):
    """
    MCP 远端工具同步适配器; 通过后台事件循环线程桥接异步 MCP 调用

    - 内部持有 MCPToolAdapter (异步) 与后台事件循环, execute() 将协程提交到
      该循环并同步等待结果, 供同步 SimpleSession (ToolsManager) 使用
    """

    def __init__(
        self, inner: MCPToolAdapter, loop: AbstractEventLoop, timeout: float = 300.0
    ):
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
        """
        工具定义 (透传 MCP 的完整 JSON Schema)

        返回:
        - Dict[str, Any]: 工具定义 (透传 MCP 的完整 JSON Schema)
        """
        if not self.assert_tool():
            return {}
        return self._inner.get_tool_defined()

    def execute(self, *input: Any, **kwargs: Any) -> Any:
        """
        同步执行远端 MCP 工具: 提交到后台循环并等待结果

        参数:
        - input: 输入
        - kwargs: 额外关键字参数

        返回:
        - Any: 同步执行远端 MCP 工具: 提交到后台循环并等待结果
        """
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._inner.execute(*input, **kwargs), self._loop
            )
            return future.result(timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.error(
                f"[MCP同步适配器] 工具 {self.get_tool_name()} 调用超时 ({self._timeout}s)"
            )
            return _mcp_error(
                self.get_tool_name(), f"MCP 工具调用超时 ({self._timeout}s)"
            )
        except Exception as e:
            logger.error(f"[MCP同步适配器] 工具 {self.get_tool_name()} 调用失败: {e}")
            return _mcp_error(self.get_tool_name(), f"MCP 工具调用异常: {str(e)}")
