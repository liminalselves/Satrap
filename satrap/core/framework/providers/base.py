"""
会话 Provider 公共契约与注册表

定义命名会话配置到运行时会话实例之间的统一边界,
使扫描式 Session 类和 Edictum 会话可以共享 SessionManager
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Protocol, runtime_checkable

from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.type import SessionConfig


SESSION_CLASS_PROVIDER = "session_class"
"""传统扫描式 Session 类 Provider 名称"""


@dataclass(frozen=True)
class SessionProviderDefinition:
    """Provider 暴露给运行时的统一命名会话定义"""

    name: str
    provider_name: str
    is_async: bool
    enabled: bool = True
    model_key: str = "model_name"
    params: dict[str, Any] = field(default_factory=dict[str, Any])
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


@runtime_checkable
class SessionProvider(Protocol):
    """命名会话定义的运行时 Provider 契约"""

    @property
    def provider_name(self) -> str:
        """返回 Provider 唯一名称"""
        ...

    def has_definition(self, name: str) -> bool:
        """
        判断命名会话定义是否存在

        参数:
        - name: 命名会话定义名称

        返回:
        - bool: 命名会话定义是否存在
        """
        ...

    def get_definition(self, name: str) -> SessionProviderDefinition | None:
        """
        获取统一命名会话定义

        参数:
        - name: 命名会话定义名称

        返回:
        - SessionProviderDefinition | None: 定义不存在时返回 None
        """
        ...

    def create_session(
        self,
        session_config: SessionConfig,
        llm: LLM | AsyncLLM | None = None,
    ) -> Session | AsyncSession:
        """
        根据实例配置创建运行时会话

        参数:
        - session_config: 持久化的会话实例配置
        - llm: 已按同步或异步形态构建的模型实例

        返回:
        - Session | AsyncSession: 运行时会话实例
        """
        ...


class SessionProviderRegistry:
    """会话 Provider 注册表, 负责注册和命名定义分发"""

    def __init__(self) -> None:
        """初始化空 Provider 注册表"""
        self._providers: dict[str, SessionProvider] = {}
        self._lock = threading.RLock()

    def register(self, provider: SessionProvider, *, replace: bool = False) -> None:
        """
        注册 Provider

        参数:
        - provider: Provider 实例
        - replace: 是否允许覆盖同名 Provider
        """
        name = provider.provider_name.strip()
        if not name:
            raise ValueError("Provider 名称不能为空")
        with self._lock:
            if name in self._providers and not replace:
                raise ValueError(f"Provider 已存在: {name}")
            self._providers[name] = provider

    def get(self, name: str) -> SessionProvider | None:
        """
        按名称获取 Provider

        参数:
        - name: Provider 名称

        返回:
        - SessionProvider | None: Provider 不存在时返回 None
        """
        with self._lock:
            return self._providers.get(name.strip())

    def require(self, name: str) -> SessionProvider:
        """
        按名称获取 Provider, 不存在时抛出异常

        参数:
        - name: Provider 名称

        返回:
        - SessionProvider: Provider 实例
        """
        provider = self.get(name)
        if provider is None:
            raise ValueError(f"未知会话 Provider: {name}")
        return provider

    def resolve_definition(
        self,
        definition_name: str,
        provider_name: str | None = None,
    ) -> tuple[SessionProvider, SessionProviderDefinition] | None:
        """
        解析命名会话定义

        指定 provider_name 时只在该 Provider 中查找; 未指定时要求名称全局唯一

        参数:
        - definition_name: 命名会话定义名称
        - provider_name: 可选 Provider 名称

        返回:
        - tuple[SessionProvider, SessionProviderDefinition] | None: 定义不存在时返回 None
        """
        if provider_name:
            provider = self.require(provider_name)
            definition = provider.get_definition(definition_name)
            return (provider, definition) if definition is not None else None

        with self._lock:
            providers = list(self._providers.values())
        matches: list[tuple[SessionProvider, SessionProviderDefinition]] = []
        for provider in providers:
            definition = provider.get_definition(definition_name)
            if definition is not None:
                matches.append((provider, definition))
        if len(matches) > 1:
            provider_names = ", ".join(item[0].provider_name for item in matches)
            raise ValueError(f"会话定义名称存在歧义: {definition_name}, Providers: {provider_names}")
        return matches[0] if matches else None

    def list_names(self) -> list[str]:
        """
        列出已注册 Provider 名称

        返回:
        - list[str]: Provider 名称列表
        """
        with self._lock:
            return list(self._providers)
