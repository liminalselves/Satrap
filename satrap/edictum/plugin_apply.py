"""单次插件应用的目录与全局参数快照, 不用于工具授权或保存事务"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, TYPE_CHECKING
import hashlib
import traceback
import time

from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.core.log import logger
from satrap.edictum.plugin_spec import PluginSpec, parse_plugin_specs

if TYPE_CHECKING:
    from satrap.edictum.config import EdictumConfigManager


def _file_state(path: Path) -> tuple[int, int, int, int] | None:
    """
    读取来源状态, 缺失与不可读取分别处理

    参数:
    - path: 配置文件路径

    返回:
    - 文件时间, 大小与身份; 缺失时为 None, 其它读取错误抛出
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino


def _file_digest(path: Path) -> str | None:
    """
    核验捕获前后内容, 不依赖仅有时间戳的相等判断

    参数:
    - path: 配置文件路径

    返回:
    - 内容摘要, 缺失时为 None; 不返回配置正文
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def plugin_source_version(path: Path) -> tuple[tuple[int, int, int, int] | None, str | None]:
    """
    取得捕获前后的完整来源标识

    参数:
    - path: 插件状态或参数来源

    返回:
    - 文件身份与内容摘要, 仅供应用内部核验
    """
    return _file_state(path), _file_digest(path)


class PluginApplySnapshot:
    """只在一次应用中使用的固定来源, 返回独立参数副本"""

    def __init__(self, catalog: PluginCatalog, values: dict[str, dict[str, Any]],
                 sources: dict[Path, tuple[int, int, int, int] | None], catalog_state: tuple[object, ...]) -> None:
        """
        保存已核验的来源与有效全局参数

        参数:
        - catalog: 已固定目录版本
        - values: 默认值与全局显式字段的合成结果
        - sources: 捕获结束的文件状态
        - catalog_state: 捕获结束的目录状态
        """
        self.catalog = catalog
        self._values = deepcopy(values)
        self._sources = dict(sources)
        self._catalog_state = catalog_state

    @classmethod
    def capture(cls, catalog: PluginCatalog, manager: PluginConfigManager, *, additional_sources: tuple[Path, ...] = (), plugin_names: set[str] | None = None) -> PluginApplySnapshot:
        """
        捕获同一代目录与全局参数, 迁移或外部写入导致变化时有界重试

        参数:
        - catalog: 实际插件目录
        - manager: 当前配置来源, 迁移仍由原管理器处理
        - additional_sources: 可选启用状态等来源, 一并核验但不读取为全局参数
        - plugin_names: 可选参数读取范围, None 时读取全部目录插件

        返回:
        - 已核验快照; 损坏来源或持续变化时记录堆栈并抛出
        """
        try:
            for _attempt in range(3):
                catalog_state = catalog.fingerprint()
                fixed = catalog.snapshot()
                entries = [entry for entry in fixed.scan() if plugin_names is None or entry.name in plugin_names]
                paths = {entry.name: manager._global_path(entry.name) for entry in entries}
                sources = [*paths.values(), *additional_sources]
                before = {path: plugin_source_version(path) for path in sources}
                values = {
                    entry.name: {**{key: deepcopy(field.default) for key, field in entry.config_schema.items()},
                                 **manager.load_global_explicit(entry.name, entry.config_schema)}
                    for entry in entries
                }
                after = {path: plugin_source_version(path) for path in sources}
                if before == after and catalog_state == catalog.fingerprint():
                    return cls(fixed, values, {path: state[0] for path, state in after.items()}, catalog_state)
            raise RuntimeError("插件配置在捕获期间持续变化, 请重试应用")
        except Exception as error:
            logger.error(f"[插件应用] 捕获全局参数失败: {error}\n{traceback.format_exc()}")
            raise

    def global_config(self, name: str) -> dict[str, Any]:
        """
        返回固定版本的有效全局参数

        参数:
        - name: 捕获目录中的插件名称

        返回:
        - 独立参数副本; 未捕获名称明确失败, 不回退实时读取
        """
        return deepcopy(self._values[name])

    def is_current(self) -> bool:
        """
        在实例锁内核验排队期间的来源替换, 不读取正文或权限决策

        返回:
        - 目录与全部捕获文件仍匹配时为 True; 文件读取错误由协调入口捕获
        """
        return self.catalog.fingerprint() == self._catalog_state and all(
            _file_state(path) == expected for path, expected in self._sources.items()
        )


class EdictumPluginApplySnapshot:
    """平台同次应用共享的命名规格与全局参数, 不包含实例覆盖或授权"""

    def __init__(self, sources: PluginApplySnapshot, specs: dict[str, list[PluginSpec]], read_ms: float) -> None:
        """
        保存独立命名规格及来源核验器

        参数:
        - sources: 目录与全局参数来源
        - specs: 本次选定命名配置的规格
        - read_ms: 实际捕获耗时
        """
        self.sources = sources
        self._specs = deepcopy(specs)
        self.read_ms = read_ms

    @classmethod
    def capture(cls, manager: EdictumConfigManager, config_names: set[str], *, desired_plugins: object | None = None,
                global_manager: PluginConfigManager | None = None, plugin_name: str | None = None,
                retained_config_names: set[str] | None = None) -> EdictumPluginApplySnapshot:
        """
        在线程中统一刷新命名配置并捕获所需插件参数

        参数:
        - manager: 当前命名配置管理器
        - config_names: 已按会话与插件引用选定的配置名称
        - desired_plugins: 可选临时目标, None 时采用各命名配置
        - global_manager: 可选全局参数来源, 默认使用当前项目目录
        - plugin_name: 可选插件引用过滤, 在新命名内容中判断
        - retained_config_names: 已运行该插件的配置名称, 移除旧引用仍需协调

        返回:
        - 同一代来源; 持续变化或读取失败时记录堆栈并抛出
        """
        started = time.perf_counter()
        globals_ = global_manager or PluginConfigManager()
        try:
            for _attempt in range(3):
                before = plugin_source_version(manager.storage_path)
                manager.reload()
                catalog = manager.plugin_catalog.snapshot()
                configs = manager.list_configs()
                specs = {
                    name: parse_plugin_specs(desired_plugins if desired_plugins is not None else configs.get(name, {}).get("plugins", []),
                                             catalog, require_available=False, migration_manager=globals_)
                    for name in config_names
                }
                if plugin_name is not None:
                    specs = {name: items for name, items in specs.items()
                             if name in (retained_config_names or set()) or any(spec.name == plugin_name for spec in items)}
                names = {spec.name for items in specs.values() for spec in items}
                if "base_take" in names:
                    names.add("memory")   # 实例旧记忆覆盖可能在运行目标合成时迁移为独立插件
                sources = PluginApplySnapshot.capture(manager.plugin_catalog, globals_, additional_sources=(manager.storage_path,), plugin_names=names)
                if (before == plugin_source_version(manager.storage_path) and sources.is_current()
                        and catalog.snapshot_token() == sources.catalog.snapshot_token()):
                    return cls(sources, specs, (time.perf_counter() - started) * 1000)
            raise RuntimeError("Edictum 插件配置在捕获期间持续变化, 请重试应用")
        except Exception as error:
            logger.error(f"[插件应用] 捕获 Edictum 命名配置失败: {error}\n{traceback.format_exc()}")
            raise

    def specs_for(self, config_name: str) -> list[PluginSpec]:
        """
        返回选定命名配置的独立规格

        参数:
        - config_name: 本次已捕获配置名称

        返回:
        - 规格副本; 未捕获配置明确失败, 不沿用实时内容
        """
        return deepcopy(self._specs[config_name])

    def includes_config(self, config_name: str) -> bool:
        """
        检查命名配置是否属于已捕获应用范围

        参数:
        - config_name: 候选命名配置

        返回:
        - 被捕获时为 True, 未引用目标插件的配置不进入协调
        """
        return config_name in self._specs
