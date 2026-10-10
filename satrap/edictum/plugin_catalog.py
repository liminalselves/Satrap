"""Edictum 插件目录: 统一扫描插件元数据, 配置结构和能力声明"""
from __future__ import annotations
from dataclasses import dataclass, field, replace
from copy import deepcopy
from pathlib import Path
from typing import Any
import os
import time
import yaml
import json
import hashlib

from satrap.edictum.plugin_compatibility import PluginEnvironment, check_plugin_compatibility, parse_compatibility
from satrap.edictum.plugin_permissions import EMPTY_PERMISSIONS, PluginPermissions, parse_plugin_permissions
from satrap.edictum.plugin_config import ConfigField, parse_config_schema, schema_to_payload
from satrap.core.utils.snapshot_cache import SnapshotCache
from satrap.core.utils.async_worker import RAG_WORKERS
from satrap.edictum.plugin import (
    CAPABILITY_KINDS,
    PLUGINS_PRESET_DIR,
    USER_PLUGINS_DIR,
    load_plugin_meta,
    parse_capability_descriptions,
)

from satrap.core.log import logger


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
    permissions: PluginPermissions = field(default_factory=lambda: EMPTY_PERMISSIONS)
    config_version: int = 0

    def check_environment(self, environment: PluginEnvironment):
        """根据指定环境计算适用性"""
        return check_plugin_compatibility({"compatibility": self.compatibility, "applicability": self.applicability}, environment)

    def to_payload(self) -> dict[str, Any]:
        """转换为前端可使用的插件元数据"""
        return {
            **self.permissions.to_payload(),
            "name": self.name,
            "config_version": self.config_version,
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


@dataclass(frozen=True)
class _CatalogSnapshot:
    """仅供目录内部使用的私有索引与错误快照"""

    entries: dict[str, PluginCatalogEntry]
    errors: list[dict[str, str]]
    fingerprint: tuple[object, ...]


_SNAPSHOTS: SnapshotCache[_CatalogSnapshot] = SnapshotCache()


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
        self.scan_errors: list[dict[str, str]] = []
        self._fixed: _CatalogSnapshot | None = None

    def fingerprint(self) -> tuple[object, ...]:
        """
        获取目录集合及元数据文件指纹, 不解析 YAML

        返回:
        - 包含目录增删及原子替换信息的指纹
        """
        records: list[object] = []
        for base in (self.preset_dir, self.user_dir):
            records.append(os.path.normcase(str(base.resolve())))
            if not base.is_dir():
                records.append(None)
                continue
            for directory in sorted(base.iterdir()):
                if not directory.is_dir():
                    continue
                meta = directory / 'meta.yaml'
                try:
                    info = meta.stat()
                except FileNotFoundError:
                    records.append((directory.name, None))
                    continue
                records.append((directory.name, info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino))
        return tuple(records)

    def invalidate(self) -> None:
        """显式刷新全部进程内目录快照, 已固定的请求快照继续保持原代"""
        _SNAPSHOTS.invalidate()

    def snapshot(self) -> PluginCatalog:
        """
        为一次读取或应用固定同一目录版本

        返回:
        - 从私有索引读取并对外复制的目录对象
        """
        fixed = PluginCatalog(self.preset_dir, self.user_dir)
        fixed._fixed = self._read_snapshot()
        fixed.scan_errors = deepcopy(fixed._fixed.errors)
        return fixed

    def snapshot_token(self) -> str:
        """
        获取已解析快照的版本标识, 固定目录不跟随外部编辑

        返回:
        - 不包含目录路径的快照摘要
        """
        return hashlib.sha256(repr(self._read_snapshot().fingerprint).encode('utf-8')).hexdigest()

    async def snapshot_async(self) -> PluginCatalog:
        """
        异步取得固定目录, 同步服务消费时不再等待同池工作任务

        返回:
        - 当前已核验代次的固定目录
        """
        fixed = PluginCatalog(self.preset_dir, self.user_dir)
        fixed._fixed = self._fixed or await _SNAPSHOTS.get_async(self._resource_key(), self.fingerprint, self._build_snapshot, RAG_WORKERS)
        fixed.scan_errors = deepcopy(fixed._fixed.errors)
        return fixed

    def _resource_key(self) -> tuple[str, str]:
        """
        获取规范化目录组合

        返回:
        - 内置和用户目录的绝对路径
        """
        return (os.path.normcase(str(self.preset_dir.resolve())), os.path.normcase(str(self.user_dir.resolve())))

    @staticmethod
    def _copy_entry(entry: PluginCatalogEntry) -> PluginCatalogEntry:
        """
        复制可变元数据, 权限声明本身不可变且不含授权结果

        参数:
        - entry: 私有目录条目

        返回:
        - 可供调用方独立使用的条目
        """
        return replace(entry, config_schema=deepcopy(entry.config_schema), capabilities=deepcopy(entry.capabilities),
                       compatibility=deepcopy(entry.compatibility), applicability=deepcopy(entry.applicability))

    def _read_snapshot(self) -> _CatalogSnapshot:
        """
        取得固定或按指纹刷新后的私有快照

        返回:
        - 不向目录调用方暴露的索引
        """
        value = self._fixed or _SNAPSHOTS.get(self._resource_key(), self.fingerprint, self._build_snapshot)
        self.scan_errors = deepcopy(value.errors)
        return value

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
        config_schema = parse_config_schema(meta)
        permissions = parse_plugin_permissions(meta, config_schema)
        config_version = meta.get("config_version", 0)
        if type(config_version) is not int or config_version < 0:
            raise ValueError("插件配置版本必须为非负整数")
        return PluginCatalogEntry(
            name=name,
            config_version=config_version,
            path=plugin_dir,
            compatibility=compatibility,
            applicability=applicability,
            version=str(meta.get("version") or ""),
            author=str(meta.get("author") or ""),
            description=str(meta.get("description") or ""),
            config_schema=config_schema,
            permissions=permissions,
            capabilities={
                kind: dict(descriptions.get(kind, {}))
                for kind in CAPABILITY_KINDS
            },
        )

    def _build_snapshot(self) -> _CatalogSnapshot:
        """
        扫描所有合法插件并按名称排序

        返回:
        - 去重后的私有插件索引与错误列表
        """
        started = time.perf_counter()
        source_fingerprint = self.fingerprint()
        found: dict[str, PluginCatalogEntry] = {}
        failures: list[dict[str, str]] = []
        for base in (self.preset_dir, self.user_dir):
            if not base.is_dir():
                continue
            for plugin_dir in sorted(base.iterdir()):
                if not plugin_dir.is_dir() or not (plugin_dir / "meta.yaml").is_file():
                    continue
                try:
                    entry = self._load_entry(plugin_dir)
                    json.dumps(entry.to_payload(), allow_nan=False)
                except (OSError, ValueError, TypeError, yaml.YAMLError, RecursionError) as error:
                    logger.warning(f"[插件目录] 元数据加载失败, 目录={plugin_dir.name}, 原因={error}")
                    failures.append({"name": plugin_dir.name, "error": str(error)})
                    continue
                if entry.name not in found:   # 官方目录先扫描, 同名用户插件不覆盖
                    found[entry.name] = entry
        logger.debug(f"[插件目录] 快照加载完成, 插件={len(found)}, 失败={len(failures)}, elapsed_ms={(time.perf_counter() - started) * 1000:.2f}")
        return _CatalogSnapshot(dict(sorted(found.items())), failures, source_fingerprint)

    def scan(self) -> list[PluginCatalogEntry]:
        """
        按名称返回目录副本, 同代元数据只解析一次

        返回:
        - 去重后的条目副本, 调用方修改不影响缓存
        """
        return [self._copy_entry(entry) for entry in self._read_snapshot().entries.values()]

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
        entry = self._read_snapshot().entries.get(target)
        return self._copy_entry(entry) if entry is not None else None

    def list_payloads(self) -> list[dict[str, Any]]:
        """
        返回全部前端插件元数据

        返回:
        - list[dict[str, Any]]: 插件元数据列表
        """
        return [entry.to_payload() for entry in self.scan()]
