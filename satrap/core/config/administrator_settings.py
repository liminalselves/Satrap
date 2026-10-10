"""
管理员设置的配置区段与权限预览

只更新同一配置文档的管理组与成员例外区段, 使用修订阻止覆盖其他设置;
草稿预览不改变运行权限, 平台身份校验由适配器声明提供
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from copy import deepcopy

from satrap.core.config.administrator_groups import (AdministratorService, administrator_revision, migrate_legacy_scope_exclusions,
                                                     normalize_administrator_overrides, normalize_administrator_groups)
from satrap.core.config.document import load_config_document, config_document_revision, save_config_document, validate_platforms
from satrap.core.config.platform_identity import platform_instance_id
from satrap.core.call_context import CallOrigin
from satrap.edictum.plugin_catalog import PluginCatalog

from satrap.core.platform import registry


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

    adapter_catalog()
    adapter = registry.get(str(platform.get("type", "")))
    if adapter is None:
        raise ValueError("所选平台的适配器不可用, 无法校验新管理员身份")
    return adapter.normalize_user_identifier(user_id)


def _rebind_keys(
    existing: set[tuple[str, str, str]], rebinding: object, key_field: str, field_name: str, label: str,
) -> set[tuple[str, str, str]]:
    """
    校验显式重绑列表并返回被要求重绑的旧身份

    参数:
    - existing: 已保存条目中 (条目 ID, 平台 ID, 用户识别号) 的集合
    - rebinding: 提交的重绑列表, 缺省空
    - key_field: 条目 ID 在重绑项中的字段名
    - field_name: 请求载荷中的列表字段名, 用于错误提示
    - label: 提示中的条目名称

    返回:
    - 去重后的重绑身份集合, 结构非法或引用不存在条目时抛出 ValueError
    """
    rebinding = [] if rebinding is None else rebinding
    if not isinstance(rebinding, list):
        raise ValueError(f"{field_name} 必须是列表")
    requested: set[tuple[str, str, str]] = set()
    for entry in rebinding:
        if not isinstance(entry, dict) or set(entry) != {key_field, "platform_id", "user_id"} or any(not isinstance(value, str) for value in entry.values()):
            raise ValueError(f"重绑{label}必须明确填写条目, 平台和原用户识别号")
        key = (entry[key_field], entry["platform_id"], entry["user_id"])
        if key not in existing or key in requested:
            raise ValueError(f"重绑{label}不存在或重复")
        requested.add(key)
    return requested


def _group_member_keys(groups: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    """
    展开管理组中所有成员的绑定身份

    参数:
    - groups: 已保存的规范管理组列表

    返回:
    - (组 ID, 平台 ID, 用户识别号) 集合
    """
    return {(group["id"], member["platform_id"], member["user_id"])
            for group in groups for member in group["members"]}


def _override_keys(overrides: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    """
    展开成员例外条目的绑定身份

    参数:
    - overrides: 已保存的规范成员例外列表

    返回:
    - (条目 ID, 平台 ID, 用户识别号) 集合
    """
    return {(item["id"], item["platform_id"], item["user_id"]) for item in overrides}


def _strip_members(source: list[dict[str, Any]], requested: set[tuple[str, str, str]]) -> list[dict[str, Any]]:
    """
    生成移除待重绑成员后的旧管理组副本

    参数:
    - source: 已保存的规范管理组列表
    - requested: 被要求重绑的成员集合

    返回:
    - 深拷贝且已剔除待重绑成员的组列表, 供重新绑定新实例代次
    """
    preserved = deepcopy(source)
    for item in preserved:
        item["members"] = [member for member in item["members"] if (item["id"], member["platform_id"], member["user_id"]) not in requested]
    return preserved


def _strip_overrides(source: list[dict[str, Any]], requested: set[tuple[str, str, str]]) -> list[dict[str, Any]]:
    """
    生成移除待重绑身份后的旧成员例外副本

    参数:
    - source: 已保存的规范成员例外列表
    - requested: 被要求重绑的身份集合

    返回:
    - 深拷贝且已剔除待重绑条目的例外列表, 供重新绑定新实例代次
    """
    return [deepcopy(item) for item in source if (item["id"], item["platform_id"], item["user_id"]) not in requested]


def administrator_settings_snapshot(
    config: Mapping[str, Any], catalog: PluginCatalog | None = None, enabled_plugins: set[str] | None = None,
    platform_plugins: Mapping[str, set[str]] | None = None,
) -> dict[str, Any]:
    """
    构造设置页快照, 标记旧身份的平台绑定是否仍有效

    参数:
    - config: 已保存配置文档
    - catalog: 可选插件目录, 默认使用宿主目录
    - enabled_plugins: 可选的启用插件名称集合, 提供时给插件载荷标注 enabled
    - platform_plugins: 可选的平台 ID 到加载插件集合, 提供时给插件载荷标注 loaded_platforms

    返回:
    - 管理组, 成员例外, 配置修订, 动态平台和插件列表及失效身份
    """
    platforms = validate_platforms(config.get("platforms", []))
    groups = normalize_administrator_groups(config.get("administrator_groups"), platforms)
    overrides = normalize_administrator_overrides(config.get("administrator_overrides"), platforms)
    migrated_groups, migrated_overrides, pending = migrate_legacy_scope_exclusions(groups, overrides)
    # 迁移在读取路径生效, 界面据此提示保存后落盘
    migrated_from_legacy = migrated_groups != groups or migrated_overrides != overrides
    groups, overrides = migrated_groups, migrated_overrides
    available = {platform["id"]: platform_instance_id(platform) for platform in platforms}
    invalid = [{"group_id": group["id"], **member} for group in groups for member in group["members"]
               if available.get(member["platform_id"]) != member["platform_instance_id"]]
    invalid_overrides = [{"override_id": item["id"], "platform_id": item["platform_id"],
                          "platform_instance_id": item["platform_instance_id"], "user_id": item["user_id"]}
                         for item in overrides
                         if available.get(item["platform_id"]) != item["platform_instance_id"]]
    directory = catalog or PluginCatalog()
    plugins = directory.list_payloads()
    if enabled_plugins is not None:
        for payload in plugins:
            payload["enabled"] = payload.get("name") in enabled_plugins
    if platform_plugins is not None:
        for payload in plugins:
            payload["loaded_platforms"] = sorted(pid for pid, names in platform_plugins.items() if payload.get("name") in names)
    return {"groups": groups, "overrides": overrides, "revision": config_document_revision(config),
            "section_revision": administrator_revision(groups, overrides),
            "platforms": [{"id": item["id"], "type": item["type"], "name": item.get("name", item["id"]),
                           "instance_id": platform_instance_id(item), "enabled": item.get("enable", True)} for item in platforms],
            "plugins": plugins, "plugin_errors": list(directory.scan_errors), "invalid_members": invalid,
            "invalid_overrides": invalid_overrides, "migrated_from_legacy": migrated_from_legacy,
            "migration_pending": pending}


def prepare_administrator_groups(config: Mapping[str, Any], groups: object, rebind_members: object = None) -> list[dict[str, Any]]:
    """
    校验管理组草稿并显式重绑指定旧成员, 不保存或授予权限

    参数:
    - config: 当前已保存配置
    - groups: 提交的完整管理组列表
    - rebind_members: 明确要求重绑的 group_id, platform_id, user_id 列表, 默认空

    返回:
    - 服务端绑定的规范管理组, 非法提交抛出 ValueError
    """
    platforms = validate_platforms(config.get("platforms", []))
    previous = normalize_administrator_groups(config.get("administrator_groups"), platforms)
    requested = _rebind_keys(_group_member_keys(previous), rebind_members, "group_id", "rebind_members", "成员")
    preserved = _strip_members(previous, requested)
    return normalize_administrator_groups(groups, platforms, bind_new=True, previous=preserved,
                                          normalize_user=normalize_platform_user, strict_scope=True)


def prepare_administrator_overrides(config: Mapping[str, Any], overrides: object, rebind_overrides: object = None) -> list[dict[str, Any]]:
    """
    校验成员例外草稿并显式重绑指定旧身份, 不保存或授予权限

    参数:
    - config: 当前已保存配置
    - overrides: 提交的完整成员例外列表
    - rebind_overrides: 明确要求重绑的 override_id, platform_id, user_id 列表, 默认空

    返回:
    - 服务端绑定的规范成员例外, 非法提交抛出 ValueError
    """
    platforms = validate_platforms(config.get("platforms", []))
    previous = normalize_administrator_overrides(config.get("administrator_overrides"), platforms)
    requested = _rebind_keys(_override_keys(previous), rebind_overrides, "override_id", "rebind_overrides", "例外")
    preserved = _strip_overrides(previous, requested)
    return normalize_administrator_overrides(overrides, platforms, bind_new=True, previous=preserved,
                                             normalize_user=normalize_platform_user, strict_scope=True)


def save_administrator_groups(
    path: str | Path, groups: object, *, overrides: object, expected_revision: str,
    rebind_members: object = None, rebind_overrides: object = None,
) -> dict[str, Any]:
    """
    只更新管理员双区段并原子保存, 版本冲突由 API 边界捕获

    参数:
    - path: 现有配置文档路径
    - groups: 完整管理组草稿
    - overrides: 完整成员例外草稿, 必填以免静默丢弃例外层
    - expected_revision: 读取时的完整配置修订, 必填
    - rebind_members: 明确重绑的旧成员, 默认空
    - rebind_overrides: 明确重绑的旧例外身份, 默认空

    返回:
    - 已保存的完整配置, 非法或版本冲突不会覆盖旧文件
    """
    if not isinstance(expected_revision, str) or not expected_revision:
        raise ValueError("保存管理员设置必须携带 expected_revision")
    config = load_config_document(path)
    config["administrator_groups"] = prepare_administrator_groups(config, groups, rebind_members)
    config["administrator_overrides"] = prepare_administrator_overrides(config, overrides, rebind_overrides)
    return save_config_document(path, config, expected_revision=expected_revision)


def preview_administrator_groups(
    config: Mapping[str, Any], groups: object, *, overrides: object,
    rebind_members: object = None, rebind_overrides: object = None,
) -> dict[str, Any]:
    """
    计算草稿中各身份的有效插件权限, 保留组与例外两侧来源

    参数:
    - config: 当前已保存配置
    - groups: 完整管理组草稿
    - overrides: 完整成员例外草稿, 必填以免预览漏算例外层
    - rebind_members: 明确重绑的成员, 默认空
    - rebind_overrides: 明确重绑的例外身份, 默认空

    返回:
    - 各身份的有效插件, 来源归属与管理条目, 不保存也不应用
    """
    normalized = prepare_administrator_groups(config, groups, rebind_members)
    normalized_overrides = prepare_administrator_overrides(config, overrides, rebind_overrides)
    service = AdministratorService(lambda: validate_platforms(config.get("platforms", [])), normalized, normalized_overrides)
    catalog = PluginCatalog().scan()
    override_index = {(item["platform_id"], item["user_id"]): item["id"] for item in normalized_overrides}
    identities = {(member["platform_id"], member["user_id"]) for group in normalized for member in group["members"]}
    identities.update(override_index)   # 只由例外授权的人也要出现在预览里
    members = []
    for platform_id, user_id in sorted(identities):
        origin = CallOrigin(platform_id, "", "", "", user_id, "", "")
        plugins = []
        for plugin in catalog:
            if not plugin.permissions.supports_administrators:
                continue
            grant = service.resolve(origin, plugin.name)
            sources = {"allow": [*(f"group:{item}" for item in grant.group_ids),
                                 *(f"override:{item}" for item in grant.allowed_overrides)],
                       "deny": [*(f"group:{item}" for item in grant.excluded_by),
                                *(f"override:{item}" for item in grant.denied_overrides)]}
            # 被否决的插件也要列出, 界面才能展示"允许来源, 否决来源, 最终无效"
            if not sources["allow"] and not sources["deny"]:
                continue
            plugins.append({"name": plugin.name, "allowed": grant.allowed, "group_ids": list(grant.group_ids),
                            "override_ids": list(grant.allowed_overrides), "sources": sources, "permissions": [
                {"id": key, "description": rule.description} for key, rule in plugin.permissions.rules.items() if rule.system_admin]})
        members.append({"platform_id": platform_id, "user_id": user_id,
                        "override_id": override_index.get((platform_id, user_id), ""), "plugins": plugins})
    return {"groups": normalized, "overrides": normalized_overrides, "members": members,
            "section_revision": administrator_revision(normalized, normalized_overrides)}
