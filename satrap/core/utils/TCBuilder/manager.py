from typing import Dict, Any, cast
from satrap.core.log import logger
from .utils import _tool_execution_error
from .tool import Tool
from .manager_base import _ToolsRegistry


class ToolsManager(_ToolsRegistry[Tool]):
    """工具管理器, 用于注册和执行工具"""

    def register_tool(self, tool: Tool):
        """
        注册工具

        参数:
        - tool: 要注册的工具实例, 必须是 Tool 类的子类
        """
        if not tool.assert_tool():
            logger.error(f"[注册工具] 工具 {tool.get_tool_name()} 定义不完整, 无法注册")

        else:
            self.tools[tool.get_tool_name()] = tool
            logger.info(f"[注册工具] 工具 {tool.get_tool_name()} 已注册")

    def enable_tool(self, tool_name: str) -> bool:
        """
        启用指定工具

        参数:
        - tool_name: 工具名称

        返回:
        - bool: 启用指定工具
        """
        tool = self.tools.get(tool_name)
        if not tool:
            logger.warning(f"[启用工具] 工具 {tool_name} 不存在, 无法启用")
            return False
        tool.enable()
        logger.info(f"[启用工具] 工具 {tool_name} 已启用")
        return True

    def disable_tool(self, tool_name: str) -> bool:
        """
        禁用指定工具

        参数:
        - tool_name: 工具名称

        返回:
        - bool: 禁用指定工具
        """
        tool = self.tools.get(tool_name)
        if not tool:
            logger.warning(f"[禁用工具] 工具 {tool_name} 不存在, 无法禁用")
            return False
        tool.disable()
        logger.info(f"[禁用工具] 工具 {tool_name} 已禁用")
        return True

    def enable_all_tools(self) -> bool:
        """
        启用所有已注册工具

        返回:
        - bool: 启用所有已注册工具
        """
        if not self.tools:
            logger.warning("[启用工具] 当前没有已注册工具")
            return False
        for tool in self.tools.values():
            tool.enable()
        logger.info("[启用工具] 已启用所有工具")
        return True

    def disable_all_tools(self) -> bool:
        """
        禁用所有已注册工具

        返回:
        - bool: 禁用所有已注册工具
        """
        if not self.tools:
            logger.warning("[禁用工具] 当前没有已注册工具")
            return False
        for tool in self.tools.values():
            tool.disable()
        logger.info("[禁用工具] 已禁用所有工具")
        return True

    def execute_tool(self, tool_name: str, arguments: Dict[str, Any] | None) -> Any:
        """
        执行指定工具

        参数:
        - tool_name: 工具名称
        - arguments: 调用参数

        返回:
        - Any: 执行指定工具
        """
        tool, arguments, error = self._prepare_execution(tool_name, arguments)
        if tool is None:
            return error

        try:
            result = tool(**arguments)
            logger.debug(f"[执行工具] 工具 {tool_name} 执行成功")
            return result
        except Exception as e:
            return _tool_execution_error(tool_name, e, arguments)

    def execute_tool_call(self, call_info: Dict[str, Any]) -> Any:
        """
        执行工具调用流程

        参数:
        - call_info: 工具调用信息, 格式为 {"name": "工具名", "arguments": {"param1": "值1", "param2": "值2", ...}}

        返回:
        - 元组 (工具调用消息, 工具调用的返回结果)
        """
        tool_message = self.create_call_message(call_info)
        call_error = self.validate_call_info(call_info)
        if call_error is not None:
            return tool_message, call_error

        tool_name, arguments = self.get_call_info(call_info)
        call_id = str(call_info.get("id", "")) if isinstance(call_info, dict) else ""
        base_event: Dict[str, Any] = {
            "name": tool_name,
            "arguments": arguments,
            "call_id": call_id,
        }
        self._notify_tool_observer(self.tool_call_start, dict(base_event))
        tool_result = self.execute_tool(tool_name, arguments)
        success = not (
            isinstance(tool_result, dict)
            and cast(Dict[str, Any], tool_result).get("ok") is False
        )
        self._notify_tool_observer(
            self.tool_call_end, {**base_event, "success": success}
        )
        return tool_message, tool_result

    def unregister_tool(self, tool_name: str) -> bool:
        """
        注销工具

        参数:
        - tool_name: 工具名称

        返回:
        - bool: 注销工具
        """
        if tool_name in self.tools:
            del self.tools[tool_name]
            logger.info(f"[注销工具] 工具 {tool_name} 已注销")
            return True
        else:
            logger.warning(f"[注销工具] 工具 {tool_name} 不存在，无法注销")
            return False

    def unregister_all_tools(self) -> bool:
        """
        注销所有工具

        返回:
        - bool: 注销所有工具
        """
        if self.tools:
            self.tools.clear()
            logger.info(f"[注销工具] 已注销所有工具")
            return True
        else:
            logger.warning(f"[注销工具] 已没有已注册工具")
            return False
