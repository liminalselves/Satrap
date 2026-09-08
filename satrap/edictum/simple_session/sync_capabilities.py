from __future__ import annotations
from typing import Any, Callable
from satrap.core.APICall.LLMCall import LLM
from satrap.core.utils.TCBuilder import Tool
from satrap.core.utils.skills import SkillsManager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sync import SimpleSession


def add_command(
    self: SimpleSession, name: str, handler: Callable[..., Any], intro: str = "None"
):
    """
    注册命令

    参数:
    - name: 名称
    - handler: 处理器
    - intro: 简介文本
    """
    self.cmd_handler.register_command(name, handler, intro=intro)


def remove_command(self: SimpleSession, name: str) -> bool:
    """
    注销命令

    参数:
    - name: 名称

    返回:
    - bool: 注销命令
    """
    return self.cmd_handler.unregister_command(name)


def enable_command(self: SimpleSession, name: str) -> bool:
    """
    启用命令

    参数:
    - name: 名称

    返回:
    - bool: 启用命令
    """
    return self.cmd_handler.enable_command(name)


def disable_command(self: SimpleSession, name: str) -> bool:
    """
    停用命令 (停用后消息不再按命令处理)

    参数:
    - name: 名称

    返回:
    - bool: 停用命令 (停用后消息不再按命令处理)
    """
    return self.cmd_handler.disable_command(name)


def is_command_enabled(self: SimpleSession, name: str) -> bool:
    """
    检查命令是否启用

    参数:
    - name: 名称

    返回:
    - bool: 检查结果
    """
    return self.cmd_handler.is_command_enabled(name)


def list_commands(self: SimpleSession) -> dict[str, str]:
    """
    列出已注册命令及其简介

    返回:
    - dict[str, str]: 列出已注册命令及其简介
    """
    return self.cmd_handler.list_commands()


def add_tool(self: SimpleSession, tool: Tool):
    """
    注册工具 (任意时刻可注入, 立即生效)

    参数:
    - tool: 工具
    """
    self._wf.tools_manager.register_tool(tool)


def add_tools(self: SimpleSession, *tools: Tool):
    """
    批量注册工具

    参数:
    - tools: 工具列表
    """
    for tool in tools:
        self._wf.tools_manager.register_tool(tool)


def remove_tool(self: SimpleSession, name: str) -> bool:
    """
    注销工具

    参数:
    - name: 名称

    返回:
    - bool: 注销工具
    """
    return self._wf.tools_manager.unregister_tool(name)


def enable_tool(self: SimpleSession, name: str) -> bool:
    """
    启用工具

    参数:
    - name: 名称

    返回:
    - bool: 启用工具
    """
    return self._wf.tools_manager.enable_tool(name)


def disable_tool(self: SimpleSession, name: str) -> bool:
    """
    停用工具

    参数:
    - name: 名称

    返回:
    - bool: 停用工具
    """
    return self._wf.tools_manager.disable_tool(name)


def is_tool_enabled(self: SimpleSession, name: str) -> bool:
    """
    检查工具是否启用

    参数:
    - name: 名称

    返回:
    - bool: 检查结果
    """
    return self._wf.tools_manager.is_tool_enabled(name)


def list_tools(self: SimpleSession) -> list[str]:
    """
    列出已注册工具名

    返回:
    - list[str]: 列出已注册工具名
    """
    return list(self._wf.tools_manager.tools.keys())


def add_skill(
    self: SimpleSession, skill_name: str, skills_manager: SkillsManager | None = None
) -> bool:
    """
    加载并激活技能到主工作流

    参数:
    - skill_name: skill名称
    - skills_manager: skills管理器

    返回:
    - bool: 加载并激活技能到主工作流
    """
    mgr = skills_manager or self._get_skills_manager()
    if skills_manager is not None:
        self._skills_manager = skills_manager
    return mgr.activate(skill_name, self._wf)


def remove_skill(self: SimpleSession, skill_name: str) -> bool:
    """
    从工作流卸载并从管理器移除技能定义

    参数:
    - skill_name: skill名称

    返回:
    - bool: 从工作流卸载并从管理器移除技能定义
    """
    mgr = self._skills_manager
    if mgr is None:
        return False
    removed = mgr.deactivate(skill_name, self._wf)
    mgr.unregister_skill(skill_name)
    return removed


def enable_skill(self: SimpleSession, skill_name: str) -> bool:
    """
    重新激活技能

    参数:
    - skill_name: skill名称

    返回:
    - bool: 重新激活技能
    """
    return self.add_skill(skill_name)


def disable_skill(self: SimpleSession, skill_name: str) -> bool:
    """
    停用技能 (从工作流卸载, 保留注册)

    参数:
    - skill_name: skill名称

    返回:
    - bool: 停用技能 (从工作流卸载, 保留注册)
    """
    if self._skills_manager is None:
        return False
    return self._skills_manager.deactivate(skill_name, self._wf)


def _tool_effective(self: SimpleSession, tool_name: str) -> bool:
    """
    工具生效过滤 (执行路径合成): 所属插件缺失或启用时生效

    参数:
    - tool_name: 工具名称

    返回:
    - bool: 工具生效过滤 (执行路径合成): 所属插件缺失或启用时生效
    """
    tool = self._wf.tools_manager.tools.get(tool_name)
    owner = tool.owner_plugin if tool is not None else None
    if owner is None:
        return True
    with self._registry_lock:
        plugin = self._plugins.get(owner)
    return plugin is not None and plugin.enabled


def set_llm(self: SimpleSession, llm: LLM):
    """
    替换主模型

    参数:
    - llm: 模型实例
    """
    self._wf.llm = llm


def set_model_parameters(self: SimpleSession, **kwargs: Any):
    """
    调整主模型调用参数 (temperature / top_p / max_tokens 等)

    参数:
    - kwargs: 额外关键字参数
    """
    self._wf.llm.set_parameters(**kwargs)


def reload_llm(self: SimpleSession, llm: LLM):
    """
    重载 LLM 实例 (Session 兼容接口)

    参数:
    - llm: 模型实例
    """
    self.set_llm(llm)
