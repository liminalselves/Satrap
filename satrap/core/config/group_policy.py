"""逐群策略字段的校验与有效值解析"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from satrap.core.config.platform_policy import normalize_wake_words, validate_wake_policy
from satrap.core.config.wake_overrides import GROUP_KEYS, resolve_wake_policy_sources, resolve_wake_settings


def policy_values(explicit: Mapping[str, object]) -> dict[str, object]:
    """将无歧义的继承或显式值传输结构转换成运行时覆盖"""
    result: dict[str, object] = {}
    for key, raw in explicit.items():
        if key != "enabled" and key not in GROUP_KEYS:
            raise ValueError(f"群策略不支持字段 {key}")
        if not isinstance(raw, dict) or raw.get("mode") not in {"inherit", "value"}:
            raise ValueError(f"群策略 {key} 缺少有效 mode")
        if raw["mode"] == "inherit":
            if set(raw) != {"mode"}:
                raise ValueError(f"群策略 {key} 继承模式不能携带值")
            continue
        if set(raw) != {"mode", "value"}:
            raise ValueError(f"群策略 {key} 显式模式必须携带值")
        value = raw["value"]
        if key == "enabled":
            if type(value) is not bool:
                raise ValueError("enabled 必须为布尔值")
        else:
            if key in {"wake_words", "wake_aliases"}:
                normalize_wake_words(value)
            validate_wake_policy({key: value})
        result[key] = value
    return result


def resolve_group_policy(
    settings: Mapping[str, Any], group_id: str, explicit: Mapping[str, object],
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """用现有平台与时段解析器合并群显式值并返回同源来源信息"""
    values = policy_values(explicit)
    base = {key: value for key, value in settings.items() if key != "wake_group_overrides"}
    resolved = resolve_wake_settings(base, group_id, now)
    sources = resolve_wake_policy_sources(base, group_id, now)
    for key, value in values.items():
        if key == "enabled":
            continue
        resolved[key] = value
        sources[key] = {"source": "group", "source_index": None,
                        "source_label": f"群 {group_id} 覆盖"}
    return resolved, sources
