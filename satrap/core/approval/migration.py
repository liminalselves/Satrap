"""
权限插件拆分时的管理员范围迁移

旧 coding 管理员范围同步到权限插件, 包括成员否决;
文档记录版本后不再推导, 保留用户后续独立修改的结果
"""
from __future__ import annotations

from typing import Any
from copy import deepcopy


def migrate_approval_admin_config(data: dict[str, Any]) -> dict[str, Any]:
    """
    对旧管理员文档生成一次性迁移视图

    参数:
    - data: 原始配置文档, 调用者继续负责结构与身份校验

    返回:
    - 独立配置副本; 新权限范围已有明确配置时以新值为准
    """
    result = deepcopy(data)
    version = result.get("approval_plugin_migration", 0)
    if type(version) is not int or version not in (0, 1):
        raise ValueError("审批插件迁移版本无效")
    if version == 1:
        return result
    groups = result.get("administrator_groups")
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict) or not isinstance(group.get("plugin_scope"), dict):
            continue
        scope = group["plugin_scope"]
        if any(isinstance(scope.get(key), list) and "permissions" in scope[key] for key in ("included", "excluded")):
            continue
        for key in ("included", "excluded"):
            names = scope.get(key)
            if isinstance(names, list) and "satrap_coding" in names:
                names.append("permissions")
    overrides = result.get("administrator_overrides")
    for item in overrides if isinstance(overrides, list) else []:
        if not isinstance(item, dict) or any(isinstance(item.get(key), list) and "permissions" in item[key] for key in ("allow", "deny")):
            continue
        for key in ("allow", "deny"):
            names = item.get(key)
            if isinstance(names, list) and "satrap_coding" in names:
                names.append("permissions")
    result["approval_plugin_migration"] = 1
    return result
