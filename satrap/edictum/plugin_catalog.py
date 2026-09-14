"""Edictum 插件目录: 统一扫描插件元数据, 配置结构和能力声明"""
from __future__ import annotations
from satrap.edictum.plugin_compatibility import PluginEnvironment, check_plugin_compatibility, parse_compatibility

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from satrap.edictum.plugin_config import ConfigField, parse_config_schema, schema_to_payload
from satrap.edictum.plugin import (
    CAPABILITY_KINDS,
    PLUGINS_PRESET_DIR,
    USER_PLUGINS_DIR,
    load_plugin_meta,
    parse_capability_descriptions,
)


@dataclass(frozen=True)
class PluginCatalogEntry:
    """一个可用插件的稳定目录条目"""

    name: str
    path: Path
    version: str = ""
    author: str = ""
    description: str = ""
    config_schema: dict[str, ConfigField] = field(default_factory=dict[str, ConfigField])
    capabilities: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    compatibility: dict[str, Any] = field(default_factory=dict[str, Any])
    applicability: dict[str, Any] = field(default_factory=dict[str, Any])

    def check_environment(self, environment: PluginEnvironment):
        """根据指定环境计算适用性"""
        return check_plugin_compatibility({"compatibility": self.compatibility, "applicability": self.applicability}, environment)

    def to_payload(self) -> dict[str, Any]:
        """转换为前端可使用的插件元数据"""
        return {
            "name": self.name,
            "version": self.version,
            "compatibility": dict(self.compatibility),
            "applicability": dict(self.applicability),
            "author": self.author,
            "description": self.description,
            "config_schema": schema_to_payload(self.config_schema),
            "capabilities": {
                kind: dict(self.capabilities.get(kind, {}))
                for kind in CAPABILITY_KINDS
            },
        }


class PluginCatalog:
    """统一插件目录, 官方插件优先于同名用户插件"""

    def __init__(
        self,
        preset_dir: str | Path | None = None,
        user_dir: str | Path | None = None,
    ) -> None:
        """
        初始化插件目录

        参数:
        - preset_dir: 官方插件目录
        - user_dir: 用户插件目录
        """
        self.preset_dir = Path(preset_dir) if preset_dir is not None else PLUGINS_PRESET_DIR
        self.user_dir = Path(user_dir) if user_dir is not None else USER_PLUGINS_DIR

    @staticmethod
    def _load_entry(plugin_dir: Path) -> PluginCatalogEntry:
        """
        从插件目录读取目录条目

        参数:
        - plugin_dir: 插件目录

        返回:
        - PluginCatalogEntry: 插件目录条目
        """
        meta = load_plugin_meta(plugin_dir)
        name = str(meta.get("name") or "").strip()
        if not name:
            raise ValueError(f"插件目录缺少合法 name: {plugin_dir}")
        compatibility, applicability = parse_compatibility(meta)
        descriptions = parse_capability_descriptions(meta)
        return PluginCatalogEntry(
            name=name,
            path=plugin_dir,
            compatibility=compatibility,
            applicability=applicability,
            version=str(meta.get("version") or ""),
            author=str(meta.get("author") or ""),
            description=str(meta.get("description") or ""),
            config_schema=parse_config_schema(meta),
            capabilities={
                kind: dict(descriptions.get(kind, {}))
                for kind in CAPABILITY_KINDS
            },
        )

    def scan(self) -> list[PluginCatalogEntry]:
        """
        扫描所有合法插件并按名称排序

        返回:
        - list[PluginCatalogEntry]: 去重后的插件目录条目
        """
        found: dict[str, PluginCatalogEntry] = {}
        for base in (self.preset_dir, self.user_dir):
            if not base.is_dir():
                continue
            for plugin_dir in sorted(base.iterdir()):
                if not plugin_dir.is_dir() or not (plugin_dir / "meta.yaml").is_file():
                    continue
                try:
                    entry = self._load_entry(plugin_dir)
                except ValueError:
                    continue
                if entry.name not in found:   # 官方目录先扫描, 同名用户插件不覆盖
                    found[entry.name] = entry
        return [found[name] for name in sorted(found)]

    def get(self, name: str) -> PluginCatalogEntry | None:
        """
        按插件元数据名称获取目录条目

        参数:
        - name: 插件名称

        返回:
        - PluginCatalogEntry | None: 插件不存在时返回 None
        """
        target = name.strip()
        if not target:
            return None
        return next((entry for entry in self.scan() if entry.name == target), None)

    def list_payloads(self) -> list[dict[str, Any]]:
        """
        返回全部前端插件元数据

        返回:
        - list[dict[str, Any]]: 插件元数据列表
        """
        return [entry.to_payload() for entry in self.scan()]
