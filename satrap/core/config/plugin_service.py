"""插件管理目录, 汇总来源与配置引用而不导入插件代码"""
from __future__ import annotations

from typing import Any
import hashlib

from satrap.display.plugins import ChatPluginRegistry
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_config import PluginConfigManager, schema_to_payload, validate_config_values
from satrap.edictum.plugin_settings import validate_plugin_settings
from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK
from satrap.core.config.document import ConfigRevisionConflict, config_document_revision
from satrap.core.storage.file_lock import FileLock
from satrap.core.config.edictum_service import EdictumConfigService
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.edictum.plugin_spec import parse_plugin_specs


class PluginManagementService:
    """独立管理页的插件元数据服务"""

    def __init__(
        self,
        catalog: PluginCatalog,
        configs: dict[str, dict[str, Any]],
        chat: ChatPluginRegistry,
        *,
        manager: PluginConfigManager | None = None,
        models: Any = None,
        rag: Any = None,
        edictum: EdictumConfigService | None = None,
    ) -> None:
        """
        初始化插件目录服务

        参数:
        - catalog: 共享插件目录
        - configs: Edictum 命名配置快照
        - chat: Chat 插件状态注册表
        - manager: 全局参数管理器, 默认使用共享用户配置目录
        - models: 可选的模型引用校验服务
        - rag: 可选的知识库引用校验服务
        - edictum: 可选的命名配置写入服务
        """
        self.catalog = catalog
        self.configs = configs
        self.chat = chat
        self.chat.catalog = catalog
        self.manager = manager or PluginConfigManager()
        self.models = models
        self.rag = rag
        self.edictum = edictum

    def _revision(self, name: str) -> str:
        """
        计算配置文件版本, 保留缺失文件与空配置的区别

        参数:
        - name: 插件名称

        返回:
        - 原始文件内容的 SHA256, 文件缺失时为 missing
        """
        path = self.manager._global_path(name)
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"

    def get_config(self, name: str) -> dict[str, Any]:
        """
        读取全局显式配置、生效配置和并发版本

        参数:
        - name: 目录中的插件名称

        返回:
        - 全局配置快照, 不存在时抛出 KeyError
        """
        entry = self.catalog.get(name)
        if entry is None:
            raise KeyError(f"插件不存在: {name}")
        path = self.manager._global_path(name)
        with REFERENCE_SCAN_LOCK, FileLock(path.with_name(f".{path.name}.lock")):
            overrides = self.manager.load_global_explicit(name, entry.config_schema)
            return {"ok": True, "schema": schema_to_payload(entry.config_schema), "overrides": overrides,
                    "config": {**{key: field.default for key, field in entry.config_schema.items()}, **overrides},
                    "revision": self._revision(name)}

    def save_config(self, name: str, values: dict[str, Any], expected_revision: str) -> dict[str, Any]:
        """
        校验并保存全局显式参数, 拒绝覆盖其他页面的并发修改

        参数:
        - name: 插件名称
        - values: 显式配置, 空对象恢复全部默认值
        - expected_revision: 编辑开始时读取的文件版本

        返回:
        - 保存后的完整快照, 冲突时抛出 ConfigRevisionConflict
        """
        entry = self.catalog.get(name)
        if entry is None:
            raise KeyError(f"插件不存在: {name}")
        path = self.manager._global_path(name)
        with REFERENCE_SCAN_LOCK, FileLock(path.with_name(f".{path.name}.lock")):
            if not expected_revision or self._revision(name) != expected_revision:
                raise ConfigRevisionConflict("全局参数已被其他页面修改, 请重新读取后合并")
            cleaned = validate_config_values(entry.config_schema, values)
            effective = {**{key: field.default for key, field in entry.config_schema.items()}, **cleaned}
            validate_plugin_settings(name, entry.config_schema, effective, models=self.models, rag=self.rag)
            self.manager.save_global(name, entry.config_schema, cleaned)
            return {**self.get_config(name), "saved": True}

    def list_plugins(self) -> list[dict[str, Any]]:
        """
        汇总目录来源与使用位置数量

        返回:
        - 插件列表, 使用数量包含已配置的 Chat 和所有引用该插件的命名配置
        """
        chat_states = {item["name"]: item for item in self.chat.scan()}
        result: list[dict[str, Any]] = []
        for entry in self.catalog.scan():
            references = [
                name for name, config in self.configs.items()
                if any(
                    (item if isinstance(item, str) else item.get("name")) == entry.name
                    for item in config.get("plugins", [])
                    if isinstance(item, (str, dict))
                )
            ]
            chat_enabled = bool(chat_states.get(entry.name, {}).get("enabled", False))
            chat_configured = bool(chat_states.get(entry.name, {}).get("configured", False))
            payload = entry.to_payload()
            payload.update(
                source="builtin" if entry.path.resolve().is_relative_to(self.catalog.preset_dir.resolve()) else "user",
                usage_count=len(references) + int(chat_configured),
                edictum_configs=references,
                chat_enabled=chat_enabled,
            )
            result.append(payload)
        return result

    def get_usages(self, name: str) -> dict[str, Any]:
        """
        列出可添加的位置及各位置的独立配置快照

        参数:
        - name: 插件名称

        返回:
        - Chat 与所有 Edictum 命名配置的位置, 包含并发版本及可用性
        """
        entry = self.catalog.get(name)
        if entry is None:
            raise KeyError(f"插件不存在: {name}")
        self.chat.refresh()
        chat = next(item for item in self.chat.scan() if item["name"] == name)
        locations = [{"kind": "chat", "id": "chat", "label": "Chat", "present": chat["configured"], "enabled": chat["enabled"],
                      "capabilities": {kind: {item["name"]: item["enabled"] for item in items} for kind, items in chat["capabilities"].items()},
                      "revision": chat["revision"], "availability": chat["availability"], "parent_enabled": True}]
        types = {item["name"]: item for item in self.edictum.list_types()} if self.edictum else {}
        for config_name, config in self.configs.items():
            existing = next((item for item in config.get("plugins", []) if (item if isinstance(item, str) else item.get("name")) == name), None)
            state = {"enabled": True, "capabilities": {}} if isinstance(existing, str) else existing or {}
            can_add = bool(types.get(config.get("edictum_type"), {}).get("capabilities", {}).get("plugins", True))
            locations.append({"kind": "edictum", "id": config_name, "label": config_name,
                              "present": existing is not None, "enabled": state.get("enabled", True), "capabilities": state.get("capabilities", {}),
                              "revision": config_document_revision(config), "parent_enabled": config.get("enabled", True),
                              "availability": {"allowed": can_add, "message": "激活实例时校验适用平台" if can_add else "此 Edictum 类型不支持插件"}})
        return {"ok": True, "locations": locations}

    def save_usage(self, name: str, kind: str, location_id: str, state: dict[str, Any], expected_revision: str) -> dict[str, Any]:
        """
        保存一个位置的插件引用, 保留其他插件及已有参数覆盖

        参数:
        - name: 插件名称
        - kind: chat 或 edictum
        - location_id: Chat 固定标识或命名配置名称
        - state: present、enabled 和 capabilities 草稿
        - expected_revision: 该使用位置的编辑版本

        返回:
        - 最新位置快照及 saved 标识; 冲突时不写入配置
        """
        entry = self.catalog.get(name)
        if entry is None:
            raise KeyError(f"插件不存在: {name}")
        if set(state) - {"present", "enabled", "capabilities"} or not isinstance(state.get("present"), bool) or not isinstance(state.get("enabled"), bool):
            raise ValueError("使用位置配置必须包含布尔值 present 和 enabled")
        spec = parse_plugin_specs([{"name": name, "enabled": state["enabled"], "capabilities": state.get("capabilities", {})}], self.catalog, require_available=True)[0]
        if kind == "chat" and location_id == "chat":
            if state["present"] and spec.enabled:
                entry.check_environment(PluginEnvironment("chat")).require()
            self.chat.configure(name, {"enabled": spec.enabled, "capabilities": spec.capabilities} if state["present"] else None, expected_revision)
        elif kind == "edictum" and self.edictum is not None:
            current = self.edictum.get(location_id)
            if current is None:
                raise KeyError("Edictum 配置不存在")
            if config_document_revision(current) != expected_revision:
                raise ConfigRevisionConflict("Edictum 配置已被修改, 请重新读取后合并")
            existing = next((item for item in current.get("plugins", []) if (item if isinstance(item, str) else item.get("name")) == name), None)
            replacement = {**(existing if isinstance(existing, dict) else {}), "name": name, "enabled": spec.enabled, "capabilities": spec.capabilities}
            if state["present"] and spec.enabled:
                definition = next(item for item in self.edictum.list_types() if item["name"] == current["edictum_type"])
                if not definition["capabilities"]["plugins"]:
                    raise ValueError("此 Edictum 类型不支持插件")
            others = [item for item in current.get("plugins", []) if (item if isinstance(item, str) else item.get("name")) != name]
            if state["present"]:
                others.append(replacement)
            self.edictum.update(location_id, {"plugins": others}, expected_revision=expected_revision)
            self.configs = self.edictum.list_configs()
        else:
            raise ValueError("未知使用位置")
        return {**self.get_usages(name), "saved": True}
