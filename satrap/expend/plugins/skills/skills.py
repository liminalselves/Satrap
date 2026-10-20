"""通过宿主技能来源接口提供已导入的纯文本技能"""
from __future__ import annotations



from satrap.expend.plugins.skills.store import SkillStore
from satrap.core.utils.skills import Skill
from satrap.edictum import AsyncSimpleSession, SimpleSession, PluginHost


def get_skills(session: SimpleSession | AsyncSimpleSession, config: dict[str, object],
               *, plugin_host: PluginHost | None = None) -> list[Skill]:
    """
    安装并按会话类型过滤外部技能, 不读取外部可执行代码

    参数:
    - session: 当前实际会话
    - config: 插件有效配置, allowed_skills 留空表示全部
    - plugin_host: 安装代次绑定的宿主接口

    返回:
    - 当前会话允许使用的已启用技能
    """
    if plugin_host is None:
        raise ValueError("技能插件需要有效的 plugin_host")
    selected = {name.strip() for name in str(config.get("allowed_skills") or "").splitlines() if name.strip()}
    store = SkillStore()

    def source() -> list[Skill]:
        """
        读取本插件登记的技能快照

        返回:
        - 满足全局开关, 会话类型及配置选择的纯文本技能
        """
        return [entry.skill() for entry in store.entries()
                if entry.enabled and session.plugin_environment.session_type in entry.scopes
                and (not selected or entry.name in selected or f"user.{entry.name}" in selected)]

    return plugin_host.skills.provide(source)
