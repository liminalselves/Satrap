"""会话类配置共享领域服务"""
from __future__ import annotations

from typing import Any, cast

from satrap.core.framework.SessionClassManager import SessionClassConfigManager


_ALLOWED_FIELDS = {
    "name",
    "class_path",
    "is_async",
    "enabled",
    "context_key",
    "model_key",
    "description",
    "params",
}
"""会话类配置允许写入的字段"""


class SessionClassConfigService:
    """供平台后端和控制服务共用的会话类配置增删改查服务"""

    def __init__(self, manager: SessionClassConfigManager) -> None:
        """
        初始化会话类配置服务

        参数:
        - manager: 会话类配置管理器
        """
        self.manager = manager

    def list_configs(self) -> dict[str, dict[str, Any]]:
        """
        列出全部会话类配置

        返回:
        - dict[str, dict[str, Any]]: 按名称组织的配置
        """
        return self.manager.list_configs()

    def get(self, name: str) -> dict[str, Any] | None:
        """
        获取单个会话类配置

        参数:
        - name: 配置名称

        返回:
        - dict[str, Any] | None: 配置不存在时返回 None
        """
        return self.manager.get_config(self._validate_name(name))

    def create(self, payload: object) -> dict[str, Any]:
        """
        冷创建会话类配置

        创建过程只写入配置文件, 不导入或实例化 class_path 指向的类

        参数:
        - payload: 会话类配置字段

        返回:
        - dict[str, Any]: 已创建的完整配置
        """
        cleaned = self._validate_payload(payload)
        name = self._validate_name(cleaned.pop("name", ""))
        class_path = self._validate_class_path(cleaned.pop("class_path", ""))
        return self.manager.register_config_entry(
            name,
            class_path,
            is_async=cast(bool, cleaned.pop("is_async", False)),
            enabled=cast(bool, cleaned.pop("enabled", True)),
            context_key=cast(str, cleaned.pop("context_key", "")),
            model_key=cast(str, cleaned.pop("model_key", "")),
            description=cast(str, cleaned.pop("description", "")),
            params=cast(dict[str, Any], cleaned.pop("params", {})),
        )

    def update(self, name: str, payload: object) -> dict[str, Any]:
        """
        原子更新并按需重命名会话类配置

        参数:
        - name: 当前配置名称
        - payload: 待更新字段

        返回:
        - dict[str, Any]: 更新后的完整配置
        """
        current_name = self._validate_name(name)
        cleaned = self._validate_payload(payload)
        new_name = self._validate_name(cleaned.pop("name")) if "name" in cleaned else None
        class_path = (
            self._validate_class_path(cleaned.pop("class_path"))
            if "class_path" in cleaned
            else None
        )
        return self.manager.update_entry(
            current_name,
            new_name=new_name,
            class_path=class_path,
            is_async=cast(bool, cleaned["is_async"]) if "is_async" in cleaned else None,
            enabled=cast(bool, cleaned["enabled"]) if "enabled" in cleaned else None,
            params=cast(dict[str, Any], cleaned["params"]) if "params" in cleaned else None,
            description=cast(str, cleaned["description"]) if "description" in cleaned else None,
            context_key=cast(str, cleaned["context_key"]) if "context_key" in cleaned else None,
            model_key=cast(str, cleaned["model_key"]) if "model_key" in cleaned else None,
        )

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """
        设置会话类配置启用状态

        参数:
        - name: 配置名称
        - enabled: 是否启用

        返回:
        - dict[str, Any]: 更新后的完整配置
        """
        return self.manager.update_entry(self._validate_name(name), enabled=enabled)

    def delete(self, name: str) -> bool:
        """
        删除会话类配置

        参数:
        - name: 配置名称

        返回:
        - bool: 是否找到并删除配置
        """
        return self.manager.remove_config(self._validate_name(name))

    @staticmethod
    def _validate_name(value: object) -> str:
        """
        校验配置名称

        参数:
        - value: 原始名称

        返回:
        - str: 去除首尾空格后的名称
        """
        name = str(value or "").strip()
        if not name:
            raise ValueError("会话类配置名称不能为空")
        return name

    @staticmethod
    def _validate_class_path(value: object) -> str:
        """
        校验类路径

        参数:
        - value: 原始类路径

        返回:
        - str: 去除首尾空格后的类路径
        """
        class_path = str(value or "").strip()
        if not class_path or "." not in class_path:
            raise ValueError("class_path 必须是完整类路径")
        return class_path

    @staticmethod
    def _validate_payload(payload: object) -> dict[str, Any]:
        """
        校验会话类配置字段

        参数:
        - payload: 原始配置字段

        返回:
        - dict[str, Any]: 已校验字段
        """
        if not isinstance(payload, dict):
            raise ValueError("会话类配置必须是对象")
        cleaned = dict(cast(dict[str, Any], payload))
        unknown = sorted(set(cleaned) - _ALLOWED_FIELDS)
        if unknown:
            raise ValueError(f"未知会话类配置字段: {', '.join(unknown)}")
        for field_name in ("is_async", "enabled"):
            if field_name in cleaned and not isinstance(cleaned[field_name], bool):
                raise ValueError(f"{field_name} 必须是布尔值")
        for field_name in ("context_key", "model_key", "description"):
            if field_name in cleaned and not isinstance(cleaned[field_name], str):
                raise ValueError(f"{field_name} 必须是字符串")
        if "params" in cleaned and not isinstance(cleaned["params"], dict):
            raise ValueError("params 必须是对象")
        return cleaned
