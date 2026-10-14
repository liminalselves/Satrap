from __future__ import annotations
from typing import Any
from satrap.core.utils.skills import SkillsManager
from satrap.edictum.plugin import Plugin
from satrap.core.utils.context import AsyncContextManager, ContextManager
from .handlers import _HandlerRegistryMixin


class _SessionFeatures(_HandlerRegistryMixin):
    _skills_manager: SkillsManager | None
    stream: bool
    recovery_origin: dict[str, Any]

    def _get_skills_manager(self) -> SkillsManager:
        """
        获取技能管理器 (惰性创建)

        返回:
        - SkillsManager: 技能管理器 (惰性创建)
        """
        if self._skills_manager is None:
            self._skills_manager = SkillsManager()
        return self._skills_manager

    def list_skills(self) -> list[str]:
        """
        列出技能管理器已加载的技能

        返回:
        - list[str]: 列出技能管理器已加载的技能
        """
        if self._skills_manager is None:
            return []
        return self._skills_manager.list_skills()

    def list_plugins(self) -> list[Plugin]:
        """
        列出已安装插件

        返回:
        - list[Plugin]: 列出已安装插件
        """
        with self._registry_lock:
            return list(self._plugins.values())

    def set_stream_mode(self, stream: bool):
        """
        切换流式 / 非流式输出

        参数:
        - stream: 是否使用流式调用
        """
        self.stream = bool(stream)

    def compose_system_prompt(self, prompt: str, context: ContextManager | AsyncContextManager) -> str:
        """
        更新基础提示词时保留当前工作流已激活的技能指令

        参数:
        - prompt: 新的基础提示词
        - context: 正在更新的上下文

        返回:
        - str: 主工作流合成提示词, 其他上下文直接使用基础提示词
        """
        workflow = getattr(self, "_wf", None)
        if self._skills_manager is not None and workflow is not None and workflow.ctx is context:
            return self._skills_manager.compose_system_prompt(prompt, workflow)
        return prompt
