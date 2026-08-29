"""
Edictum 命名会话冷配置管理器

只负责持久化和校验命名配置, 不创建运行时会话,
因此控制服务可在后端尚未启动时完成增删改查
"""
from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from satrap.core.log import logger
from satrap.core.utils.paths import get_data_dir
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_spec import parse_plugin_specs
from satrap.edictum.registry import EDICTUM_PROVIDER, EdictumTypeRegistry


class EdictumConfigManager:
    """管理按名称组织的 Edictum 会话冷配置"""

    def __init__(
        self,
        type_registry: EdictumTypeRegistry,
        storage_path: str | Path | None = None,
        *,
        auto_create: bool = True,
    ) -> None:
        """
        初始化 Edictum 冷配置管理器

        参数:
        - type_registry: 用于校验 edictum_type 的类型注册表
        - storage_path: 配置文件路径
        - auto_create: 是否自动创建父目录和空配置文件
        """
        self.type_registry = type_registry
        self.storage_path = Path(storage_path) if storage_path else self._default_storage_path()
        self.plugin_catalog = PluginCatalog()
        self._configs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        if auto_create:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.reload()

    @staticmethod
    def _default_storage_path() -> Path:
        """
        获取默认配置文件路径

        返回:
        - Path: 环境变量路径或默认数据目录路径
        """
        env_path = os.getenv("SATRAP_EDICTUM_CONFIG_PATH")
        return Path(env_path) if env_path else get_data_dir() / "edictum_session_config.json"

    @staticmethod
    def _normalize_name(value: object) -> str:
        """
        校验并规范化配置名称

        参数:
        - value: 原始名称

        返回:
        - str: 去除首尾空格的非空名称
        """
        name = str(value or "").strip()
        if not name:
            raise ValueError("Edictum 配置名称不能为空")
        if "/" in name or "\\" in name:
            raise ValueError("Edictum 配置名称不能包含路径分隔符")
        return name

    def _normalize_plugins(self, value: object) -> list[dict[str, Any]]:
        """
        校验插件配置列表并复制为 JSON 安全结构

        参数:
        - value: 插件名称或插件配置对象列表

        返回:
        - list[dict[str, Any]]: 标准插件配置对象列表
        """
        return [
            spec.to_config()
            for spec in parse_plugin_specs(
                value,
                self.plugin_catalog,
                require_available=False,
            )
        ]

    def _normalize_entry(self, entry: object) -> dict[str, Any]:
        """
        校验并规范化单个配置条目

        参数:
        - entry: 原始配置对象

        返回:
        - dict[str, Any]: 规范化配置条目
        """
        if not isinstance(entry, dict):
            raise ValueError("Edictum 配置必须是对象")
        raw = dict(cast(dict[str, Any], entry))
        type_name = str(raw.get("edictum_type", "")).strip()
        self.type_registry.require(type_name)
        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("params 必须是对象")
        model_name = str(raw.get("model_name", "")).strip()
        return {
            "provider": EDICTUM_PROVIDER,
            "edictum_type": type_name,
            "enabled": bool(raw.get("enabled", True)),
            "description": str(raw.get("description", "")),
            "model_name": model_name,
            "params": dict(cast(dict[str, Any], params)),
            "plugins": self._normalize_plugins(raw.get("plugins", [])),
        }

    def _save_locked(self) -> None:
        """原子保存全部冷配置"""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.storage_path.with_suffix(f"{self.storage_path.suffix}.tmp")
        with temporary_path.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(self._configs, file, ensure_ascii=False, indent=2)
            file.write("\n")
        temporary_path.replace(self.storage_path)

    def reload(self) -> None:
        """从磁盘重新加载冷配置"""
        with self._lock:
            if not self.storage_path.exists():
                self._configs = {}
                self._save_locked()
                return
            try:
                with self.storage_path.open("r", encoding="utf-8") as file:
                    payload: object = json.load(file)
                if not isinstance(payload, dict):
                    raise ValueError("Edictum 配置文件根节点必须是对象")
                loaded: dict[str, dict[str, Any]] = {}
                for raw_name, raw_entry in cast(dict[str, object], payload).items():
                    name = self._normalize_name(raw_name)
                    loaded[name] = self._normalize_entry(raw_entry)
                self._configs = loaded
            except Exception as error:
                logger.error(f"[EdictumConfigManager] 读取配置失败: {error}")
                raise

    def list_configs(self) -> dict[str, dict[str, Any]]:
        """
        列出全部命名配置

        返回:
        - dict[str, dict[str, Any]]: 配置副本
        """
        with self._lock:
            return deepcopy(self._configs)

    def get_config(self, name: str) -> dict[str, Any] | None:
        """
        获取单个命名配置

        参数:
        - name: 配置名称

        返回:
        - dict[str, Any] | None: 配置不存在时返回 None
        """
        with self._lock:
            entry = self._configs.get(self._normalize_name(name))
            return deepcopy(entry) if entry is not None else None

    def create(self, name: str, entry: object) -> dict[str, Any]:
        """
        创建命名冷配置

        参数:
        - name: 配置名称
        - entry: 配置内容

        返回:
        - dict[str, Any]: 已保存配置
        """
        key = self._normalize_name(name)
        normalized = self._normalize_entry(entry)
        with self._lock:
            if key in self._configs:
                raise ValueError(f"Edictum 配置名称已存在: {key}")
            self._configs[key] = normalized
            self._save_locked()
            return deepcopy(normalized)

    def update(
        self,
        name: str,
        changes: object,
        *,
        new_name: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """
        原子更新并按需重命名配置

        参数:
        - name: 当前配置名称
        - changes: 待合并字段
        - new_name: 可选新名称

        返回:
        - tuple[str, dict[str, Any]]: 最终名称和配置
        """
        key = self._normalize_name(name)
        target = self._normalize_name(new_name) if new_name is not None else key
        if not isinstance(changes, dict):
            raise ValueError("Edictum 配置更新必须是对象")
        with self._lock:
            current = self._configs.get(key)
            if current is None:
                raise ValueError(f"Edictum 配置不存在: {key}")
            if target != key and target in self._configs:
                raise ValueError(f"Edictum 配置名称已存在: {target}")
            merged = dict(current)
            merged.update(cast(dict[str, Any], changes))
            normalized = self._normalize_entry(merged)
            if target != key:
                del self._configs[key]
            self._configs[target] = normalized
            self._save_locked()
            return target, deepcopy(normalized)

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """
        设置配置启用状态

        参数:
        - name: 配置名称
        - enabled: 是否启用

        返回:
        - dict[str, Any]: 更新后的配置
        """
        return self.update(name, {"enabled": enabled})[1]

    def delete(self, name: str) -> bool:
        """
        删除命名冷配置

        参数:
        - name: 配置名称

        返回:
        - bool: 是否找到并删除配置
        """
        key = self._normalize_name(name)
        with self._lock:
            if key not in self._configs:
                return False
            del self._configs[key]
            self._save_locked()
            return True
