"""Edictum 冷配置共享领域服务"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import traceback

from satrap.edictum.plugin_settings import validate_plugin_settings
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_config import PluginConfigManager, validate_config_values
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

from satrap.core.framework.BackGroundManager import ConfigInUseError, ConfigReferenceScanError
from satrap.core.log import logger


class EdictumConfigService:
    """供平台后端和控制服务共用的 Edictum 冷配置服务"""

    def __init__(
        self,
        manager: EdictumConfigManager,
        type_registry: EdictumTypeRegistry,
        *, models: Any = None, rag: Any = None,
        reference_checker: Callable[[str], list[dict[str, str]]] | None = None,
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
        self.models = models
        self.rag = rag
        self.reference_checker = reference_checker

    def _guard_reference(self, name: str) -> None:
        """
        命名配置仍被 Agent 路由绑定时拒绝删除或重命名

        参数:
        - name: 待变更的配置名称
        """

        if self.reference_checker is None:
            return
        try:
            references = self.reference_checker(name)
        except Exception as error:
            logger.error(f"[Agent 配置] edictum/{name} 引用扫描失败: {error}\n{traceback.format_exc()}")
            raise ConfigReferenceScanError("edictum", name, type(error).__name__) from error
        if references:
            logger.warning(f"[Agent 配置] 拒绝变更被引用的 edictum/{name}: {references}")
            raise ConfigInUseError("edictum", name, references)

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
        self._validate_plugin_values(cleaned.get("plugins", []))
        return self.manager.create(name, cleaned)

    def update(self, name: str, payload: object, *, expected_revision: str | None = None) -> tuple[str, dict[str, Any]]:
        """
        更新并按需重命名 Edictum 冷配置

        参数:
        - name: 当前配置名称
        - payload: 待更新字段
        - expected_revision: 可选的完整配置版本, 用于拒绝并发覆盖

        返回:
        - tuple[str, dict[str, Any]]: 最终名称和完整配置
        """
        cleaned = self._validate_payload(payload)
        if "plugins" in cleaned:
            self._validate_plugin_values(cleaned["plugins"])
        new_name = str(cleaned.pop("name")).strip() if "name" in cleaned else None
        from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK

        with REFERENCE_SCAN_LOCK:
            if new_name is not None and new_name != name:
                self._guard_reference(name)
            return self.manager.update(name, cleaned, new_name=new_name, expected_revision=expected_revision)

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
        from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK

        with REFERENCE_SCAN_LOCK:
            self._guard_reference(name)
            return self.manager.delete(name)

    def _validate_plugin_values(self, plugins: list[Any]) -> None:
        """命名配置保存前校验显式参数及继承后的模型和知识库引用"""
        manager = PluginConfigManager()
        for item in plugins:
            values: dict[str, Any] = {}
            if isinstance(item, str):
                name = item
            elif isinstance(item, dict):
                definition = cast(dict[str, Any], item)
                name = definition.get("name", "")
                values = definition.get("config", {})
            else:
                continue
            entry = self.plugin_catalog.get(name)
            if entry is None:
                continue
            values = validate_config_values(entry.config_schema, values)
            effective = manager.resolve(name, entry.config_schema, values)
            validate_plugin_settings(name, entry.config_schema, effective, models=self.models, rag=self.rag)

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
