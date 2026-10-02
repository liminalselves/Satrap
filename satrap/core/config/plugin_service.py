"""插件管理目录, 汇总来源与配置引用而不导入插件代码"""
from __future__ import annotations

from typing import Any

from satrap.display.plugins import ChatPluginRegistry
from satrap.edictum.plugin_catalog import PluginCatalog


class PluginManagementService:
    """独立管理页的插件元数据服务"""

    def __init__(
        self,
        catalog: PluginCatalog,
        configs: dict[str, dict[str, Any]],
        chat: ChatPluginRegistry,
    ) -> None:
        """
        初始化插件目录服务

        参数:
        - catalog: 共享插件目录
        - configs: Edictum 命名配置快照
        - chat: Chat 插件状态注册表
        """
        self.catalog = catalog
        self.configs = configs
        self.chat = chat

    def list_plugins(self) -> list[dict[str, Any]]:
        """
        汇总目录来源与使用位置数量

        返回:
        - 插件列表, 使用数量包含已启用的 Chat 和所有引用该插件的命名配置
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
            payload = entry.to_payload()
            payload.update(
                source="builtin" if entry.path.resolve().is_relative_to(self.catalog.preset_dir.resolve()) else "user",
                usage_count=len(references) + int(chat_enabled),
                edictum_configs=references,
                chat_enabled=chat_enabled,
            )
            result.append(payload)
        return result
