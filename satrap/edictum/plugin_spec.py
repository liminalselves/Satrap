"""插件运行规格: 统一 Chat 与平台 Edictum 的配置结构和指纹"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable, cast

from satrap.edictum.plugin import CAPABILITY_KINDS
from satrap.edictum.plugin_catalog import PluginCatalog, PluginCatalogEntry


PluginConfigResolver = Callable[[PluginCatalogEntry, dict[str, Any]], dict[str, Any]]
"""把插件配置覆盖解析为实际安装配置的回调"""


@dataclass(frozen=True)
class PluginSpec:
    """插件运行时目标规格"""

    name: str
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict[str, Any])
    capabilities: dict[str, dict[str, bool]] = field(default_factory=dict[str, dict[str, bool]])
    version: str = ""
    path: str = ""

    def to_config(self) -> dict[str, Any]:
        """
        转换为可持久化的标准配置对象

        返回:
        - dict[str, Any]: 标准插件配置对象
        """
        return {
            "name": self.name,
            "enabled": self.enabled,
            "config": dict(self.config),
            "capabilities": {
                kind: dict(self.capabilities.get(kind, {}))
                for kind in CAPABILITY_KINDS
            },
        }

    def with_config(self, config: dict[str, Any]) -> PluginSpec:
        """
        返回替换安装配置后的新规格

        参数:
        - config: 已解析的安装配置

        返回:
        - PluginSpec: 新插件规格
        """
        return replace(self, config=dict(config))


def _normalize_capabilities(
    raw: object,
    entry: PluginCatalogEntry | None,
) -> dict[str, dict[str, bool]]:
    """
    规范化五类子能力状态, 已声明但未配置的能力默认启用

    参数:
    - raw: 原始能力配置
    - entry: 可选插件目录条目

    返回:
    - dict[str, dict[str, bool]]: 完整能力状态
    """
    if raw is None:
        source: dict[str, Any] = {}
    elif isinstance(raw, dict):
        source = dict(cast(dict[str, Any], raw))
    else:
        raise ValueError("capabilities 必须是对象")
    unknown_kinds = set(source) - set(CAPABILITY_KINDS)
    if unknown_kinds:
        raise ValueError(f"未知插件能力类别: {', '.join(sorted(unknown_kinds))}")

    normalized: dict[str, dict[str, bool]] = {}
    for kind in CAPABILITY_KINDS:
        declared = dict(entry.capabilities.get(kind, {})) if entry is not None else {}
        values: dict[str, bool] = {name: True for name in declared}
        raw_values = source.get(kind, {})
        if not isinstance(raw_values, dict):
            raise ValueError(f"capabilities.{kind} 必须是对象")
        for cap_name, enabled in cast(dict[str, Any], raw_values).items():
            name = str(cap_name).strip()
            if not name:
                raise ValueError(f"capabilities.{kind} 包含空能力名称")
            if entry is not None and name not in declared:
                raise ValueError(f"插件 {entry.name} 未声明能力: {kind}.{name}")
            if not isinstance(enabled, bool):
                raise ValueError(f"插件能力状态必须是布尔值: {kind}.{name}")
            values[name] = enabled
        normalized[kind] = values
    return normalized


def parse_plugin_specs(
    value: object,
    catalog: PluginCatalog,
    *,
    require_available: bool = False,
    config_resolver: PluginConfigResolver | None = None,
) -> list[PluginSpec]:
    """
    把字符串或对象插件列表解析为统一运行规格

    参数:
    - value: 插件配置数组
    - catalog: 插件目录
    - require_available: 是否拒绝目录中不存在的插件
    - config_resolver: 可选安装配置解析器

    返回:
    - list[PluginSpec]: 标准插件运行规格
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("plugins 必须是数组")
    specs: list[PluginSpec] = []
    names: set[str] = set()
    for raw_item in cast(list[object], value):
        if isinstance(raw_item, str):
            item: dict[str, Any] = {"name": raw_item}
        elif isinstance(raw_item, dict):
            item = dict(cast(dict[str, Any], raw_item))
        else:
            raise ValueError("plugins 项必须是名称或对象")
        unknown_fields = set(item) - {"name", "enabled", "config", "capabilities"}
        if unknown_fields:
            raise ValueError(f"未知插件配置字段: {', '.join(sorted(unknown_fields))}")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("插件名称不能为空")
        if name in names:
            raise ValueError(f"插件配置重复: {name}")
        names.add(name)
        entry = catalog.get(name)
        if entry is None and require_available:
            raise ValueError(f"插件不存在: {name}")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"插件 enabled 必须是布尔值: {name}")
        raw_config = item.get("config", {})
        if not isinstance(raw_config, dict):
            raise ValueError(f"插件 config 必须是对象: {name}")
        config: dict[str, Any] = {}
        for key, raw_value in cast(dict[str, Any], raw_config).items():
            if entry is None:
                config[str(key)] = raw_value
                continue
            field_definition = entry.config_schema.get(str(key))
            if field_definition is None:
                raise ValueError(f"插件 {name} 未声明配置项: {key}")
            config[str(key)] = field_definition.validate(raw_value)
        if entry is not None and config_resolver is not None:
            config = config_resolver(entry, config)
        specs.append(
            PluginSpec(
                name=name,
                enabled=enabled,
                config=config,
                capabilities=_normalize_capabilities(item.get("capabilities"), entry),
                version=entry.version if entry is not None else "",
                path=str(entry.path) if entry is not None else "",
            )
        )
    return specs


def plugin_specs_fingerprint(specs: list[PluginSpec]) -> str:
    """
    计算插件运行规格的稳定指纹

    参数:
    - specs: 插件运行规格

    返回:
    - str: SHA-256 指纹
    """
    payload: list[dict[str, Any]] = [
        {
            "name": spec.name,
            "enabled": spec.enabled,
            "config": spec.config,
            "capabilities": spec.capabilities,
            "version": spec.version,
        }
        for spec in sorted(specs, key=lambda item: item.name)
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
