"""群工具规格的一次规范化迁移, 不注册旧工具或执行别名"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from satrap.core.config.model_tool_authorization import config_ids
from satrap.edictum.plugin_config import PluginConfigManager


QUERY_MOVES = {
    "group_admin_list_groups": "group_chat_list_groups",
    "group_admin_get_group_info": "group_chat_get_group_info",
    "group_admin_list_members": "group_chat_list_members",
    "group_admin_get_member": "group_chat_get_member",
    "group_admin_get_message": "group_chat_get_message",
}


def migrate_group_tool_specs(value: list[Any], catalog: Any) -> list[Any]:
    """
    迁移旧群工具开关, 不意外启用新能力或丢弃群范围

    参数:
    - value: 命名或实例配置中的插件列表
    - catalog: 当前实际插件目录

    返回:
    - 只含新工具名称的配置副本, 权限冲突时拒绝迁移
    """
    result = deepcopy(value)
    admin = next((item for item in result if isinstance(item, dict) and item.get("name") == "group_admin"), None)
    if admin is None or not isinstance(admin.get("capabilities"), dict):
        return result
    tools = admin["capabilities"].get("tools")
    if not isinstance(tools, dict):
        return result
    old_nickname = "group_admin_set_card"
    if old_nickname in tools:
        state = tools.pop(old_nickname)
        if type(state) is not bool:
            raise ValueError("旧群昵称工具状态必须为布尔值")
        tools.setdefault("group_admin_set_group_nickname", state)
    moved = {}
    for old, new in QUERY_MOVES.items():
        if old in tools:
            state = tools.pop(old)
            if type(state) is not bool:
                raise ValueError("旧群查询工具状态必须为布尔值")
            moved[new] = state and admin.get("enabled", True) is True
    if not moved:
        return result
    entry = catalog.get("group_chat")
    if entry is None:
        raise ValueError("旧查询工具迁移需要可用的 group_chat 插件目录")
    chat = next((item for item in result if isinstance(item, dict) and item.get("name") == "group_chat"), None)
    created = chat is None and "group_chat" not in result
    if chat is None:
        index = next((i for i, item in enumerate(result) if item == "group_chat"), None)
        chat = {"name": "group_chat", "enabled": True, "config": {},
                "capabilities": {kind: {name: False for name in names} for kind, names in entry.capabilities.items()}}
        if index is None:
            result.append(chat)
        else:
            chat.pop("capabilities")
            result[index] = chat
    capabilities = chat.setdefault("capabilities", {})
    if not isinstance(capabilities, dict):
        raise ValueError("群聊插件能力配置必须为对象")
    caps = capabilities.setdefault("tools", {})
    if not isinstance(caps, dict):
        raise ValueError("群聊插件工具配置必须为对象")
    for name, state in moved.items():
        existing = caps.get(name, True)
        if type(existing) is not bool:
            raise ValueError("群聊工具状态必须为布尔值")
        caps[name] = state if created else existing and state
    admin_config = admin.get("config", {})
    if not isinstance(admin_config, dict):
        raise ValueError("群管理插件配置必须为对象")
    manager = PluginConfigManager()
    admin_entry = catalog.get("group_admin")
    old_global = manager.load_global_explicit("group_admin", admin_entry.config_schema) if admin_entry is not None else {}
    old_groups = config_ids({**old_global, **admin_config}.get("allowed_groups"))
    if old_groups:
        config = chat.setdefault("config", {})
        if not isinstance(config, dict):
            raise ValueError("群聊插件配置必须为对象")
        new_global = manager.load_global_explicit("group_chat", entry.config_schema)
        new_groups = config_ids({**new_global, **config}.get("allowed_groups"))
        allowed = sorted(set(old_groups) & set(new_groups)) if new_groups else old_groups
        if not allowed:
            raise ValueError("群工具迁移的允许群范围无交集, 请明确设置查询范围")
        config["allowed_groups"] = "\n".join(allowed)
    return result
