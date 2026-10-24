"""
本地工具的 MCP 导出适配

沿用工具管理器的执行边界, 以原始 JSON Schema 同时驱动发现与调用校验,
不通过 Python 签名重新猜测组合类型或跨字段约束
"""
from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver import Context
from jsonschema.protocols import Validator
from contextvars import ContextVar
from jsonschema import Draft202012Validator, ValidationError
from mcp.server import MCPServer
from mcp.types import Tool as MCPTool, CallToolResult, InputRequiredResult
import inspect
from typing import Any, Callable, Literal, cast
import copy

from satrap.core.utils.TCBuilder import Tool, ToolsManager
from satrap.core.type import safe_getattr
from .utils import TYPE_MAP

from satrap.core.log import logger

_CALL_ARGUMENTS: ContextVar[dict[str, Any] | None] = ContextVar("mcp_export_arguments", default=None)


class _SchemaMCPServer(MCPServer):
    """通过 SDK 公开入口保持模型定义与 MCP 调用参数的一致性"""

    def __init__(self, name: str, description: str) -> None:
        """
        初始化带原始工具契约的 MCP 服务

        参数:
        - name: 服务名称
        - description: 服务说明
        """
        super().__init__(name=name, description=description)
        self._schemas: dict[str, dict[str, Any]] = {}
        self._validators: dict[str, Validator] = {}

    def register_schema(self, name: str, schema: dict[str, Any]) -> None:
        """
        校验并冻结工具的完整输入契约

        参数:
        - name: 工具名称
        - schema: 包含根节点条件和局部定义的 JSON Schema
        """
        frozen = copy.deepcopy(schema)
        check_schema = cast(Callable[[dict[str, Any]], None], safe_getattr(Draft202012Validator, "check_schema"))
        check_schema(frozen)
        self._schemas[name] = frozen
        self._validators[name] = cast(Validator, Draft202012Validator(frozen))
        # jsonschema 动态构造验证器类, 在库边界收窄到其公开协议

    async def list_tools(self) -> list[MCPTool]:
        """
        返回保留根节点约束的 MCP 工具列表

        返回:
        - 工具说明副本, 不暴露可修改的内部 schema
        """
        tools = await super().list_tools()
        return [tool.model_copy(update={"input_schema": copy.deepcopy(self._schemas[tool.name])})
                if tool.name in self._schemas else tool for tool in tools]

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Context | None = None) -> CallToolResult | InputRequiredResult:
        """
        按完整契约校验后进入 SDK 调用与现有工具权限边界

        参数:
        - name: 已导出的工具名
        - arguments: 客户端原始参数, 不补写省略字段或强制转换类型
        - context: SDK 请求上下文, 默认由 SDK 创建

        返回:
        - SDK 工具结果; 非法参数抛出 ToolError 交给 MCP 请求边界返回错误
        """
        validator = self._validators.get(name)
        if validator is not None:
            try:
                validator.validate(arguments)
            except ValidationError as error:
                reason = f"参数不符合 {error.validator} 约束, 路径={list(error.absolute_path)}"
                logger.warning(f"[MCP导出] 工具 {name} {reason}")
                raise ToolError(f"工具 {name} {reason}") from error
        token = _CALL_ARGUMENTS.set(dict(arguments))
        try:
            return await super().call_tool(name, arguments, context)
        finally:
            _CALL_ARGUMENTS.reset(token)


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
        server = _SchemaMCPServer(name=self.name, description=self.description)
        for tool_name, tool in self.tools_manager.tools.items():
            if not (tool.assert_tool() and tool.is_enabled()):
                continue
            if inspect.iscoroutinefunction(safe_getattr(tool, "execute")):
                logger.warning(f"[MCP导出] 异步工具 {tool_name} 不支持导出, 已跳过")
                continue
            try:
                handler = self._build_handler(tool)
                server.register_schema(tool_name, tool.get_tool_defined()["function"]["parameters"])
                server.add_tool(
                    handler, name=tool_name, description=tool.description or ""
                )
            except Exception as e:
                logger.error(f"[MCP导出] 工具 {tool_name} 导出失败: {e}")
        return server

    def _build_handler(self, tool: Tool):
        """
        根据工具定义构建 SDK 调度签名, 完整输入契约由服务适配层保留

        参数:
        - tool: 工具

        返回:
        - 保留必填项与默认值的处理函数, 富参数不交给 SDK 猜测类型
        """
        schema = tool.get_tool_defined()["function"]["parameters"]
        required = set(schema.get("required", []))
        execute_parameters = inspect.signature(tool.execute).parameters
        omitted: set[str] = set()

        def handler(**kwargs: Any) -> Any:
            """
            执行 SDK 调度的本地工具

            参数:
            - kwargs: SDK 校验后的签名参数, 独立调用处理函数时作为回退输入

            返回:
            - 现有工具管理器的执行结果
            """
            original = _CALL_ARGUMENTS.get()
            arguments = dict(original) if original is not None else {
                name: value for name, value in kwargs.items() if not (name in omitted and value is None)
            }
            # 保留显式 null 与省略字段的区别, SDK 补齐的默认值不能改变动态工具语义
            return self.tools_manager.execute_tool(tool.get_tool_name(), arguments)

        parameters: list[inspect.Parameter] = []
        for name, prop in schema.get("properties", {}).items():
            ptype = prop.get("type")
            annotation: Any = TYPE_MAP.get(ptype, Any) if isinstance(ptype, str) else Any
            if isinstance((tool.params_dict or {}).get(name), dict):
                annotation = Any   # 动态 schema 由原始 JSON Schema 校验, 不让 SDK 再次收窄或转换
            default = inspect.Parameter.empty
            if name not in required:
                source = execute_parameters.get(name)
                if source is not None and source.default is not inspect.Parameter.empty:
                    default = source.default
                else:
                    default = prop.get("default")
                    if "default" not in prop:
                        omitted.add(name)
            parameters.append(inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation, default=default,
            ))
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
