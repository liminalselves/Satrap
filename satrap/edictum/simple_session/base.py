"""同步与异步会话共用的能力管理和宿主接口"""
from __future__ import annotations
from typing import Any, TYPE_CHECKING, cast
from satrap.core.plugin_interop.capabilities import PluginInterop
from satrap.core.plugin_interop.contracts import CapabilityKind
from satrap.core.utils.skills import SkillsManager
from satrap.edictum.plugin import Plugin
from satrap.core.utils.context import AsyncContextManager, ContextManager
from .handlers import _HandlerRegistryMixin


if TYPE_CHECKING:
    from satrap.edictum import AsyncSimpleSession, SimpleSession


class _SessionFeatures(_HandlerRegistryMixin):
    _plugin_interop_runtime: PluginInterop | None = None

    @property
    def _plugin_interop(self) -> PluginInterop:
        """
        惰性创建当前会话的插件交互注册表

        返回:
        - 与会话生命周期一致的内部注册表
        """
        if self._plugin_interop_runtime is None:
            from .async_ import AsyncSimpleSession
            session = cast("SimpleSession | AsyncSimpleSession", self)
            self._plugin_interop_runtime = PluginInterop(session, async_=isinstance(self, AsyncSimpleSession))
        return self._plugin_interop_runtime

    _skills_manager: SkillsManager | None
    stream: bool
    recovery_origin: dict[str, Any]

    def _capability_plugin(self, kind: CapabilityKind, name: str) -> Plugin | None:
        """
        查找能力所属插件, 统一旧会话入口与插件命名空间的开关语义

        参数:
        - kind: 能力类别
        - name: 已注册能力名

        返回:
        - 所属插件; 独立注册能力返回 None
        """
        with self._registry_lock:
            return next((plugin for plugin in self._plugins.values() if name in getattr(plugin, kind)), None)

    def _get_skills_manager(self) -> SkillsManager:
        """
        获取技能管理器 (惰性创建)

        返回:
        - SkillsManager: 技能管理器 (惰性创建)
        """
        if self._skills_manager is None:
            self._skills_manager = SkillsManager()
            self._skills_manager.availability = self._skill_available
        return self._skills_manager

    def _skill_available(self, name: str) -> bool:
        """
        核验技能的插件开关与依赖关系, 独立技能由工作流启用状态约束

        参数:
        - name: 精确技能标识

        返回:
        - 技能当前是否允许提供给模型
        """
        owner = self._capability_plugin("skills", name)
        return owner is None or self._plugin_interop.capability_available(owner.name, "skills", name)

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
        更新基础提示词, 技能目录在请求副本中独立装配

        参数:
        - prompt: 新的基础提示词
        - context: 正在更新的上下文

        返回:
        - 原始基础提示词, 不追加技能正文
        """
        workflow = getattr(self, "_wf", None)
        if self._skills_manager is not None and workflow is not None and workflow.ctx is context:
            return self._skills_manager.compose_system_prompt(prompt, workflow)
        return prompt
