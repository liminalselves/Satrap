from __future__ import annotations
from mcp.server import MCPServer
import inspect
from typing import Any, Literal
from satrap.core.utils.TCBuilder import Tool, ToolsManager
from satrap.core.type import safe_getattr
from satrap.core.log import logger
from .utils import TYPE_MAP


class MCPServerExporter:
    """
    MCP Server 导出器; 将本地 ToolsManager 中的工具导出为 MCP Server

    基于 mcp 2.x 的 `MCPServer`, 供其他 MCP 客户端 (如 Claude Desktop) 调用本地工具

    用法:
    ``` python
    exporter = MCPServerExporter(tools_manager, name="satrap")
    server = exporter.export()
    server.run(transport="stdio")
    ```
    """

    def __init__(
        self, tools_manager: ToolsManager, name: str = "satrap", description: str = ""
    ):
        """
        初始化 MCPServerExporter

        参数:
        - tools_manager: 工具管理器实例
        - name: 名称
        - description: 说明文本
        """
        self.tools_manager = tools_manager
        self.name = name
        self.description = description

    def export(self) -> MCPServer:
        """
        注册所有已启用工具并返回一个新的 MCPServer 实例

        返回:
        - MCPServer: 一个新的 MCPServer 实例
        """
        server = MCPServer(name=self.name, description=self.description)
        for tool_name, tool in self.tools_manager.tools.items():
            if not (tool.assert_tool() and tool.is_enabled()):
                continue
            if inspect.iscoroutinefunction(safe_getattr(tool, "execute")):
                logger.warning(f"[MCP导出] 异步工具 {tool_name} 不支持导出, 已跳过")
                continue
            try:
                handler = self._build_handler(tool)
                server.add_tool(
                    handler, name=tool_name, description=tool.description or ""
                )
            except Exception as e:
                logger.error(f"[MCP导出] 工具 {tool_name} 导出失败: {e}")
        return server

    def _build_handler(self, tool: Tool):
        """
        根据 params_dict 构建带类型化签名的处理函数 (供 MCPServer 生成 JSON Schema)

        参数:
        - tool: 工具

        返回:
        - 根据 params_dict 构建带类型化签名的处理函数 (供 MCPServer 生成 JSON Schema)
        """
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
        """
        构建并运行 MCP Server (阻塞)

        参数:
        - transport: 传输方式
        - kwargs: 额外关键字参数
        """
        self.export().run(transport=transport, **kwargs)


def export_tools_to_mcp(
    tools_manager: ToolsManager,
    name: str = "satrap",
    description: str = "",
) -> MCPServer:
    """
    快捷函数: 将本地 ToolsManager 的工具导出为 MCPServer 实例

    参数:
    - tools_manager: 工具管理器实例
    - name: 名称
    - description: 说明文本

    返回:
    - MCPServer: 快捷函数: 将本地 ToolsManager 的工具导出为 MCPServer 实例
    """
    return MCPServerExporter(tools_manager, name=name, description=description).export()
