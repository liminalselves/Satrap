"""
扫描式 Session 类 Provider

把 SessionClassConfigManager 和 SessionRegistry 暴露的命名类配置,
转换成统一定义并负责构造 Session 或 AsyncSession 实例
"""
from __future__ import annotations

import inspect
from typing import Any, Protocol, Type

from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.providers.base import (
    SESSION_CLASS_PROVIDER,
    SessionProviderDefinition,
)
from satrap.core.type import SessionConfig


class SessionClassRegistryProtocol(Protocol):
    """SessionClassProvider 依赖的最小类注册表接口"""

    def register(
        self,
        session_type_name: str,
        session_class: Type[Session] | Type[AsyncSession],
    ) -> None:
        """注册命名会话类"""
        ...

    def get_class(
        self,
        session_type_name: str,
    ) -> Type[Session] | Type[AsyncSession] | None:
        """获取命名会话类"""
        ...


class SessionClassProvider:
    """根据扫描或显式注册的 Session 子类创建运行时实例"""

    provider_name = SESSION_CLASS_PROVIDER

    def __init__(
        self,
        registry: SessionClassRegistryProtocol,
        config_manager: SessionClassConfigManager | None = None,
        *,
        default_checkpoint: bool = False,
        default_checkpoint_db: str | None = None,
    ) -> None:
        """
        初始化 SessionClassProvider

        参数:
        - registry: 运行时会话类注册表
        - config_manager: 扫描式会话类配置管理器
        - default_checkpoint: 默认是否启用状态检查点
        - default_checkpoint_db: 默认上下文数据库路径
        """
        self.registry = registry
        self.config_manager = config_manager
        self.default_checkpoint = default_checkpoint
        self.default_checkpoint_db = default_checkpoint_db

    def set_config_manager(self, manager: SessionClassConfigManager | None) -> None:
        """
        更新会话类配置管理器

        参数:
        - manager: 会话类配置管理器
        """
        self.config_manager = manager

    def has_definition(self, name: str) -> bool:
        """
        判断命名会话类是否存在

        参数:
        - name: 命名会话类名称

        返回:
        - bool: 命名会话类是否存在
        """
        manager = self.config_manager
        return self.registry.get_class(name) is not None or bool(manager and manager.has_config(name))

    def get_definition(self, name: str) -> SessionProviderDefinition | None:
        """
        获取统一命名会话类定义

        参数:
        - name: 命名会话类名称

        返回:
        - SessionProviderDefinition | None: 定义不存在时返回 None
        """
        manager = self.config_manager
        config = manager.get_config(name) if manager else None
        session_class = self.registry.get_class(name)
        if session_class is None and config is None:
            return None
        return SessionProviderDefinition(
            name=name,
            provider_name=self.provider_name,
            is_async=(
                issubclass(session_class, AsyncSession)
                if session_class is not None
                else bool(config and config.get("is_async", False))
            ),
            enabled=bool(config.get("enabled", True)) if config else True,
            model_key=str(config.get("model_key", "") or "model_name") if config else "model_name",
            params=dict(config.get("params", {})) if config else {},
            description=str(config.get("description", "")) if config else "",
            metadata={
                "class_path": str(config.get("class_path", "")),
                "context_key": str(config.get("context_key", "")),
            } if config else {},
        )

    def create_session(
        self,
        session_config: SessionConfig,
        llm: LLM | AsyncLLM | None = None,
    ) -> Session | AsyncSession:
        """
        根据实例配置创建扫描式 Session

        参数:
        - session_config: 持久化的会话实例配置
        - llm: 已构建的模型实例

        返回:
        - Session | AsyncSession: 运行时会话实例
        """
        definition_name = session_config.session_type_name or ""
        definition = self.get_definition(definition_name)
        if definition is None:
            raise ValueError(f"未知会话类配置: {definition_name}")
        if not definition.enabled:
            raise ValueError(f"会话类配置已禁用: {definition_name}")
        session_class = self.registry.get_class(definition_name)
        if session_class is None and self.config_manager is not None:
            session_class = self.config_manager.get_class(definition_name)
            self.registry.register(definition_name, session_class)
        if session_class is None:
            raise ValueError(f"会话类未加载: {definition_name}")

        payload = dict(session_config.session_config or {})
        if llm is not None:
            payload["llm"] = llm
        configured = SessionConfig(
            session_id=session_config.session_id,
            session_type_name=session_config.session_type_name,
            provider_name=self.provider_name,
            created_at=session_config.created_at,
            last_used_at=session_config.last_used_at,
            message_count=session_config.message_count,
            session_config=payload,
        )
        return self._instantiate(session_class, configured)

    def _instantiate(
        self,
        session_class: Type[Session] | Type[AsyncSession],
        session_config: SessionConfig,
    ) -> Session | AsyncSession:
        """
        兼容现有三类 Session 构造签名并完成实例化

        参数:
        - session_class: Session 或 AsyncSession 子类
        - session_config: 会话实例配置

        返回:
        - Session | AsyncSession: 已创建的运行时会话
        """
        signature = inspect.signature(session_class)
        parameters = list(signature.parameters.values())
        has_var_kw = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters)
        accepted = {item.name for item in parameters}
        payload = dict(session_config.session_config or {})
        payload.pop("session_id", None)

        def inject_checkpoint_defaults(target: dict[str, Any]) -> None:
            """
            在构造器支持且配置未显式提供时注入检查点默认值

            参数:
            - target: 即将传入构造器的关键字参数
            """
            if "enable_checkpoint" not in payload and ("enable_checkpoint" in accepted or has_var_kw):
                target.setdefault("enable_checkpoint", self.default_checkpoint)
            if self.default_checkpoint_db is not None and "db_path" not in payload and ("db_path" in accepted or has_var_kw):
                target.setdefault("db_path", self.default_checkpoint_db)

        if "session_config" in accepted:
            kwargs: dict[str, Any] = {"session_config": session_config}
            if "session_id" in accepted:
                kwargs["session_id"] = session_config.session_id
            if has_var_kw:
                kwargs.update(payload)
            else:
                kwargs.update({key: value for key, value in payload.items() if key in accepted and key not in kwargs})
            inject_checkpoint_defaults(kwargs)
            return session_class(**kwargs)

        kwargs = {}
        if "session_id" in accepted:
            kwargs["session_id"] = session_config.session_id
        if has_var_kw:
            kwargs.update(payload)
        else:
            kwargs.update({key: value for key, value in payload.items() if key in accepted})
        inject_checkpoint_defaults(kwargs)

        if not kwargs and session_config.session_id:
            try:
                return session_class(session_config.session_id)
            except Exception:
                pass
        return session_class(**kwargs)
