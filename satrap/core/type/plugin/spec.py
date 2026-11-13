"""插件运行规格数据契约。"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Callable
from satrap.core.type.plugin.catalog import PluginCatalogEntry
from satrap.core.type.plugin.compatibility import CompatibilityResult
from satrap.core.type.plugin_interop import CAPABILITY_KINDS, InteropSpec

PluginConfigResolver = Callable[[PluginCatalogEntry, dict[str, Any]], dict[str, Any]]
"""把插件配置覆盖解析为实际安装配置的回调"""


@dataclass(frozen=True)
class PluginSpec:
    """插件运行时目标规格"""

    name: str
    """目标插件的稳定名称, 用于匹配目录和运行实例"""
    enabled: bool = True
    """是否启用整个插件, 默认启用"""
    config: dict[str, Any] = field(default_factory=dict[str, Any])
    """插件配置覆盖或解析后的安装配置, 默认空映射"""
    capabilities: dict[str, dict[str, bool]] = field(default_factory=dict[str, dict[str, bool]])
    """按类别和能力名记录启用状态, 解析时未配置的已声明能力默认启用"""
    version: str = ""
    """目录中的插件版本, 无可用目录条目时为空串"""
    path: str = ""
    """目录中的插件路径, 无可用目录条目时为空串"""
    resources_revision: str = ""
    """配置引用的模型资源指纹, 未解析或插件停用时可为空串, 变化时参与重载判定"""
    config_resolved: bool = False
    """配置是否已解析为实际生效值, 默认 False, 安装时据此选择配置类型"""
    availability: CompatibilityResult | None = None
    """当前环境的兼容性结果, None 表示尚未附加判定结果"""
    config_version: int = 0
    """持久化配置的版本, 默认 0, 用于识别迁移状态"""
    migration_state: dict[str, Any] = field(default_factory=dict[str, Any])
    """配置迁移时保留的状态记录, 空映射表示没有迁移记录"""
    interop: InteropSpec | None = None
    """插件的交互能力声明, None 表示尚未附加声明"""

    def to_config(self) -> dict[str, Any]:
        """
        转换为可持久化的标准配置对象

        返回:
        - dict[str, Any]: 标准插件配置对象
        """
        return {
            **({"config_version": self.config_version} if self.config_version else {}),
            **({"migration_state": deepcopy(self.migration_state)} if self.migration_state else {}),
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
