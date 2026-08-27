"""
Edictum 命名会话 Provider

把 Edictum 冷配置和类型注册表转换为统一运行时定义,
新增 Edictum 会话实现时只需注册工厂和配置元数据
"""
from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass, field
from typing import Any, cast

from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.framework.providers.base import SessionProviderDefinition
from satrap.core.log import logger
from satrap.core.type import SessionConfig
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.plugin import resolve_plugin_dir
from satrap.edictum.registry import (
    EDICTUM_PROVIDER,
    EdictumTypeDefinition,
    EdictumTypeRegistry,
)


@dataclass
class _PluginRuntimeState:
    """单个 Edictum 插件的运行时加载状态"""

    name: str
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict[str, Any])
    status: str = "pending"
    error: str | None = None


@dataclass
class _EdictumRuntimeState:
    """一个 Edictum 会话的 Provider 运行时状态"""

    type_definition: EdictumTypeDefinition
    plugins: list[_PluginRuntimeState] = field(default_factory=list[_PluginRuntimeState])


class EdictumProvider:
    """根据 Edictum 命名冷配置创建运行时会话"""

    provider_name = EDICTUM_PROVIDER

    def __init__(
        self,
        config_manager: EdictumConfigManager,
        type_registry: EdictumTypeRegistry,
        *,
        default_checkpoint: bool = False,
        default_checkpoint_db: str | None = None,
    ) -> None:
        """
        初始化 EdictumProvider

        参数:
        - config_manager: Edictum 命名冷配置管理器
        - type_registry: Edictum 会话类型注册表
        - default_checkpoint: 默认是否启用状态检查点
        - default_checkpoint_db: 默认上下文数据库路径
        """
        self.config_manager = config_manager
        self.type_registry = type_registry
        self.default_checkpoint = default_checkpoint
        self.default_checkpoint_db = default_checkpoint_db
        self._runtime_states: dict[int, _EdictumRuntimeState] = {}
        self._runtime_lock = threading.RLock()

    def has_definition(self, name: str) -> bool:
        """
        判断 Edictum 命名配置是否存在

        参数:
        - name: 命名配置名称

        返回:
        - bool: 配置是否存在
        """
        return self.config_manager.get_config(name) is not None

    def get_definition(self, name: str) -> SessionProviderDefinition | None:
        """
        获取统一 Edictum 命名会话定义

        参数:
        - name: 命名配置名称

        返回:
        - SessionProviderDefinition | None: 配置不存在时返回 None
        """
        config = self.config_manager.get_config(name)
        if config is None:
            return None
        type_name = str(config.get("edictum_type", ""))
        type_definition = self.type_registry.require(type_name)
        params = dict(config.get("params", {}))
        model_name = str(config.get("model_name", "")).strip()
        if model_name:
            params["model_name"] = model_name
        return SessionProviderDefinition(
            name=name,
            provider_name=self.provider_name,
            is_async=type_definition.is_async,
            enabled=bool(config.get("enabled", True)),
            model_key="model_name",
            params=params,
            description=str(config.get("description", "")),
            metadata={
                "edictum_type": type_name,
                "plugins": list(config.get("plugins", [])),
            },
        )

    def create_session(
        self,
        session_config: SessionConfig,
        llm: LLM | AsyncLLM | None = None,
    ) -> Session | AsyncSession:
        """
        根据实例配置创建 Edictum 会话

        参数:
        - session_config: 持久化的会话实例配置
        - llm: 已按类型构建的同步或异步模型

        返回:
        - Session | AsyncSession: 已创建的 Edictum 会话实例
        """
        name = session_config.session_type_name or ""
        definition = self.get_definition(name)
        if definition is None:
            raise ValueError(f"未知 Edictum 配置: {name}")
        if not definition.enabled:
            raise ValueError(f"Edictum 配置已禁用: {name}")
        if llm is None:
            raise ValueError(f"Edictum 配置缺少可用 LLM: {name}")

        type_name = str(definition.metadata.get("edictum_type", ""))
        type_definition = self.type_registry.require(type_name)
        factory = type_definition.factory
        signature = inspect.signature(factory)
        parameters = signature.parameters
        has_var_kw = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
        payload = dict(session_config.session_config or {})
        payload.pop("model_name", None)
        payload.pop("plugins", None)
        if "enable_checkpoint" not in payload and ("enable_checkpoint" in parameters or has_var_kw):
            payload["enable_checkpoint"] = self.default_checkpoint
        if (
            self.default_checkpoint_db is not None
            and "db_path" not in payload
            and ("db_path" in parameters or has_var_kw)
        ):
            payload["db_path"] = self.default_checkpoint_db
        if not has_var_kw:
            payload = {key: value for key, value in payload.items() if key in parameters}
        session = factory(
            session_id=session_config.session_id or "",
            llm=llm,
            **payload,
        )
        if not isinstance(session, (Session, AsyncSession)):
            raise TypeError(f"Edictum 工厂返回了无效会话类型: {type(session).__name__}")
        plugin_states = self._normalize_plugin_states(definition.metadata.get("plugins", []))
        if plugin_states and (
            not type_definition.supports_plugins
            or type_definition.plugin_installer is None
        ):
            for plugin_state in plugin_states:
                plugin_state.status = "error"
                plugin_state.error = f"Edictum 类型未提供插件安装能力: {type_name}"
        with self._runtime_lock:
            self._runtime_states[id(session)] = _EdictumRuntimeState(
                type_definition=type_definition,
                plugins=plugin_states,
            )
        return session

    @staticmethod
    def _normalize_plugin_states(value: object) -> list[_PluginRuntimeState]:
        """
        将冷配置中的插件项转换为运行时状态

        参数:
        - value: 插件名称或插件配置数组

        返回:
        - list[_PluginRuntimeState]: 规范化后的插件状态
        """
        if not isinstance(value, list):
            return []
        states: list[_PluginRuntimeState] = []
        for item in cast(list[object], value):
            if isinstance(item, str):
                name = item.strip()
                if name:
                    states.append(_PluginRuntimeState(name=name))
                continue
            if not isinstance(item, dict):
                continue
            plugin_item = cast(dict[str, object], item)
            name = str(plugin_item.get("name", "")).strip()
            if not name:
                continue
            raw_config = plugin_item.get("config", {})
            config = (
                dict(cast(dict[str, Any], raw_config))
                if isinstance(raw_config, dict)
                else {}
            )
            enabled = bool(plugin_item.get("enabled", True))
            states.append(
                _PluginRuntimeState(
                    name=name,
                    enabled=enabled,
                    config=config,
                    status="pending" if enabled else "disabled",
                )
            )
        return states

    def prepare_session(self, session: Session | AsyncSession) -> None:
        """
        同步安装会话配置中启用的插件

        参数:
        - session: 已创建的 Edictum 会话
        """
        state = self._get_runtime_state(session)
        if state is None or state.type_definition.plugin_installer is None:
            return
        for plugin_state in state.plugins:
            if plugin_state.status != "pending":
                continue
            self._install_plugin_sync(session, state.type_definition, plugin_state)

    async def prepare_session_async(self, session: Session | AsyncSession) -> None:
        """
        异步安装会话配置中启用的插件

        参数:
        - session: 已创建的 Edictum 会话
        """
        state = self._get_runtime_state(session)
        if state is None or state.type_definition.plugin_installer is None:
            return
        for plugin_state in state.plugins:
            if plugin_state.status != "pending":
                continue
            await self._install_plugin_async(session, state.type_definition, plugin_state)

    def release_session(self, session: Session | AsyncSession) -> None:
        """
        同步卸载已加载插件并释放 Provider 状态

        参数:
        - session: 待释放的 Edictum 会话
        """
        state = self._pop_runtime_state(session)
        if state is None or state.type_definition.plugin_uninstaller is None:
            return
        for plugin_state in reversed(state.plugins):
            if plugin_state.status != "loaded":
                continue
            try:
                result = state.type_definition.plugin_uninstaller(session, plugin_state.name)
                if inspect.isawaitable(result):
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
                    logger.warning(
                        f"[EdictumProvider] 同步释放路径无法等待插件卸载: {plugin_state.name}"
                    )
            except Exception as error:
                logger.warning(f"[EdictumProvider] 插件卸载失败: {plugin_state.name}, {error}")

    async def release_session_async(self, session: Session | AsyncSession) -> None:
        """
        异步卸载已加载插件并释放 Provider 状态

        参数:
        - session: 待释放的 Edictum 会话
        """
        state = self._pop_runtime_state(session)
        if state is None or state.type_definition.plugin_uninstaller is None:
            return
        for plugin_state in reversed(state.plugins):
            if plugin_state.status != "loaded":
                continue
            try:
                result = state.type_definition.plugin_uninstaller(session, plugin_state.name)
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                logger.warning(f"[EdictumProvider] 插件卸载失败: {plugin_state.name}, {error}")

    def get_runtime_metadata(self, session: Session | AsyncSession) -> dict[str, Any]:
        """
        返回前端可展示的插件加载状态

        参数:
        - session: Edictum 会话实例

        返回:
        - dict[str, Any]: 插件状态和汇总信息
        """
        state = self._get_runtime_state(session)
        if state is None:
            return {}
        plugins: list[dict[str, object]] = [
            {
                "name": item.name,
                "enabled": item.enabled,
                "status": item.status,
                "error": item.error,
            }
            for item in state.plugins
        ]
        return {
            "plugins": plugins,
            "plugin_summary": {
                "total": len(state.plugins),
                "loaded": sum(item.status == "loaded" for item in state.plugins),
                "errors": sum(item.status == "error" for item in state.plugins),
                "pending": sum(item.status == "pending" for item in state.plugins),
            },
        }

    def _get_runtime_state(self, session: Session | AsyncSession) -> _EdictumRuntimeState | None:
        """
        获取会话对应的 Provider 状态

        参数:
        - session: Edictum 会话实例

        返回:
        - _EdictumRuntimeState | None: 状态不存在时返回 None
        """
        with self._runtime_lock:
            return self._runtime_states.get(id(session))

    def _pop_runtime_state(self, session: Session | AsyncSession) -> _EdictumRuntimeState | None:
        """
        移除并返回会话对应的 Provider 状态

        参数:
        - session: Edictum 会话实例

        返回:
        - _EdictumRuntimeState | None: 状态不存在时返回 None
        """
        with self._runtime_lock:
            return self._runtime_states.pop(id(session), None)

    @staticmethod
    def _plugin_path(plugin_state: _PluginRuntimeState) -> str:
        """
        解析插件目录, 不存在时抛出明确错误

        参数:
        - plugin_state: 插件运行时状态

        返回:
        - str: 插件目录字符串
        """
        plugin_dir = resolve_plugin_dir(plugin_state.name)
        if plugin_dir is None:
            raise ValueError(f"插件不存在: {plugin_state.name}")
        return str(plugin_dir)

    @classmethod
    def _install_plugin_sync(
        cls,
        session: Session | AsyncSession,
        definition: EdictumTypeDefinition,
        plugin_state: _PluginRuntimeState,
    ) -> None:
        """
        同步安装单个插件并记录结果

        参数:
        - session: Edictum 会话实例
        - definition: Edictum 类型定义
        - plugin_state: 插件运行时状态
        """
        installer = definition.plugin_installer
        if installer is None:
            plugin_state.status = "error"
            plugin_state.error = "Edictum 类型未提供插件安装适配器"
            return
        try:
            result = installer(session, cls._plugin_path(plugin_state), plugin_state.config)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise TypeError("插件安装需要异步生命周期")
            plugin_state.status = "loaded"
            plugin_state.error = None
        except Exception as error:
            plugin_state.status = "error"
            plugin_state.error = str(error)
            logger.error(f"[EdictumProvider] 插件安装失败: {plugin_state.name}, {error}")

    @classmethod
    async def _install_plugin_async(
        cls,
        session: Session | AsyncSession,
        definition: EdictumTypeDefinition,
        plugin_state: _PluginRuntimeState,
    ) -> None:
        """
        异步安装单个插件并记录结果

        参数:
        - session: Edictum 会话实例
        - definition: Edictum 类型定义
        - plugin_state: 插件运行时状态
        """
        installer = definition.plugin_installer
        if installer is None:
            plugin_state.status = "error"
            plugin_state.error = "Edictum 类型未提供插件安装适配器"
            return
        try:
            result = installer(session, cls._plugin_path(plugin_state), plugin_state.config)
            if inspect.isawaitable(result):
                await result
            plugin_state.status = "loaded"
            plugin_state.error = None
        except Exception as error:
            plugin_state.status = "error"
            plugin_state.error = str(error)
            logger.error(f"[EdictumProvider] 插件安装失败: {plugin_state.name}, {error}")
