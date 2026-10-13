"""逐群会话绑定和路由范围的无歧义配置结构"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from satrap.core.config.agent_routing import resolve_agent_binding


@dataclass(frozen=True)
class GroupRouteSnapshot:
    """已解码的群路由, 保留配置修订与运行代次的不同含义"""

    generation: int
    revision: int | None
    binding: dict[str, str] | None
    context_scope: str | None
    overrides: dict[str, object]


def decode_group_route(snapshot: object, *, error: Callable[[], Exception],
                       with_revision: bool = False, strict_generation: bool = True) -> GroupRouteSnapshot:
    """
    解码适配器群路由并共用显式绑定, 范围和运行覆盖的提取规则

    参数:
    - snapshot: 适配器返回的群设置与代次元组
    - error: 调用入口原有的快照格式异常
    - with_revision: 事件入口是否兼容带配置修订的三项元组
    - strict_generation: 是否拒绝 bool 和 int 子类, 事件旧契约保留 isinstance 规则

    返回:
    - 已核验的路由信息, 配置字段错误仍由 session_values 原样抛出
    """
    if not isinstance(snapshot, tuple) or len(snapshot) not in ({2, 3} if with_revision else {2}):
        raise error()
    settings, generation = snapshot[:2]
    if not isinstance(settings, dict) or (type(generation) is not int if strict_generation else not isinstance(generation, int)):
        raise error()
    revision = snapshot[2] if len(snapshot) == 3 and isinstance(snapshot[2], int) else None
    values = session_values(settings)
    binding = values.get("binding")
    scope = values.get("scope")
    return GroupRouteSnapshot(
        generation, revision,
        {"provider": str(binding["provider"]), "config_name": str(binding["config_name"])} if isinstance(binding, dict) else None,
        "group" if scope == "group_shared" else "group_member" if scope == "group_member" else None,
        {key: value for key, value in values.items() if key in {"model", "prompt", "plugins"}},
    )


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
