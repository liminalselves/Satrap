"""单次插件应用的目录与全局参数快照, 不用于工具授权或保存事务"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
import hashlib
import traceback

from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.core.log import logger


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
    def capture(cls, catalog: PluginCatalog, manager: PluginConfigManager, *, additional_sources: tuple[Path, ...] = ()) -> PluginApplySnapshot:
        """
        捕获同一代目录与全局参数, 迁移或外部写入导致变化时有界重试

        参数:
        - catalog: 实际插件目录
        - manager: 当前配置来源, 迁移仍由原管理器处理
        - additional_sources: 可选启用状态等来源, 一并核验但不读取为全局参数

        返回:
        - 已核验快照; 损坏来源或持续变化时记录堆栈并抛出
        """
        try:
            for _attempt in range(3):
                catalog_state = catalog.fingerprint()
                fixed = catalog.snapshot()
                entries = fixed.scan()
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
