"""
Edictum 命名会话 Provider

把 Edictum 冷配置和类型注册表转换为统一运行时定义,
新增 Edictum 会话实现时只需注册工厂和配置元数据
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
import hashlib
import inspect
from typing import Any, cast
import json
import time

from satrap.core.framework.providers.base import SessionProviderDefinition
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_runtime import (
    PluginInstallationError,
    PluginRuntimeState,
    install_plugin_spec,
    install_plugin_spec_async,
    preview_plugin_reconciliation,
    reconcile_plugin_states_async,
)
from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.framework.Base import AsyncSession, Session
from satrap.edictum.plugin_spec import parse_plugin_specs, plugin_specs_fingerprint
from satrap.edictum.registry import (
    EDICTUM_PROVIDER,
    EdictumTypeDefinition,
    EdictumTypeRegistry,
)
from satrap.edictum.config import EdictumConfigManager
from satrap.core.type import SessionConfig

from satrap.core.log import logger


@dataclass
class _EdictumRuntimeState:
    """一个 Edictum 会话的 Provider 运行时状态"""

    type_definition: EdictumTypeDefinition
    config_name: str
    plugins: list[PluginRuntimeState] = field(default_factory=list[PluginRuntimeState])
    desired_fingerprint: str = ""
    applied_fingerprint: str = ""
    revision: int = 0
    instance_overrides: dict[str, Any] = field(default_factory=dict[str, Any])
    applied_config: dict[str, Any] = field(default_factory=dict[str, Any])
    desired_config: dict[str, Any] = field(default_factory=dict[str, Any])
    applied_config_fingerprint: str = ""
    desired_config_fingerprint: str = ""
    config_revision: int = 0
    config_status: str = "applied"
    config_error: str | None = None
    last_restarted_at: float | None = None


def _runtime_config_fingerprint(config: dict[str, Any]) -> str:
    """
    计算 Edictum 有效运行时配置指纹

    参数:
    - config: 已规范化的有效运行时配置

    返回:
    - str: SHA-256 配置指纹
    """
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
        self.plugin_catalog = PluginCatalog()
        self._runtime_states: dict[int, _EdictumRuntimeState] = {}
        self._runtime_lock = threading.RLock()

    def _effective_runtime_config(
        self,
        config_name: str,
        instance_overrides: dict[str, Any],
        desired_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        合并命名配置默认值和实例覆盖值

        参数:
        - config_name: Edictum 命名配置
        - instance_overrides: 会话实例专属覆盖参数
        - desired_config: 可选的尚未保存目标配置

        返回:
        - dict[str, Any]: 不含插件和描述字段的有效运行时配置
        """
        raw = (
            dict(desired_config)
            if desired_config is not None
            else dict(self.config_manager.get_config(config_name) or {})
        )
        params = dict(raw.get("params", {}))
        overrides = dict(instance_overrides)
        model_name = str(overrides.pop("model_name", "") or raw.get("model_name", "")).strip()
        overrides.pop("plugins", None)
        params.update(overrides)
        return {
            "edictum_type": str(raw.get("edictum_type", "")).strip(),
            "enabled": bool(raw.get("enabled", True)),
            "model_name": model_name,
            "params": params,
        }

    def _refresh_desired_runtime_config(
        self,
        state: _EdictumRuntimeState,
        desired_config: dict[str, Any] | None = None,
    ) -> None:
        """
        刷新运行时状态中的目标配置

        参数:
        - state: Edictum 运行时状态
        - desired_config: 可选的尚未保存目标配置
        """
        desired = self._effective_runtime_config(
            state.config_name,
            state.instance_overrides,
            desired_config,
        )
        fingerprint = _runtime_config_fingerprint(desired)
        if fingerprint != state.desired_config_fingerprint:
            state.config_revision += 1
        state.desired_config = desired
        state.desired_config_fingerprint = fingerprint

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
            instance_overrides = dict(
                getattr(
                    session_config,
                    "_satrap_instance_overrides",
                    session_config.session_config or {},
                )
            )
            runtime_config = self._effective_runtime_config(name, instance_overrides)
            runtime_config_fingerprint = _runtime_config_fingerprint(runtime_config)
            fingerprint = plugin_specs_fingerprint([
                item.desired_spec
                for item in plugin_states
                if item.desired_spec is not None
            ])
            self._runtime_states[id(session)] = _EdictumRuntimeState(
                type_definition=type_definition,
                config_name=name,
                plugins=plugin_states,
                desired_fingerprint=fingerprint,
                applied_fingerprint="",
                instance_overrides=instance_overrides,
                applied_config=runtime_config,
                desired_config=runtime_config,
                applied_config_fingerprint=runtime_config_fingerprint,
                desired_config_fingerprint=runtime_config_fingerprint,
                last_restarted_at=time.time(),
            )
        return session

    def _normalize_plugin_states(self, value: object) -> list[PluginRuntimeState]:
        """
        将冷配置中的插件项转换为运行时状态

        参数:
        - value: 插件名称或插件配置数组

        返回:
        - list[PluginRuntimeState]: 规范化后的插件状态
        """
        specs = parse_plugin_specs(value, self.plugin_catalog, require_available=False)
        return [
            PluginRuntimeState(
                applied_spec=spec if not spec.enabled else None,
                desired_spec=spec,
                status="pending" if spec.enabled else "disabled",
                last_operation_status="pending" if spec.enabled else "unchanged",
            )
            for spec in specs
        ]

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
        self._refresh_runtime_fingerprint(state)
        failures = [
            item
            for item in state.plugins
            if item.status == "error"
            and item.desired_spec is not None
            and item.desired_spec.enabled
        ]
        if failures:
            raise RuntimeError(f"Edictum 插件准备失败: {', '.join(item.name for item in failures)}")

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
        self._refresh_runtime_fingerprint(state)
        failures = [
            item
            for item in state.plugins
            if item.status == "error"
            and item.desired_spec is not None
            and item.desired_spec.enabled
        ]
        if failures:
            raise RuntimeError(f"Edictum 插件准备失败: {', '.join(item.name for item in failures)}")

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
        self._refresh_desired_runtime_config(state)
        config_drift = (
            state.applied_config_fingerprint != state.desired_config_fingerprint
        )
        if config_drift and state.config_status == "applied":
            state.config_status = "restart_pending"
        elif not config_drift and state.config_status != "error":
            state.config_status = "applied"
        plugins: list[dict[str, object]] = [
            {
                "name": item.name,
                "enabled": item.enabled,
                "status": item.status,
                "error": item.error,
                "last_error": item.last_error,
                "last_operation_status": item.last_operation_status,
                "revision": item.revision,
                "capabilities": {
                    "applied": (
                        item.applied_spec.capabilities
                        if item.applied_spec is not None
                        else {}
                    ),
                    "desired": (
                        item.desired_spec.capabilities
                        if item.desired_spec is not None
                        else {}
                    ),
                },
                "drift": item.drift,
                "restart_required": item.restart_required,
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
                "drift": sum(item.drift for item in state.plugins),
                "restart_required": sum(item.restart_required for item in state.plugins),
            },
            "plugin_revision": state.revision,
            "plugin_fingerprint": {
                "applied": state.applied_fingerprint,
                "desired": state.desired_fingerprint,
            },
            "config": {
                "status": state.config_status,
                "error": state.config_error,
                "drift": config_drift,
                "revision": state.config_revision,
                "applied": state.applied_config,
                "desired": state.desired_config,
                "applied_fingerprint": state.applied_config_fingerprint,
                "desired_fingerprint": state.desired_config_fingerprint,
                "last_restarted_at": state.last_restarted_at,
            },
        }

    def preview_session_runtime(
        self,
        session: Session | AsyncSession,
        *,
        desired_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        预览完整 Edictum 配置对一个活跃会话的影响

        参数:
        - session: 活跃 Edictum 会话
        - desired_config: 可选的尚未保存目标配置

        返回:
        - dict[str, Any]: 变更动作、字段和插件差量
        """
        state = self._get_runtime_state(session)
        if state is None:
            return {"ok": False, "error": "Edictum 运行时状态不存在"}
        self._refresh_desired_runtime_config(state, desired_config)
        changed_fields = [
            field_name
            for field_name in ("edictum_type", "enabled", "model_name", "params")
            if state.applied_config.get(field_name) != state.desired_config.get(field_name)
        ]
        raw = (
            desired_config
            if desired_config is not None
            else self.config_manager.get_config(state.config_name)
        )
        desired_plugins: object = raw.get("plugins", []) if raw is not None else []
        plugin_preview = self.preview_session_plugins(
            session,
            desired_plugins=desired_plugins,
        )
        raw_plugin_changes: object = plugin_preview.get("plugins", [])
        plugin_changes: list[dict[str, Any]] = []
        if isinstance(raw_plugin_changes, list):
            for raw_item in cast(list[object], raw_plugin_changes):
                if not isinstance(raw_item, dict):
                    continue
                item = dict(cast(dict[str, Any], raw_item))
                if item.get("action") not in {"noop", None}:
                    plugin_changes.append(item)
        if not state.desired_config.get("enabled", True):
            action = "unload"
        elif changed_fields:
            action = "restart"
        elif plugin_changes:
            action = "reconcile_plugins"
        else:
            action = "noop"
        return {
            "ok": True,
            "config_name": state.config_name,
            "action": action,
            "changed_fields": changed_fields,
            "config_revision": state.config_revision,
            "applied_config_fingerprint": state.applied_config_fingerprint,
            "desired_config_fingerprint": state.desired_config_fingerprint,
            "plugins": plugin_preview.get("plugins", []),
        }

    def mark_runtime_restart_failed(
        self,
        session: Session | AsyncSession,
        error: str,
    ) -> None:
        """
        标记候选实例创建失败且旧运行时被保留

        参数:
        - session: 仍在服务的旧会话
        - error: 热重启错误
        """
        state = self._get_runtime_state(session)
        if state is None:
            return
        self._refresh_desired_runtime_config(state)
        state.config_status = "error"
        state.config_error = error

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
    def _install_plugin_sync(
        session: Session | AsyncSession,
        definition: EdictumTypeDefinition,
        plugin_state: PluginRuntimeState,
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
            target = plugin_state.desired_spec or plugin_state.applied_spec
            if target is None:
                raise ValueError("插件目标规格不存在")
            plugin, _changes = install_plugin_spec(
                lambda path, config: installer(session, path, config),
                target,
            )
            plugin_state.handle = plugin
            plugin_state.applied_spec = plugin_state.desired_spec
            plugin_state.mark("applied", status="loaded")
        except Exception as error:
            if isinstance(error, PluginInstallationError):
                uninstaller = definition.plugin_uninstaller
                if uninstaller is not None:
                    try:
                        cleanup = uninstaller(session, plugin_state.name)
                        if inspect.isawaitable(cleanup):
                            close = getattr(cleanup, "close", None)
                            if callable(close):
                                close()
                            logger.warning(
                                f"[EdictumProvider] 同步准备无法等待失败插件清理: {plugin_state.name}"
                            )
                    except Exception as cleanup_error:
                        logger.warning(
                            f"[EdictumProvider] 失败插件清理失败: {plugin_state.name}, {cleanup_error}"
                        )
            plugin_state.mark("error", status="error", error=str(error))
            logger.error(f"[EdictumProvider] 插件安装失败: {plugin_state.name}, {error}")

    @staticmethod
    async def _install_plugin_async(
        session: Session | AsyncSession,
        definition: EdictumTypeDefinition,
        plugin_state: PluginRuntimeState,
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
            target = plugin_state.desired_spec or plugin_state.applied_spec
            if target is None:
                raise ValueError("插件目标规格不存在")
            plugin, _changes = await install_plugin_spec_async(
                lambda path, config: installer(session, path, config),
                target,
            )
            plugin_state.handle = plugin
            plugin_state.applied_spec = plugin_state.desired_spec
            plugin_state.mark("applied", status="loaded")
        except Exception as error:
            if isinstance(error, PluginInstallationError):
                uninstaller = definition.plugin_uninstaller
                if uninstaller is not None:
                    try:
                        cleanup = uninstaller(session, plugin_state.name)
                        if inspect.isawaitable(cleanup):
                            await cleanup
                    except Exception as cleanup_error:
                        logger.warning(
                            f"[EdictumProvider] 失败插件清理失败: {plugin_state.name}, {cleanup_error}"
                        )
            plugin_state.mark("error", status="error", error=str(error))
            logger.error(f"[EdictumProvider] 插件安装失败: {plugin_state.name}, {error}")

    async def reconcile_session_plugins_async(
        self,
        session: Session | AsyncSession,
        *,
        desired_plugins: object | None = None,
    ) -> dict[str, Any]:
        """
        把命名冷配置中的完整插件变化应用到一个活跃会话

        参数:
        - session: 活跃 Edictum 会话
        - desired_plugins: 可选目标插件配置, None 时读取当前冷配置

        返回:
        - dict[str, Any]: 生命周期协调结果
        """
        state = self._get_runtime_state(session)
        if state is None:
            return {"ok": False, "error": "Edictum 运行时状态不存在"}
        if desired_plugins is None:
            config = self.config_manager.get_config(state.config_name)
            if config is None:
                desired_plugins = []
            else:
                desired_plugins = config.get("plugins", [])
        desired_specs = parse_plugin_specs(
            desired_plugins,
            self.plugin_catalog,
            require_available=False,
        )
        state.desired_fingerprint = plugin_specs_fingerprint(desired_specs)
        definition = state.type_definition
        installer = definition.plugin_installer
        if installer is None:
            return {
                "ok": False,
                "config_name": state.config_name,
                "error": "Edictum 类型未提供插件安装适配器",
            }
        uninstaller = definition.plugin_uninstaller
        result = await reconcile_plugin_states_async(
            state.plugins,
            desired_specs,
            lambda path, config: installer(session, path, config),
            (
                None
                if uninstaller is None
                else lambda name: uninstaller(session, name)
            ),
        )
        state.revision += 1
        self._refresh_runtime_fingerprint(state)
        return {
            **result,
            "config_name": state.config_name,
            "revision": state.revision,
            "applied_fingerprint": state.applied_fingerprint,
            "desired_fingerprint": state.desired_fingerprint,
        }

    def preview_session_plugins(
        self,
        session: Session | AsyncSession,
        *,
        desired_plugins: object | None = None,
    ) -> dict[str, Any]:
        """
        预览一个活跃会话的插件协调影响

        参数:
        - session: 活跃 Edictum 会话
        - desired_plugins: 可选目标插件配置, None 时读取当前冷配置

        返回:
        - dict[str, Any]: 逐插件影响和运行时版本
        """
        state = self._get_runtime_state(session)
        if state is None:
            return {"ok": False, "error": "Edictum 运行时状态不存在"}
        if desired_plugins is None:
            config = self.config_manager.get_config(state.config_name)
            desired_plugins = config.get("plugins", []) if config is not None else []
        desired_specs = parse_plugin_specs(
            desired_plugins,
            self.plugin_catalog,
            require_available=False,
        )
        return {
            "ok": True,
            "config_name": state.config_name,
            "revision": state.revision,
            "applied_fingerprint": state.applied_fingerprint,
            "desired_fingerprint": plugin_specs_fingerprint(desired_specs),
            "plugins": preview_plugin_reconciliation(state.plugins, desired_specs),
        }

    @staticmethod
    def _refresh_runtime_fingerprint(state: _EdictumRuntimeState) -> None:
        """
        根据已应用插件规格刷新运行时指纹

        参数:
        - state: Edictum 会话运行状态
        """
        applied_specs = [
            item.applied_spec
            for item in state.plugins
            if item.applied_spec is not None
        ]
        state.applied_fingerprint = plugin_specs_fingerprint(applied_specs)
