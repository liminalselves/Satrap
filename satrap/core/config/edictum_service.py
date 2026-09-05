"""Edictum 冷配置共享领域服务"""
from __future__ import annotations

from typing import Any, cast

from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.registry import EdictumTypeRegistry
from satrap.edictum.config import EdictumConfigManager


_ALLOWED_FIELDS = {
    "name",
    "edictum_type",
    "enabled",
    "description",
    "model_name",
    "params",
    "plugins",
}
"""Edictum 冷配置允许写入的字段"""


class EdictumConfigService:
    """供平台后端和控制服务共用的 Edictum 冷配置服务"""

    def __init__(
        self,
        manager: EdictumConfigManager,
        type_registry: EdictumTypeRegistry,
    ) -> None:
        """
        初始化 Edictum 冷配置服务

        参数:
        - manager: Edictum 冷配置管理器
        - type_registry: Edictum 类型注册表
        """
        self.manager = manager
        self.type_registry = type_registry
        self.plugin_catalog = PluginCatalog()

    def list_types(self) -> list[dict[str, Any]]:
        """
        列出可创建的 Edictum 类型

        返回:
        - list[dict[str, Any]]: 类型能力与配置元数据
        """
        return self.type_registry.list_types()

    def list_configs(self) -> dict[str, dict[str, Any]]:
        """
        列出全部 Edictum 冷配置

        返回:
        - dict[str, dict[str, Any]]: 按名称组织的配置
        """
        return self.manager.list_configs()

    def list_plugins(self) -> list[dict[str, Any]]:
        """
        扫描可供 Edictum 会话使用的插件

        返回:
        - list[dict[str, Any]]: 插件元数据, 配置结构和能力清单
        """
        return self.plugin_catalog.list_payloads()

    def get(self, name: str) -> dict[str, Any] | None:
        """
        获取单个 Edictum 冷配置

        参数:
        - name: 配置名称

        返回:
        - dict[str, Any] | None: 配置不存在时返回 None
        """
        return self.manager.get_config(name)

    def create(self, payload: object) -> dict[str, Any]:
        """
        创建命名 Edictum 冷配置

        参数:
        - payload: 包含 name 和 edictum_type 的配置对象

        返回:
        - dict[str, Any]: 已创建的完整配置
        """
        cleaned = self._validate_payload(payload)
        name = str(cleaned.pop("name", "")).strip()
        if not name:
            raise ValueError("Edictum 配置名称不能为空")
        if not str(cleaned.get("edictum_type", "")).strip():
            raise ValueError("edictum_type 不能为空")
        return self.manager.create(name, cleaned)

    def update(self, name: str, payload: object) -> tuple[str, dict[str, Any]]:
        """
        更新并按需重命名 Edictum 冷配置

        参数:
        - name: 当前配置名称
        - payload: 待更新字段

        返回:
        - tuple[str, dict[str, Any]]: 最终名称和完整配置
        """
        cleaned = self._validate_payload(payload)
        new_name = str(cleaned.pop("name")).strip() if "name" in cleaned else None
        return self.manager.update(name, cleaned, new_name=new_name)

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """
        设置 Edictum 冷配置启用状态

        参数:
        - name: 配置名称
        - enabled: 是否启用

        返回:
        - dict[str, Any]: 更新后的完整配置
        """
        return self.manager.set_enabled(name, enabled)

    def delete(self, name: str) -> bool:
        """
        删除 Edictum 冷配置

        参数:
        - name: 配置名称

        返回:
        - bool: 是否找到并删除配置
        """
        return self.manager.delete(name)

    @staticmethod
    def _validate_payload(payload: object) -> dict[str, Any]:
        """
        校验 API 配置对象和字段类型

        参数:
        - payload: 原始请求体

        返回:
        - dict[str, Any]: 允许传给管理器的字段
        """
        if not isinstance(payload, dict):
            raise ValueError("Edictum 配置必须是对象")
        raw = dict(cast(dict[str, Any], payload))
        unknown = set(raw) - _ALLOWED_FIELDS
        if unknown:
            raise ValueError(f"未知 Edictum 配置字段: {', '.join(sorted(unknown))}")
        if "enabled" in raw and not isinstance(raw["enabled"], bool):
            raise ValueError("enabled 必须是布尔值")
        if "params" in raw and not isinstance(raw["params"], dict):
            raise ValueError("params 必须是对象")
        if "plugins" in raw and not isinstance(raw["plugins"], list):
            raise ValueError("plugins 必须是数组")
        for key in ("name", "edictum_type", "description", "model_name"):
            if key in raw and not isinstance(raw[key], str):
                raise ValueError(f"{key} 必须是字符串")
        return raw
