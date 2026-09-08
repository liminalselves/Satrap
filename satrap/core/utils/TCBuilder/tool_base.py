from typing import Dict, Tuple, Any
from satrap.core.type import safe_getattr
from satrap.core.log import logger
from .utils import create_tool_defined


class _ToolBase:
    def __init__(
        self,
        tool_name: str | None = None,
        description: str | None = None,
        params_dict: Dict[str, Tuple[str, str]] | None = None,
        owner_plugin: str | None = None,
    ):
        """
        初始化工具

        参数:
        - tool_name: 工具名称; 为 None 时读取同名类属性
        - description: 工具描述; 为 None 时读取同名类属性
        - params_dict: 工具参数定义; 为 None 时读取同名类属性
        - owner_plugin: 所属插件名 (插件禁用时执行路径过滤), None 表示不属于任何插件
        """
        cls = self.__class__
        self.tool_name = (
            tool_name if tool_name is not None else safe_getattr(cls, "tool_name")
        )
        self.description = (
            description if description is not None else safe_getattr(cls, "description")
        )
        self.params_dict = (
            params_dict if params_dict is not None else safe_getattr(cls, "params_dict")
        )
        self.owner_plugin = owner_plugin
        self.tool_available = True
        self.tool_enabled = True

        if (
            self.tool_name is None
            or self.description is None
            or self.params_dict is None
        ):
            logger.error(f"[创建工具] 工具 {tool_name} 定义不完整")
            self.tool_available = False

    def get_tool_defined(self) -> Dict[str, Any]:
        """
        获取工具定义

        返回:
        - Dict[str, Any]: 工具定义
        """
        if not self.assert_tool():
            return {}
        tool_name = self.tool_name
        description = self.description
        params_dict = self.params_dict
        if tool_name is None or description is None or params_dict is None:
            return {}  # 工具定义不完整时保持既有空字典契约
        return create_tool_defined(tool_name, description, params_dict)

    def get_tool_name(self) -> str:
        """
        获取工具名称

        返回:
        - str: 工具名称
        """
        return self.tool_name or "unknown_tool"

    def assert_tool(self) -> bool:
        """
        断言工具是否可用

        返回:
        - bool: 断言工具是否可用
        """
        return self.tool_available

    def enable(self) -> None:
        """启用工具"""
        self.tool_enabled = True

    def disable(self) -> None:
        """禁用工具"""
        self.tool_enabled = False

    def is_enabled(self) -> bool:
        """
        检查工具是否启用

        返回:
        - bool: 检查结果
        """
        return self.tool_enabled
