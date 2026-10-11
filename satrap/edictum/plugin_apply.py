"""单次插件应用的目录与全局参数快照, 不用于工具授权或保存事务"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, TYPE_CHECKING, Callable
import hashlib
import traceback
import time

from satrap.edictum.plugin_catalog import PluginCatalog, PluginCatalogEntry
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.core.log import logger
from satrap.edictum.plugin_spec import PluginSpec, parse_plugin_specs
from satrap.core.config.model_snapshot import ModelConfigSnapshot
from satrap.edictum.plugin_resources import MODEL_TYPES

if TYPE_CHECKING:
    from satrap.edictum.config import EdictumConfigManager
    from satrap.core.framework.BackGroundManager import ModelConfigManager


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
                 sources: dict[Path, tuple[int, int, int, int] | None], catalog_state: tuple[object, ...],
                 models: dict[Path, ModelConfigSnapshot] | None = None) -> None:
        """
        保存已核验的来源与有效全局参数

        参数:
        - catalog: 已固定目录版本
        - values: 默认值与全局显式字段的合成结果
        - sources: 捕获结束的文件状态
        - catalog_state: 捕获结束的目录状态
        - models: 同代模型来源, 不捕获调用授权或客户端
        """
        self.catalog = catalog
        self._values = deepcopy(values)
        self._sources = dict(sources)
        self._catalog_state = catalog_state
        self._models = dict(models or {})

    @classmethod
    def capture(cls, catalog: PluginCatalog, manager: PluginConfigManager, *, additional_sources: tuple[Path, ...] = (),
                plugin_names: set[str] | None = None, model_managers: tuple[ModelConfigManager, ...] = (),
                model_plugin_filter: Callable[[PluginCatalogEntry], bool] | None = None) -> PluginApplySnapshot:
        """
        捕获同一代目录与全局参数, 迁移或外部写入导致变化时有界重试

        参数:
        - catalog: 实际插件目录
        - manager: 当前配置来源, 迁移仍由原管理器处理
        - additional_sources: 可选启用状态等来源, 一并核验但不读取为全局参数
        - plugin_names: 可选参数读取范围, None 时读取全部目录插件
        - model_managers: 可选资源模型来源, 同一路径只解析一次
        - model_plugin_filter: 可选模型引用插件过滤, 在固定目录中决定是否需要模型来源

        返回:
        - 已核验快照; 损坏来源或持续变化时记录堆栈并抛出
        """
        try:
            for _attempt in range(3):
                catalog_state = catalog.fingerprint()
                fixed = catalog.snapshot()
                entries = [entry for entry in fixed.scan() if plugin_names is None or entry.name in plugin_names]
                paths = {entry.name: manager._global_path(entry.name) for entry in entries}
                needs_models = model_plugin_filter is None or any(
                    model_plugin_filter(entry) and any(field.type in MODEL_TYPES for field in entry.config_schema.values())
                    for entry in entries
                )
                model_sources = {item.storage_path.absolute(): item for item in model_managers} if needs_models else {}
                sources = [*paths.values(), *additional_sources, *model_sources]
                before = {path: plugin_source_version(path) for path in sources}
                models = {path: ModelConfigSnapshot.read(item) for path, item in model_sources.items()}
                values = {
                    entry.name: {**{key: deepcopy(field.default) for key, field in entry.config_schema.items()},
                                 **manager.load_global_explicit(entry.name, entry.config_schema)}
                    for entry in entries
                }
                after = {path: plugin_source_version(path) for path in sources}
                if before == after and catalog_state == catalog.fingerprint():
                    return cls(fixed, values, {path: state[0] for path, state in after.items()}, catalog_state, models)
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

    def models_for(self, manager: ModelConfigManager | None) -> ModelConfigSnapshot | None:
        """
        选择本次捕获的模型来源, 不回退未捕获的实时文件

        参数:
        - manager: 实例所属模型管理器

        返回:
        - 对应只读来源; 来源未捕获时明确失败
        """
        return self._models[manager.storage_path.absolute()] if manager is not None else None

    @property
    def has_model_sources(self) -> bool:
        """本次是否包含模型引用来源"""
        return bool(self._models)


class EdictumPluginApplySnapshot:
    """平台同次应用共享的命名规格与全局参数, 不包含实例覆盖或授权"""

    def __init__(self, sources: PluginApplySnapshot, specs: dict[str, list[PluginSpec]], read_ms: float,
                 configs: dict[str, dict[str, Any] | None]) -> None:
        """
        保存独立命名规格及来源核验器

        参数:
        - sources: 目录与全局参数来源
        - specs: 本次选定命名配置的规格
        - read_ms: 实际捕获耗时
        - configs: 对应完整命名配置, 已删除配置保存为 None
        """
        self.sources = sources
        self._specs = deepcopy(specs)
        self.read_ms = read_ms
        self._configs = deepcopy(configs)

    @classmethod
    def capture(cls, manager: EdictumConfigManager, config_names: set[str], *, desired_plugins: object | None = None,
                global_manager: PluginConfigManager | None = None, plugin_name: str | None = None,
                retained_config_names: set[str] | None = None, desired_config: dict[str, Any] | None = None,
                model_managers: tuple[ModelConfigManager, ...] = (), include_main_models: bool = True,
                model_config_scopes: dict[Path, set[str]] | None = None) -> EdictumPluginApplySnapshot:
        """
        在线程中统一刷新命名配置并捕获所需插件参数

        参数:
        - manager: 当前命名配置管理器
        - config_names: 已按会话与插件引用选定的配置名称
        - desired_plugins: 可选临时目标, None 时采用各命名配置
        - global_manager: 可选全局参数来源, 默认使用当前项目目录
        - plugin_name: 可选插件引用过滤, 在新命名内容中判断
        - retained_config_names: 已运行该插件的配置名称, 移除旧引用仍需协调
        - desired_config: 可选完整临时运行目标, 校验后仅用于本次应用
        - model_managers: 运行应用的模型文件来源, 同一路径只解析一次
        - include_main_models: 完整 Agent 应用读取主模型, 仅插件应用按资源 schema 判断
        - model_config_scopes: 模型文件对应的候选 Agent 名称, 排除未进入本次目标的来源

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
                if desired_config is not None:
                    target = manager._normalize_entry(desired_config, catalog)
                    configs.update({name: deepcopy(target) for name in config_names})
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
                active_names = {name for name in specs if configs.get(name) is not None and configs[name].get("enabled", True)}
                required_models = active_names if include_main_models else {name for name in active_names if any(
                    spec.enabled and (entry := catalog.get(spec.name)) is not None
                    and any(field.type in MODEL_TYPES for field in entry.config_schema.values()) for spec in specs[name]
                )}
                selected_models = tuple(item for item in model_managers if required_models and (
                    model_config_scopes is None or required_models.intersection(model_config_scopes.get(item.storage_path.absolute(), set()))
                ))
                sources = PluginApplySnapshot.capture(manager.plugin_catalog, globals_, additional_sources=(manager.storage_path,),
                                                      plugin_names=names, model_managers=selected_models)
                if (before == plugin_source_version(manager.storage_path) and sources.is_current()
                        and catalog.snapshot_token() == sources.catalog.snapshot_token()):
                    return cls(sources, specs, (time.perf_counter() - started) * 1000, {name: configs.get(name) for name in specs})
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

    def config_for(self, config_name: str) -> dict[str, Any] | None:
        """
        返回同代完整命名目标, 不回退当前文件或旧内存内容

        参数:
        - config_name: 本次捕获的命名配置

        返回:
        - 独立配置副本, 已删除时为 None; 未捕获名称明确失败
        """
        return deepcopy(self._configs[config_name])

    def models_for(self, manager: ModelConfigManager | None) -> ModelConfigSnapshot | None:
        """
        选择本次捕获的模型来源, 不回退未捕获的实时文件

        参数:
        - manager: 实例所属模型管理器, 没有管理器时不提供模型服务

        返回:
        - 对应只读模型来源; 来源未捕获时明确失败
        """
        return self.sources.models_for(manager)

    @property
    def has_model_sources(self) -> bool:
        """本次是否捕获模型来源"""
        return self.sources.has_model_sources
