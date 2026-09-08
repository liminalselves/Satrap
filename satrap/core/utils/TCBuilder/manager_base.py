from typing import Dict, Any, Callable, cast
from satrap.core.log import logger
from .utils import _create_tool_error, _safe_json_dumps
from typing import Generic, TypeVar
from .tool import Tool
from .async_tool import AsyncTool

_ToolT = TypeVar("_ToolT", Tool, AsyncTool)


class _ToolsRegistry(Generic[_ToolT]):
    def get_tools_definitions(self) -> list[dict[str, Any]]:
        """
        获取所有工具的 OpenAI 格式定义

        返回:
        - 一个列表, 每个元素为所有已注册工具的 OpenAI 格式定义
        """
        return [
            tool.get_tool_defined()
            for tool in self.tools.values()
            if tool.assert_tool() and tool.is_enabled()
        ]

    def is_tool_enabled(self, tool_name: str) -> bool:
        """
        检查指定工具是否启用

        参数:
        - tool_name: 工具名称

        返回:
        - bool: 检查结果
        """
        tool = self.tools.get(tool_name)
        return bool(tool and tool.is_enabled())

    @staticmethod
    def get_call_info(call_info: Dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
        """
        获取工具调用信息

        参数:
        - call_info: 工具调用信息, 格式为 {"name": "工具名", "arguments": {"param1": "值1", "param2": "值2", ...}}

        返回:
        - 一个元组 (工具名, 参数字典)
        """
        if not isinstance(call_info, dict):
            return "", {}

        tool_name: str = str(call_info.get("name", ""))
        arguments: Any = call_info.get("arguments", {})
        if arguments is None:
            arguments = {}
        return tool_name, arguments

    @staticmethod
    def create_call_message(call_info: Dict[str, Any] | None) -> Dict[str, Any]:
        """
        创建工具调用消息

        参数:
        - call_info: 工具调用信息, 格式为 {"name": "工具名", "arguments": {"param1": "值1", "param2": "值2", ...}}

        返回:
        - 一个字典, 符合 OpenAI function calling 的工具调用消息格式

        例如:
        ```
        {
            "id": "call_abc123",
            "type": "function",
            "function": {
                "name": "sum",
                "arguments": "{"a": 1, "b": 2}"
            }
        }
        ```
        """
        if not isinstance(call_info, dict):
            call_info = {}

        tool_name = call_info.get("name", "")
        tool_call_id = call_info.get("id", "")
        arguments = call_info.get("arguments", {})
        if arguments is None:
            arguments = {}
        # 创建工具调用消息

        return {
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": _safe_json_dumps(cast(object, arguments)),
            },
        }

    @staticmethod
    def validate_call_info(call_info: Dict[str, Any] | None) -> Dict[str, Any] | None:
        """
        校验工具调用信息

        参数:
        - call_info: 调用信息

        返回:
        - Dict[str, Any] | None: 校验工具调用信息
        """
        if not isinstance(call_info, dict):
            return _create_tool_error("", "工具调用信息必须是字典", "invalid_tool_call")

        tool_name = call_info.get("name", "")
        if not isinstance(tool_name, str) or not tool_name.strip():
            return _create_tool_error(
                "", "工具调用缺少有效工具名称", "invalid_tool_call"
            )

        arguments = call_info.get("arguments", {})
        if arguments is not None and not isinstance(arguments, dict):
            return _create_tool_error(
                tool_name, f"工具 {tool_name} 参数必须是字典", "invalid_arguments"
            )

        return None

    def __init__(self):
        """初始化 ToolsManager"""
        self.tools: Dict[str, _ToolT] = {}
        self.effectiveness_guard: Callable[[str], bool] | None = None
        """生效过滤钩子 (执行路径合成): 返回 False 时工具视为不可用 (如所属插件已禁用); None 不启用"""
        self.tool_call_start: Callable[[Dict[str, Any]], None] | None = None
        """工具调用前观察钩子: 收到 {name, arguments, call_id}; None 不启用 (回调异常隔离, 不影响执行)"""
        self.tool_call_end: Callable[[Dict[str, Any]], None] | None = None
        """工具调用后观察钩子: 收到 {name, arguments, call_id, success}; None 不启用 (回调异常隔离, 不影响执行)"""

    @staticmethod
    def _notify_tool_observer(
        hook: Callable[[Dict[str, Any]], None] | None,
        event: Dict[str, Any],
    ) -> None:
        """
        触发工具观察钩子 (异常隔离: 观察者绝不能拖垮工具执行主流程)

        参数:
        - hook: 钩子函数
        - event: 事件
        """
        if hook is None:
            return
        try:
            hook(event)
        except Exception as e:
            logger.warning(f"[执行工具] 工具观察钩子异常 (已忽略): {e}")

    def _prepare_execution(
        self, tool_name: str, arguments: Dict[str, Any] | None
    ) -> tuple[_ToolT | None, Dict[str, Any], Dict[str, object] | None]:
        """统一工具参数和可用性校验, 执行方式由入口决定"""
        if not isinstance(tool_name, str) or not tool_name.strip():
            return None, {}, _create_tool_error("", "工具名称无效", "invalid_tool_call")

        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return (
                None,
                {},
                _create_tool_error(
                    tool_name, f"工具 {tool_name} 参数必须是字典", "invalid_arguments"
                ),
            )

        if tool_name not in self.tools:
            return (
                None,
                {},
                _create_tool_error(tool_name, f"工具 {tool_name} 不存在", "not_found"),
            )

        tool = self.tools[tool_name]
        if not tool.is_enabled():
            return (
                None,
                {},
                _create_tool_error(tool_name, f"工具 {tool_name} 已禁用", "disabled"),
            )
        guard = self.effectiveness_guard
        if guard is not None and not guard(tool_name):
            return (
                None,
                {},
                _create_tool_error(
                    tool_name,
                    f"工具 {tool_name} 当前不可用 (所属插件已禁用)",
                    "disabled",
                ),
            )

        return tool, arguments, None
