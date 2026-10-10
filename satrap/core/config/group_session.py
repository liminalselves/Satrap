"""逐群会话绑定和路由范围的无歧义配置结构"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from satrap.core.config.agent_routing import resolve_agent_binding


def session_values(explicit: Mapping[str, object]) -> dict[str, object]:
    """校验群会话配置并提取显式值"""
    allowed = {"binding", "scope", "model", "prompt", "plugins"}
    if set(explicit) - allowed:
        raise ValueError("群会话配置包含不支持的字段")
    result: dict[str, object] = {}
    for key, raw in explicit.items():
        if not isinstance(raw, dict) or raw.get("mode") not in {"inherit", "value"}:
            raise ValueError(f"会话字段 {key} 的 mode 无效")
        if raw["mode"] == "inherit":
            if set(raw) != {"mode"}:
                raise ValueError(f"会话字段 {key} 继承模式不能携带值")
            continue
        if set(raw) != {"mode", "value"}:
            raise ValueError(f"会话字段 {key} 显式模式必须携带值")
        value = raw["value"]
        if key == "binding":
            if (not isinstance(value, dict) or set(value) != {"provider", "config_name"}
                    or value.get("provider") not in {"session_class", "edictum"}
                    or not isinstance(value.get("config_name"), str)
                    or not value["config_name"] or len(value["config_name"]) > 128):
                raise ValueError("会话绑定必须指定 Provider 和命名配置")
        elif key == "scope":
            if value not in {"group_member", "group_shared"}:
                raise ValueError("会话范围必须为 group_member 或 group_shared")
        elif key == "model":
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ValueError("模型覆盖必须是命名模型引用")
        elif key == "prompt":
            if not isinstance(value, str) or len(value) > 20000:
                raise ValueError("提示词覆盖必须是不超过 20000 字符的文本")
        else:
            if not isinstance(value, list) or len(value) > 64:
                raise ValueError("插件覆盖必须是最多 64 项的列表")
            names: set[str] = set()
            for item in value:
                if (not isinstance(item, dict) or set(item) - {"name", "mode", "config"}
                        or not isinstance(item.get("name"), str) or not item["name"]
                        or len(item["name"]) > 128 or item.get("mode") not in {"enabled", "disabled"}
                        or not isinstance(item.get("config", {}), dict)):
                    raise ValueError("插件覆盖项必须有名称, 启停状态和配置对象")
                if item["name"] in names:
                    raise ValueError("插件覆盖名称重复")
                names.add(item["name"])
        result[key] = value
    return result


def group_binding_chain(platform: Mapping[str, Any], explicit: Mapping[str, object]) -> list[dict[str, str]]:
    """
    展示平台默认, 群聊类型和本群覆盖的完整绑定继承链

    参数:
    - platform: 已解析平台默认配置的快照
    - explicit: 本群显式会话设置

    返回:
    - 各层的绑定, 模式与来源, 继承层显示实际继承结果
    """
    default = {"provider": str(platform.get("session_provider") or "session_class"),
               "config_name": str(platform.get("session_type") or "")}
    by_kind, source = resolve_agent_binding(platform, "group")
    override = session_values(explicit).get("binding")
    selected = override if isinstance(override, dict) else by_kind
    return [
        {"source": "platform", "mode": "value", **default},
        {"source": "conversation_kind", "mode": "value" if source == "conversation_kind" else "inherit", **by_kind},
        {"source": "group", "mode": "value" if isinstance(override, dict) else "inherit",
         "provider": str(selected["provider"]), "config_name": str(selected["config_name"])},
    ]


def resolve_group_session(
    platform: Mapping[str, Any], explicit: Mapping[str, object],
    defaults: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, str]]:
    """合并平台绑定和群显式设置, 保留每项来源"""
    values = session_values(explicit)
    settings = platform.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("平台 settings 无效")
    binding, binding_source = resolve_agent_binding(platform, "group")
    scope: object = settings.get("context_scope", "legacy_user")
    scope_source = "platform"
    if scope == "group":
        scope = "group_shared"
    elif scope == "legacy_user" and platform.get("session_bindings"):
        scope = "group_member"
        scope_source = "conversation_kind"
    effective: dict[str, object] = {"binding": binding, "scope": scope}
    sources = {"binding": binding_source, "scope": scope_source}
    for key in ("model", "prompt", "plugins"):
        if defaults is not None and key in defaults:
            effective[key] = defaults[key]
            sources[key] = "named_config"
    for key, value in values.items():
        if key == "plugins":
            base = effective.get("plugins", [])
            if not isinstance(value, list):
                raise ValueError("插件覆盖必须是数组")
            merged: dict[str, dict[str, object]] = {}
            if isinstance(base, list):
                for item in base:
                    if isinstance(item, str):
                        merged[item] = {"name": item, "enabled": True, "config": {}}
                    elif isinstance(item, dict) and isinstance(item.get("name"), str):
                        merged[item["name"]] = dict(item)
            for item in value:
                if not isinstance(item, dict):
                    continue
                previous = merged.get(item["name"], {"name": item["name"], "config": {}})
                previous_config = previous.get("config", {})
                current_config = item.get("config", {})
                merged[item["name"]] = {
                    **previous, "enabled": item["mode"] == "enabled",
                    "config": {**(previous_config if isinstance(previous_config, dict) else {}),
                               **(current_config if isinstance(current_config, dict) else {})},
                }
            effective[key] = list(merged.values())
            sources[key] = "group"
            continue
        effective[key] = value
        sources[key] = "group"
    return effective, sources
