"""
技能提供者的宿主登记和本轮强制加载入口

动态来源绑定插件安装代次, 只更新当前提供者的纯文本技能,
强制使用通过任务资料传递, 不改变其它能力的授权
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from satrap.core.utils.skills import Skill
from satrap.core.type import CommandAction, RequestContextBlock

if TYPE_CHECKING:
    from .capabilities import PluginHost


@dataclass(frozen=True)
class SkillHostAccess:
    """绑定当前插件身份的技能资料接口, 不提供工具权限"""

    _host: PluginHost

    def provide(self, factory: Callable[[], list[Skill]]) -> list[Skill]:
        """
        登记当前插件的纯文本技能来源, 后续任务开始时刷新

        参数:
        - factory: 返回当前插件管理的纯文本技能

        返回:
        - 首次登记的技能, 交给安装器统一校验与启用
        """
        runtime = self._host._runtime()
        manager = runtime.session()._get_skills_manager()
        owner = self._host._owner
        independent: dict[str, bool] = {}

        def refresh() -> None:
            """只替换当前插件的技能名册, 不执行外部技能代码"""
            self._host._runtime()
            binding = runtime.bindings[owner]
            plugin = binding.plugin
            if plugin is None:
                return
            found = factory()
            target = {item.skill_id or item.name: item for item in found}
            if len(target) != len(found) or any(item.tools or item.mcp_clients for item in found):
                raise ValueError("动态技能来源只能提供不重名的纯文本技能")
            if any(key in manager.skills and key not in plugin.skills for key in target):
                raise ValueError("技能标识与其它提供者冲突")
            workflow = runtime.session()._wf
            if workflow is None:
                return
            previous = set(plugin.skills)
            independent.update(plugin.skills)
            for key in previous - target.keys():
                manager._active.get(id(workflow), {}).pop(key, None)
                manager.unregister_skill(key)
            plugin.skills = {key: independent.get(key, True) for key in target}
            for key, item in target.items():
                manager._register_skill(item)
                if plugin.skills[key]:
                    manager._active.setdefault(id(workflow), {})[key] = item
                else:
                    manager._active.get(id(workflow), {}).pop(key, None)
            manager.attach(workflow)
            manager.detach_if_empty(workflow)

        manager.sources[owner] = refresh
        return factory()

    def catalog(self) -> list[tuple[str, str]]:
        """
        查询当前模型通道允许使用的技能目录

        返回:
        - 精确标识与用途简介, 不包含被停用或被替代的技能
        """
        session = self._host._runtime().session()
        manager = session._get_skills_manager()
        return [(item.skill_id or item.name, item.description) for item in manager.available_skills(session._wf)]

    def read(self, name: str) -> str:
        """
        按模型通道相同规则读取技能说明

        参数:
        - name: 精确技能标识

        返回:
        - 技能正文, 不可用时抛出 ValueError
        """
        session = self._host._runtime().session()
        return session._get_skills_manager().read_skill(name, session._wf)

    def use(self, name: str, task: str = "") -> CommandAction | str:
        """
        为用户命令准备立即执行或下一次本人任务的技能资料

        参数:
        - name: 精确技能标识
        - task: 本次任务, 为空时只登记下一次本人任务

        返回:
        - 本轮执行动作或待办回执, 不伪造工具调用消息
        """
        session = self._host._runtime().session()
        manager = session._get_skills_manager()
        content = manager.read_skill(name, session._wf)
        if task:
            manager._forced.pop(manager.caller_key(), None)
            return CommandAction("run_task", user_input=task,
                                 request_blocks=(RequestContextBlock("forced_skill", "skills", content),))
        if session._wf is None:
            raise ValueError("会话尚未初始化")
        manager.force_next(name, session._wf)
        return f"下一次由你发起的任务将使用技能: {name}"
