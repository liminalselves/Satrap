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


def migrate_group_tool_specs(value: list[Any], catalog: Any, migration_manager: Any = None) -> list[Any]:
    """
    迁移旧群工具开关, 不意外启用新能力或丢弃群范围

    参数:
    - value: 命名或实例配置中的插件列表
    - catalog: 当前实际插件目录
    - migration_manager: 好友迁移使用的全局配置管理器, 默认项目目录

    返回:
    - 只含新工具名称的配置副本, 权限冲突时拒绝迁移
    """
    result = migrate_friend_tool_specs(migrate_forward_tool_specs(value, catalog), catalog, migration_manager)
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


FRIEND_MOVES = {
    "group_admin_list_friend_requests": "friend_manager_list_requests",
    "group_admin_handle_friend_request": "friend_manager_handle_request",
}


FORWARD_MOVES = {"group_admin_get_forward": "message_forward_read", "group_admin_send_forward": "message_forward_compose"}


def migrate_forward_tool_specs(value: list[Any], catalog: Any) -> list[Any]:
    """迁移旧转发工具并清除废弃范围字段, 原消息发送与跨对话功能不自动启用"""
    result = deepcopy(value)
    for item in result:
        if isinstance(item, dict) and item.get("name") == "message_forward" and isinstance(item.get("config"), dict):
            item["config"].pop("allow_private", None)
            item["config"].pop("allowed_groups", None)
    admin = next((item for item in result if isinstance(item, dict) and item.get("name") == "group_admin"), None)
    if admin is None:
        return result
    raw_caps = admin.get("capabilities", {})
    raw_config = admin.get("config", {})
    if not isinstance(raw_caps, dict) or not isinstance(raw_config, dict):
        raise ValueError("旧转发插件配置和能力状态必须为对象")
    tools = raw_caps.get("tools", {})
    if not isinstance(tools, dict):
        return result
    explicit = any(name in tools for name in FORWARD_MOVES)
    if not explicit:
        return result
    entry = catalog.get("message_forward")
    if entry is None:
        if explicit:
            raise ValueError("旧转发工具迁移需要可用的 message_forward 插件")
        return result
    existing = next((item for item in result if isinstance(item, dict) and item.get("name") == "message_forward"), None)
    old_entry = catalog.get("group_admin")
    manager = PluginConfigManager()
    old_global = manager.load_global_explicit("group_admin", old_entry.config_schema) if old_entry else {}
    old_config = {**old_global, **raw_config}
    moved = {}
    for old, new in FORWARD_MOVES.items():
        enabled = tools.pop(old, True)
        if type(enabled) is not bool:
            raise ValueError("旧转发工具状态必须为布尔值")
        moved[new] = enabled and admin.get("enabled", True) is True
    created = existing is None and "message_forward" not in result
    if existing is None:
        existing = {"name": "message_forward", "enabled": admin.get("enabled", True), "config": {},
                    "capabilities": {kind: {name: False for name in names} for kind, names in entry.capabilities.items()}}
        if created:
            result.append(existing)
        else:
            existing.pop("capabilities")
            result[result.index("message_forward")] = existing
    config = existing.setdefault("config", {})
    caps = existing.setdefault("capabilities", {}).setdefault("tools", {})
    if not isinstance(config, dict) or not isinstance(caps, dict):
        raise ValueError("转发插件配置和工具状态必须为对象")
    merged = {**manager.load_global_explicit("message_forward", entry.config_schema), **config}
    for old_key, new_key, empty_denies in (("allowed_callers", "write_callers", True), ("allowed_read_callers", "read_callers", False)):
        old_ids = set(config_ids(old_config.get(old_key)))
        new_ids = set(config_ids(merged.get(new_key)))
        effective = old_ids if created else old_ids & new_ids if empty_denies or old_ids and new_ids else old_ids or new_ids
        if not created and not empty_denies and old_ids and new_ids and not effective:
            raise ValueError("转发迁移授权范围无交集, 请明确设置新插件权限")
        config[new_key] = "\n".join(sorted(effective))
    config["send_enabled"] = old_config.get("write_tools_enabled") is True and (created or merged.get("send_enabled") is True)
    if created:
        config["cross_conversation_enabled"] = False
    for name, enabled in moved.items():
        caps[name] = enabled if created else enabled and caps.get(name, True)
    return result


def migrate_friend_tool_specs(value: list[Any], catalog: Any, migration_manager: Any = None) -> list[Any]:
    """
    迁移旧好友申请工具, 不启用新增查询或删除能力

    参数:
    - value: 原始插件列表
    - catalog: 可用插件目录
    - migration_manager: 可选全局配置迁移管理器

    返回:
    - 不含旧好友工具名的副本, 授权冲突时明确拒绝
    """
    from satrap.edictum.friend_migration import migrate_friend_switch_specs
    result = migrate_friend_switch_specs(value, migration_manager or PluginConfigManager())
    admin = next((item for item in result if isinstance(item, dict) and item.get("name") == "group_admin"), None)
    if admin is None:
        if "group_admin" not in result:
            return result
        admin = {"name": "group_admin"}
    caps = admin.get("capabilities", {})
    if not isinstance(caps, dict) or not isinstance(caps.get("tools", {}), dict):
        return result
    old_tools = caps.get("tools", {})
    old_entry = catalog.get("group_admin")
    global_config = PluginConfigManager().load_global_explicit("group_admin", old_entry.config_schema) if old_entry else {}
    raw_config = admin.get("config", {})
    if not isinstance(raw_config, dict):
        return result
    old_config = {**global_config, **raw_config}
    managers = config_ids(old_config.get("request_managers"))
    existing: dict[str, Any] | None = next((item for item in result if isinstance(item, dict) and item.get("name") == "friend_manager"), None)
    has_existing = existing is not None or "friend_manager" in result
    explicit = any(name in old_tools for name in FRIEND_MOVES)
    if not explicit and (has_existing or not managers):
        return result
    entry = catalog.get("friend_manager")
    if entry is None:
        raise ValueError("旧好友工具迁移需要可用的 friend_manager 插件")
    moved = {}
    for old, new in FRIEND_MOVES.items():
        enabled = old_tools.pop(old, True)
        if type(enabled) is not bool:
            raise ValueError("旧好友工具开关必须为布尔值")
        moved[new] = enabled and admin.get("enabled", True) is True
    if existing is None:
        if has_existing:
            existing = {"name": "friend_manager"}
            result[result.index("friend_manager")] = existing
        else:
            existing = {"name": "friend_manager", "config_version": 1, "enabled": admin.get("enabled", True), "config": {},
                        "capabilities": {kind: {name: False for name in names} for kind, names in entry.capabilities.items()}}
            result.append(existing)
    new_config = existing.setdefault("config", {})
    new_caps = existing.setdefault("capabilities", {}).setdefault("tools", {})
    if not isinstance(new_config, dict) or not isinstance(new_caps, dict):
        raise ValueError("好友插件配置或能力开关无效")
    new_global = PluginConfigManager().load_global_explicit("friend_manager", entry.config_schema)
    merged = {**new_global, **new_config}
    new_managers = config_ids(merged.get("managers"))
    if has_existing:
        managers = sorted(set(managers) & set(new_managers))
    callers = config_ids(old_config.get("allowed_callers"))
    writers = sorted(set(managers) & set(callers)) if callers else managers
    if has_existing:
        writers = sorted(set(writers) & set(config_ids(merged.get("write_callers"))))
    new_config["managers"] = "\n".join(managers)
    new_config["write_callers"] = "\n".join(writers)
    handle_enabled = old_config.get("write_tools_enabled") is True and bool(writers)
    if not has_existing:
        for kind, names in entry.capabilities.items():
            existing.setdefault("capabilities", {}).setdefault(kind, {}).update({name: False for name in names})
    for name, state in moved.items():
        new_caps[name] = state and (new_caps.get(name, True) if has_existing else True)
    new_caps["friend_manager_handle_request"] = new_caps["friend_manager_handle_request"] and handle_enabled
    return result
