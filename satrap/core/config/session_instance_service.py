"""
会话实例冷管理服务

在平台后端未启动时直接管理 SessionConfig SQLite,
并同步清理用户绑定和上下文路由
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
import dataclasses
import secrets
import string
import time

from satrap.core.framework.providers.base import SESSION_CLASS_PROVIDER
from satrap.edictum.registry import EDICTUM_PROVIDER
from satrap.core.storage import StorageMaintenanceService, StorageScope
from satrap.core.type import SessionConfig

if TYPE_CHECKING:
    from satrap.core.framework.SessionClassManager import SessionClassConfigManager
    from satrap.core.framework.SessionManager import SessionConfigStore
    from satrap.core.framework.UserManager import UserInfoStore
    from satrap.edictum.config import EdictumConfigManager
    from satrap.core.storage import StorageLayout
# 管理器和存储对象由调用方注入, 此处仅在静态类型检查时导入


_UID_ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase


def _short_uid(length: int = 6) -> str:
    """
    生成会话实例的短随机 ID

    参数:
    - length: ID 长度

    返回:
    - str: base62 随机 ID
    """
    return "".join(secrets.choice(_UID_ALPHABET) for _ in range(length))


class SessionInstanceConfigService:
    """管理未加载到内存的持久化会话实例"""

    def __init__(
        self,
        store: SessionConfigStore,
        user_store: UserInfoStore,
        session_class_manager: SessionClassConfigManager,
        edictum_manager: EdictumConfigManager,
        platform_id: str,
        storage_layout: StorageLayout,
    ) -> None:
        """
        初始化会话实例冷管理服务

        参数:
        - store: 会话实例配置存储
        - user_store: 用户和上下文路由存储
        - session_class_manager: 扫描式会话类配置管理器
        - edictum_manager: Edictum 命名配置管理器
        - platform_id: 平台实例 ID
        - storage_layout: v2 数据布局
        """
        self.store = store
        self.user_store = user_store
        self.session_class_manager = session_class_manager
        self.edictum_manager = edictum_manager
        self.platform_id = platform_id
        self.storage_layout = storage_layout

    def list_instances(self, limit: int = 200) -> list[dict[str, Any]]:
        """
        列出持久化会话实例

        参数:
        - limit: 最大返回数量

        返回:
        - list[dict[str, Any]]: 统一前端会话实例结构
        """
        instances: list[dict[str, Any]] = []
        for config in self.store.list(limit=limit):
            serialized = dataclasses.asdict(config)
            serialized["platform_id"] = self.platform_id
            serialized["active"] = False
            serialized["runtime"] = {}
            instances.append(serialized)
        return instances

    def create_instance(
        self,
        provider_name: str,
        definition_name: str,
        *,
        session_id: str | None = None,
        llm_name: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> SessionConfig:
        """
        根据冷配置创建未激活的持久化会话实例

        参数:
        - provider_name: 会话 Provider 名称
        - definition_name: Provider 内的命名定义
        - session_id: 可选自定义会话 ID
        - llm_name: 可选实例级模型覆盖
        - extra_params: 可选实例级参数覆盖

        返回:
        - SessionConfig: 已保存的会话实例配置
        """
        provider = provider_name.strip() or SESSION_CLASS_PROVIDER
        definition = definition_name.strip()
        if not definition:
            raise ValueError("session_type 不能为空")
        params: dict[str, Any]
        model_key = "model_name"
        if provider == SESSION_CLASS_PROVIDER:
            config = self.session_class_manager.get_config(definition)
            if config is None:
                raise ValueError(f"未知会话类配置: {definition}")
            if not bool(config.get("enabled", True)):
                raise ValueError(f"会话类配置已禁用: {definition}")
            params = dict(config.get("params", {}))
            model_key = str(config.get("model_key", "") or "model_name")
        elif provider == EDICTUM_PROVIDER:
            config = self.edictum_manager.get_config(definition)
            if config is None:
                raise ValueError(f"未知 Edictum 配置: {definition}")
            if not bool(config.get("enabled", True)):
                raise ValueError(f"Edictum 配置已禁用: {definition}")
            params = {}   # Edictum 实例只保存覆盖值, 默认值始终继承命名配置
        else:
            raise ValueError(f"冷管理不支持会话 Provider: {provider}")
        if extra_params:
            params.update(extra_params)
        if llm_name:
            params[model_key] = llm_name.strip()

        final_session_id = (session_id or "").strip() or _short_uid()
        if self.store.get(final_session_id) is not None:
            raise ValueError(f"session_id 已存在: {final_session_id}")
        now = time.time()
        created = SessionConfig(
            session_id=final_session_id,
            session_type_name=definition,
            provider_name=provider,
            created_at=now,
            last_used_at=now,
            message_count=0,
            session_config=params,
        )
        self.store.upsert(created)
        self.storage_layout.bind_session(
            StorageScope(platform_id=self.platform_id, session_id=final_session_id)
        )
        return created

    def delete_instances(self, session_ids: list[str]) -> list[str]:
        """
        删除持久化实例及其用户侧引用

        参数:
        - session_ids: 待删除的会话 ID

        返回:
        - list[str]: 实际删除的会话 ID
        """
        requested = list(dict.fromkeys(item.strip() for item in session_ids if item.strip()))
        existing_configs = {
            item: config
            for item in requested
            if (config := self.store.get(item)) is not None
        }
        for session_id in existing_configs:
            StorageMaintenanceService(self.storage_layout).archive_session(
                self.platform_id,
                session_id,
                database_path=self.store.db_path,
            )
        deleted = [item for item in existing_configs if self.store.get(item) is None]
        if deleted:
            self.user_store.remove_session_references(deleted)
        return deleted

    def delete_by_mode(self, mode: str, session_ids: list[str] | None = None) -> list[str]:
        """
        按消息数或显式选择批量删除会话实例

        参数:
        - mode: empty, single 或 selected
        - session_ids: selected 模式下的会话 ID

        返回:
        - list[str]: 实际删除的会话 ID
        """
        if mode == "empty":
            targets = self.store.list_ids_by_message_count(0)
        elif mode == "single":
            targets = self.store.list_ids_by_message_count(1)
        elif mode == "selected":
            targets = session_ids or []
            if not targets:
                raise ValueError("至少选择一个会话实例")
        else:
            raise ValueError(f"未知批量删除模式: {mode}")
        return self.delete_instances(targets)
