"""群管理审批设置的字段校验与有效模式解析"""
from __future__ import annotations

from collections.abc import Mapping

from satrap.core.config.group_store import GROUP_APPROVAL_ACTIONS, HIGH_IMPACT_ACTIONS


_PLUGIN_HIGH_RISK_TOOLS = frozenset({
    "group_admin_kick", "group_admin_ban", "group_admin_whole_ban", "group_admin_ban_anonymous",
    "group_admin_set_admin", "group_admin_set_name", "group_admin_leave", "group_admin_handle_group_request",
})


def model_plugin_requires_approval(tool_name: str, config: Mapping[str, object]) -> bool:
    """
    解析模型插件对当前管理工具的额外审批要求

    参数:
    - tool_name: 宿主确认的当前工具名
    - config: 当前会话的有效插件配置

    返回:
    - bool: 高危管理工具且显式启用逐次审批时为 True
    """
    return tool_name in _PLUGIN_HIGH_RISK_TOOLS and config.get("high_risk_approval") is True


def approval_values(explicit: Mapping[str, object]) -> dict[str, str]:
    """解析群审批设置的继承或显式模式"""
    result: dict[str, str] = {}
    for action, raw in explicit.items():
        if action not in GROUP_APPROVAL_ACTIONS:
            raise ValueError("群审批设置包含非群目标动作")
        if not isinstance(raw, dict) or raw.get("mode") not in {"inherit", "value"}:
            raise ValueError(f"审批动作 {action} 的 mode 无效")
        if raw["mode"] == "inherit":
            if set(raw) != {"mode"}:
                raise ValueError(f"审批动作 {action} 继承模式不能携带值")
            continue
        if set(raw) != {"mode", "value"} or raw["value"] not in {"approval_required", "auto_execute"}:
            raise ValueError(f"审批动作 {action} 模式无效")
        result[action] = raw["value"]
    return result


def effective_approval(
    action: str, account_defaults: Mapping[str, object], group_explicit: Mapping[str, object],
) -> tuple[str, str]:
    """按群覆盖、账号默认和高影响动作默认值解析审批模式"""
    if action not in GROUP_APPROVAL_ACTIONS:
        raise ValueError("动作不属于逐群审批范围")
    overrides = approval_values(group_explicit)
    if action in overrides:
        return overrides[action], "group"
    inherited = account_defaults.get(action)
    if inherited in {"approval_required", "auto_execute"}:
        return str(inherited), "account"
    return ("approval_required" if action in HIGH_IMPACT_ACTIONS else "auto_execute"), "default"
