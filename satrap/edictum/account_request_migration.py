"""拆分旧群管理工具中的机器人入群邀请能力"""
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from satrap.core.plugin_authorization import permission_id_list
from satrap.edictum.plugin_config import PluginConfigManager

if TYPE_CHECKING:
    from satrap.edictum.plugin_catalog import PluginCatalog


def migrate_account_request_specs(value: list[Any], catalog: PluginCatalog,
                                  manager: PluginConfigManager | None = None) -> list[Any]:
    """
    一次拆分账号邀请工具, 保留原开关和明确授权, 不注册旧执行别名

    参数:
    - value: 原插件配置列表
    - catalog: 当前可用插件目录
    - manager: 可选的全局配置读取器

    返回:
    - 包含迁移标记和独立账号插件的配置副本; 已显式配置的新插件保持原选择
    """
    result = deepcopy(value)
    index = next((i for i, item in enumerate(result) if item == "group_admin" or isinstance(item, dict) and item.get("name") == "group_admin"), None)
    if index is None:
        return result
    raw = result[index]
    admin: dict[str, Any] = {"name": raw} if isinstance(raw, str) else raw
    result[index] = admin
    state = admin.setdefault("migration_state", {})
    if not isinstance(state, dict):
        raise ValueError("群管理插件迁移状态必须为对象")
    if state.get("account_group_invites") == 1:
        return result
    old_entry = catalog.get("group_admin")
    if old_entry is None or "group_admin_handle_group_request" not in old_entry.capabilities.get("tools", {}):
        return result
    existing = next((item for item in result if item == "account_requests" or isinstance(item, dict) and item.get("name") == "account_requests"), None)
    if existing is None:
        manager = manager or PluginConfigManager()
        config = admin.get("config", {})
        capabilities = admin.get("capabilities", {})
        if not isinstance(config, dict) or not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools", {}), dict):
            raise ValueError("旧群管理配置或工具开关无效")
        old = {**manager.load_global_explicit("group_admin", old_entry.config_schema), **config}
        if not any(name in capabilities.get("tools", {}) for name in (
                "group_admin_list_group_requests", "group_admin_handle_group_request")) and not permission_id_list(old.get("request_managers")):
            state["account_group_invites"] = 1
            return result   # 没有旧申请配置时只记录迁移完成, 避免普通群管理依赖账号插件
        entry = catalog.get("account_requests")
        if entry is None:
            raise ValueError("入群邀请拆分需要可用的 account_requests 插件")
        tools = capabilities.get("tools", {})
        read = tools.get("group_admin_list_group_requests", True)
        write = tools.get("group_admin_handle_group_request", True)
        if type(read) is not bool or type(write) is not bool:
            raise ValueError("旧申请工具开关必须为布尔值")
        managers = set(permission_id_list(old.get("request_managers")))
        readers = set(permission_id_list(old.get("allowed_read_callers")))
        if readers:
            managers &= readers
        writers = managers & set(permission_id_list(old.get("allowed_callers")))
        caps = {kind: {name: False for name in names} for kind, names in entry.capabilities.items()}
        caps["tools"].update(account_requests_list_group_invites=read,
                             account_requests_handle_group_invite=write and old.get("write_tools_enabled") is True)
        caps.get("skills", {}).update({name: read or write for name in entry.capabilities.get("skills", {})})
        result.append({"name": "account_requests", "enabled": admin.get("enabled", True),
            "config": {"request_managers": "\n".join(sorted(managers)), "allowed_callers": "\n".join(sorted(writers)),
                       "allowed_invitation_groups": "\n".join(permission_id_list(old.get("allowed_groups"))),
                       "approval_required": True}, "capabilities": caps})
    state["account_group_invites"] = 1   # 删除独立账号插件后不会因再次归一化而重新安装
    return result
