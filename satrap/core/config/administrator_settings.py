"""
管理员设置的配置区段与权限预览

只更新同一配置文档的管理员区段, 使用修订阻止覆盖其他设置;
草稿预览不改变运行权限, 平台身份校验由适配器声明提供
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from copy import deepcopy

from satrap.core.config.administrator_groups import AdministratorService, administrator_revision, normalize_administrator_groups
from satrap.core.config.document import load_config_document, config_document_revision, save_config_document, validate_platforms
from satrap.core.config.platform_identity import platform_instance_id
from satrap.core.call_context import CallOrigin
from satrap.edictum.plugin_catalog import PluginCatalog


def normalize_platform_user(platform: Mapping[str, Any], user_id: str) -> str:
    """
    通过实际适配器类校验用户识别号, 不建立网络连接

    参数:
    - platform: 已配置的平台实例
    - user_id: 用户填写的识别号

    返回:
    - 规范身份, 适配器不可用或格式错误时抛出 ValueError
    """
    from satrap.core.platform.catalog import adapter_catalog
    from satrap.core.platform import registry

    adapter_catalog()
    adapter = registry.get(str(platform.get("type", "")))
    if adapter is None:
        raise ValueError("所选平台的适配器不可用, 无法校验新管理员身份")
    return adapter.normalize_user_identifier(user_id)


def administrator_settings_snapshot(config: Mapping[str, Any], catalog: PluginCatalog | None = None) -> dict[str, Any]:
    """
    构造设置页快照, 标记旧成员的平台绑定是否仍有效

    参数:
    - config: 已保存配置文档
    - catalog: 可选插件目录, 默认使用宿主目录

    返回:
    - 管理组, 配置修订, 动态平台和插件列表及失效成员
    """
    platforms = validate_platforms(config.get("platforms", []))
    groups = normalize_administrator_groups(config.get("administrator_groups"), platforms)
    available = {platform["id"]: platform_instance_id(platform) for platform in platforms}
    invalid = [{"group_id": group["id"], **member} for group in groups for member in group["members"]
               if available.get(member["platform_id"]) != member["platform_instance_id"]]
    directory = catalog or PluginCatalog()
    plugins = directory.list_payloads()
    return {"groups": groups, "revision": config_document_revision(config), "section_revision": administrator_revision(groups),
            "platforms": [{"id": item["id"], "type": item["type"], "name": item.get("name", item["id"]),
                           "instance_id": platform_instance_id(item), "enabled": item.get("enable", True)} for item in platforms],
            "plugins": plugins, "plugin_errors": list(directory.scan_errors), "invalid_members": invalid}


def prepare_administrator_groups(config: Mapping[str, Any], groups: object, rebind_members: object = None) -> list[dict[str, Any]]:
    """
    校验管理草稿并显式重绑指定旧成员, 不保存或授予权限

    参数:
    - config: 当前已保存配置
    - groups: 提交的完整管理组列表
    - rebind_members: 明确要求重绑的 group_id, platform_id, user_id 列表, 默认空

    返回:
    - 服务端绑定的规范管理组, 非法提交抛出 ValueError
    """
    platforms = validate_platforms(config.get("platforms", []))
    previous = normalize_administrator_groups(config.get("administrator_groups"), platforms)
    rebinding = [] if rebind_members is None else rebind_members
    if not isinstance(rebinding, list):
        raise ValueError("rebind_members 必须是列表")
    existing = {(group["id"], member["platform_id"], member["user_id"]) for group in previous for member in group["members"]}
    requested: set[tuple[str, str, str]] = set()
    for member in rebinding:
        if not isinstance(member, dict) or set(member) != {"group_id", "platform_id", "user_id"} or any(not isinstance(value, str) for value in member.values()):
            raise ValueError("重绑成员必须明确填写组, 平台和原用户识别号")
        key = (member["group_id"], member["platform_id"], member["user_id"])
        if key not in existing or key in requested:
            raise ValueError("重绑成员不存在或重复")
        requested.add(key)
    preserved = deepcopy(previous)
    for group in preserved:
        group["members"] = [member for member in group["members"] if (group["id"], member["platform_id"], member["user_id"]) not in requested]
    return normalize_administrator_groups(groups, platforms, bind_new=True, previous=preserved, normalize_user=normalize_platform_user)


def save_administrator_groups(path: str | Path, groups: object, expected_revision: str, rebind_members: object = None) -> dict[str, Any]:
    """
    只更新管理员区段并原子保存, 版本冲突由 API 边界捕获

    参数:
    - path: 现有配置文档路径
    - groups: 完整管理组草稿
    - expected_revision: 读取时的完整配置修订, 必填
    - rebind_members: 明确重绑的旧成员, 默认空

    返回:
    - 已保存的完整配置, 非法或版本冲突不会覆盖旧文件
    """
    if not isinstance(expected_revision, str) or not expected_revision:
        raise ValueError("保存管理员设置必须携带 expected_revision")
    config = load_config_document(path)
    config["administrator_groups"] = prepare_administrator_groups(config, groups, rebind_members)
    return save_config_document(path, config, expected_revision=expected_revision)


def preview_administrator_groups(config: Mapping[str, Any], groups: object, rebind_members: object = None) -> dict[str, Any]:
    """
    计算草稿中各成员的有效插件权限, 保留多组排除规则

    参数:
    - config: 当前已保存配置
    - groups: 完整草稿
    - rebind_members: 明确重绑的成员, 默认空

    返回:
    - 各身份的有效插件与管理条目, 不保存也不应用
    """
    normalized = prepare_administrator_groups(config, groups, rebind_members)
    service = AdministratorService(lambda: validate_platforms(config.get("platforms", [])), normalized)
    catalog = PluginCatalog().scan()
    identities = sorted({(member["platform_id"], member["user_id"]) for group in normalized for member in group["members"]})
    members = []
    for platform_id, user_id in identities:
        origin = CallOrigin(platform_id, "", "", "", user_id, "", "")
        plugins = []
        for plugin in catalog:
            grant = service.resolve(origin, plugin.name)
            if grant.allowed and plugin.permissions.supports_administrators:
                plugins.append({"name": plugin.name, "group_ids": list(grant.group_ids), "permissions": [
                    {"id": key, "description": rule.description} for key, rule in plugin.permissions.rules.items() if rule.system_admin]})
        members.append({"platform_id": platform_id, "user_id": user_id, "plugins": plugins})
    return {"groups": normalized, "members": members, "section_revision": administrator_revision(normalized)}
