"""会话 Provider 注册表与命名定义分发"""
from __future__ import annotations

import threading

from satrap.core.type.session_provider import (
    BindingState,
    BindingStatus,
    SessionProvider,
    SessionProviderDefinition,
)


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

    def binding_status(self, definition_name: str, provider_name: str | None = None) -> BindingStatus:
        """
        判定命名会话绑定此刻是否可执行

        与 resolve_definition 的区别在于把"未命中"与"已禁用"区分为两种状态:
        前者是配置错误, 后者是正常配置状态, 调用方据此决定是拒绝加载还是仅暂缓执行

        参数:
        - definition_name: 命名会话定义名称
        - provider_name: 可选 Provider 名称

        返回:
        - BindingStatus: RUNNABLE/DISABLED/INVALID 及对应诊断文本
        """
        location = f"provider={provider_name or ''}, name={definition_name}"
        try:
            resolved = self.resolve_definition(definition_name, provider_name)
        except ValueError as error:
            # 未知 Provider 与名称歧义属于既有诊断, 原样透出以便调用方定位配置错误
            return BindingStatus(BindingState.INVALID, str(error))
        if resolved is None:
            return BindingStatus(BindingState.INVALID, f"会话定义不可用 {location}")
        if not resolved[1].enabled:
            return BindingStatus(BindingState.DISABLED, f"会话定义已禁用: {location}")
        return BindingStatus(BindingState.RUNNABLE)

    def list_names(self) -> list[str]:
        """
        列出已注册 Provider 名称

        返回:
        - list[str]: Provider 名称列表
        """
        with self._lock:
            return list(self._providers)
