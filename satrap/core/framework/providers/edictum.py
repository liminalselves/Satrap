"""
Edictum 命名会话 Provider

把 Edictum 冷配置和类型注册表转换为统一运行时定义,
新增 Edictum 会话实现时只需注册工厂和配置元数据
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import threading
import hashlib
import inspect
from typing import Any, cast, Iterator, TYPE_CHECKING
import json
import time

from satrap.core.framework.providers.base import SessionProviderDefinition
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_apply import EdictumPluginApplySnapshot
from satrap.edictum.plugin_settings import resolve_runtime_specs
from satrap.core.utils.performance import PhaseTimings
from satrap.edictum.plugin_runtime import (
    PluginRuntimeState,
    preview_plugin_reconciliation,
    reconcile_plugin_states_async,
    reconcile_plugin_states,
)
from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.framework.Base import AsyncSession, Session
from satrap.edictum.plugin_spec import PluginSpec, parse_plugin_specs, plugin_specs_fingerprint
from satrap.edictum.registry import (
    EDICTUM_PROVIDER,
    EdictumTypeDefinition,
    EdictumTypeRegistry,
)
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.settings import normalize_session_settings
from satrap.core.type import SessionConfig

from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.core.framework.BackGroundManager import ModelConfigManager


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
    config_applied_at: float | None = None
    applied_revision: str | None = None


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
        instance_overrides: dict[str, Any] | None = None,
    ) -> None:
        """
        刷新运行时状态中的目标配置

        参数:
        - state: Edictum 运行时状态
        - desired_config: 可选的尚未保存目标配置
        - instance_overrides: 可选最新实例覆盖, 在操作锁内从存储核对
        """
        if instance_overrides is not None:
            state.instance_overrides = dict(instance_overrides)
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

    def get_definition(self, name: str, *, apply_snapshot: EdictumPluginApplySnapshot | None = None) -> SessionProviderDefinition | None:
        """
        获取统一 Edictum 命名会话定义

        参数:
        - name: 命名配置名称
        - apply_snapshot: 可选本次应用目标, 默认读取当前内存冷配置

        返回:
        - SessionProviderDefinition | None: 配置不存在时返回 None
        """
        config = apply_snapshot.config_for(name) if apply_snapshot is not None else self.config_manager.get_config(name)
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
        *, apply_snapshot: EdictumPluginApplySnapshot | None = None,
    ) -> Session | AsyncSession:
        """
        根据实例配置创建 Edictum 会话

        参数:
        - session_config: 持久化的会话实例配置
        - llm: 已按类型构建的同步或异步模型
        - apply_snapshot: 可选候选实例来源, 不进入会话持久化或工具执行上下文

        返回:
        - Session | AsyncSession: 已创建的 Edictum 会话实例
        """
        name = session_config.session_type_name or ""
        definition = self.get_definition(name, apply_snapshot=apply_snapshot)
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
        payload = {**definition.params, **(session_config.session_config or {})}
        if type_name in {"simple", "async_simple"}:
            payload = normalize_session_settings(payload)
        payload.pop("model_name", None)
        payload.pop("plugins", None)
        payload.pop("plugin_environment", None)
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
        plugin_states = self._normalize_plugin_states(definition.metadata.get("plugins", []),
                                                     specs=apply_snapshot.specs_for(name) if apply_snapshot is not None else None)
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
            runtime_config = self._effective_runtime_config(name, instance_overrides,
                                                           apply_snapshot.config_for(name) if apply_snapshot is not None else None)
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
                config_applied_at=time.time() if type_definition.plugin_installer is None else None,
                applied_revision=runtime_config_fingerprint if type_definition.plugin_installer is None else None,
            )
        return session

    def _normalize_plugin_states(self, value: object, *, specs: list[PluginSpec] | None = None) -> list[PluginRuntimeState]:
        """
        将冷配置中的插件项转换为运行时状态

        参数:
        - value: 插件名称或插件配置数组
        - specs: 可选已捕获规格, 提供后不重新解析 value

        返回:
        - list[PluginRuntimeState]: 规范化后的插件状态
        """
        specs = specs if specs is not None else parse_plugin_specs(value, self.plugin_catalog, require_available=False)
        return [
            PluginRuntimeState(
                applied_spec=spec if not spec.enabled else None,
                desired_spec=spec,
                status="pending" if spec.enabled else "disabled",
                last_operation_status="pending" if spec.enabled else "unchanged",
            )
            for spec in specs
        ]

    def prepare_session(self, session: Session | AsyncSession, *, apply_snapshot: EdictumPluginApplySnapshot | None = None) -> None:
        """
        同步安装会话配置中启用的插件

        参数:
        - session: 已创建的 Edictum 会话
        - apply_snapshot: 可选完整应用目标, 默认保持正常会话准备流程
        """
        state = self._get_runtime_state(session)
        if state is None or state.type_definition.plugin_installer is None:
            return
        if apply_snapshot is not None:
            with self._application_models(session, apply_snapshot):
                specs = resolve_runtime_specs(session, apply_snapshot.specs_for(state.config_name), apply_snapshot.sources.catalog,
                                              snapshot=apply_snapshot.sources)
                installer = state.type_definition.plugin_installer
                uninstaller = state.type_definition.plugin_uninstaller
                result = reconcile_plugin_states(state.plugins, specs, lambda path, values: installer(session, path, values),
                                                 (lambda name: uninstaller(session, name)) if uninstaller else None, session=session)
            self._refresh_runtime_fingerprint(state)
            if not result.get("ok", False):
                raise RuntimeError("候选会话插件准备失败, 请检查插件运行状态")
            return
        self.config_manager.reload()
        models = getattr(session, "plugin_model_manager", None)
        if models is not None:
            models.reload()
        if not any(item.status == "pending" for item in state.plugins):
            config = self.config_manager.get_config(state.config_name) or {}
            specs = resolve_runtime_specs(session, parse_plugin_specs(config.get("plugins", []), self.plugin_catalog), self.plugin_catalog)
            installer = state.type_definition.plugin_installer
            uninstaller = state.type_definition.plugin_uninstaller
            result = reconcile_plugin_states(
                state.plugins, specs, lambda path, values: installer(session, path, values),
                (lambda name: uninstaller(session, name)) if uninstaller else None, session=session,
            )
            self._refresh_runtime_fingerprint(state)
            if not result.get("ok", False):
                raise RuntimeError("会话插件配置更新失败, 请检查插件运行状态")
            return
        specs = [resolve_runtime_specs(session, [item.desired_spec], self.plugin_catalog)[0]
                 for item in state.plugins if item.desired_spec is not None]
        installer = state.type_definition.plugin_installer
        uninstaller = state.type_definition.plugin_uninstaller
        result = reconcile_plugin_states(
            state.plugins, specs, lambda path, values: installer(session, path, values),
            (lambda name: uninstaller(session, name)) if uninstaller else None,
            session=session,
        )
        self._refresh_runtime_fingerprint(state)
        if not result.get("ok", False):
            failures = [item.name for item in state.plugins if item.error and item.desired_spec is not None and item.desired_spec.enabled]
            raise RuntimeError(f"Edictum 插件准备失败: {', '.join(failures)}")

    async def prepare_session_async(self, session: Session | AsyncSession, *, apply_snapshot: EdictumPluginApplySnapshot | None = None) -> None:
        """
        异步安装会话配置中启用的插件

        参数:
        - session: 已创建的 Edictum 会话
        - apply_snapshot: 可选完整应用目标, 默认保持正常会话准备流程
        """
        state = self._get_runtime_state(session)
        if state is None or state.type_definition.plugin_installer is None:
            return
        if apply_snapshot is not None:
            result = await self.reconcile_session_plugins_async(session, apply_snapshot=apply_snapshot)
            if not result.get("ok", False):
                raise RuntimeError("候选会话插件准备失败, 请检查插件运行状态")
            return
        self.config_manager.reload()
        models = getattr(session, "plugin_model_manager", None)
        if models is not None:
            models.reload()
        if not any(item.status == "pending" for item in state.plugins):
            result = await self.reconcile_session_plugins_async(
                session, desired_plugins=getattr(session, "_satrap_group_plugins", None),
            )
            if not result.get("ok", False):
                raise RuntimeError("会话插件配置更新失败, 请检查插件运行状态")
            return
        specs = [resolve_runtime_specs(session, [item.desired_spec], self.plugin_catalog)[0]
                 for item in state.plugins if item.desired_spec is not None]
        installer = state.type_definition.plugin_installer
        uninstaller = state.type_definition.plugin_uninstaller
        result = await reconcile_plugin_states_async(
            state.plugins, specs, lambda path, values: installer(session, path, values),
            (lambda name: uninstaller(session, name)) if uninstaller else None,
            session=session,
        )
        self._refresh_runtime_fingerprint(state)
        if not result.get("ok", False):
            failures = [item.name for item in state.plugins if item.error and item.desired_spec is not None and item.desired_spec.enabled]
            raise RuntimeError(f"Edictum 插件准备失败: {', '.join(failures)}")

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
        plugins = [item.to_payload() for item in state.plugins]
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
                "config_applied_at": state.config_applied_at,
                "applied_revision": state.applied_revision,
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
        apply_snapshot: EdictumPluginApplySnapshot | None = None,
        target_config_name: str | None = None,
        instance_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        预览完整 Edictum 配置对一个活跃会话的影响

        参数:
        - session: 活跃 Edictum 会话
        - desired_config: 可选的尚未保存目标配置
        - apply_snapshot: 可选固定应用来源
        - target_config_name: 可选当前持久化绑定, 配置重命名时核验新名称
        - instance_overrides: 可选操作锁内读取的最新实例覆盖

        返回:
        - dict[str, Any]: 变更动作、字段和插件差量
        """
        state = self._get_runtime_state(session)
        if state is None:
            return {"ok": False, "error": "Edictum 运行时状态不存在"}
        name = target_config_name or state.config_name
        if apply_snapshot is not None:
            desired_config = apply_snapshot.config_for(name)
            if desired_config is None:
                desired_config = {"enabled": False}   # 已删除目标只能卸载, 不用空默认配置重建实例
        self._refresh_desired_runtime_config(state, desired_config, instance_overrides)
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
            apply_snapshot=apply_snapshot,
            target_config_name=name,
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
        elif changed_fields or name != state.config_name:
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

    async def reconcile_session_plugins_async(
        self,
        session: Session | AsyncSession,
        *,
        desired_plugins: object | None = None,
        apply_snapshot: EdictumPluginApplySnapshot | None = None,
    ) -> dict[str, Any]:
        """
        把命名冷配置中的完整插件变化应用到一个活跃会话

        参数:
        - session: 活跃 Edictum 会话
        - desired_plugins: 可选目标插件配置, None 时读取当前冷配置
        - apply_snapshot: 可选本次共享来源, 默认保持单会话实时解析

        返回:
        - dict[str, Any]: 生命周期协调结果
        """
        with self._application_models(session, apply_snapshot):
            return await self._reconcile_session_plugins_from_sources_async(session, desired_plugins=desired_plugins, apply_snapshot=apply_snapshot)

    @contextmanager
    def _application_models(self, session: Session | AsyncSession, apply_snapshot: EdictumPluginApplySnapshot | None) -> Iterator[None]:
        """
        操作锁内显式使用捕获模型资源, 结束后恢复运行期管理器

        参数:
        - session: 当前或候选实例
        - apply_snapshot: 可选完整应用来源

        返回:
        - 仅在本次资源合成与安装期间生效的作用域
        """
        original = session.plugin_model_manager
        if apply_snapshot is None or not apply_snapshot.has_model_sources:
            yield
            return
        session.plugin_model_manager = apply_snapshot.models_for(cast("ModelConfigManager | None", original))
        try:
            yield
        finally:
            session.plugin_model_manager = original

    async def _reconcile_session_plugins_from_sources_async(
        self, session: Session | AsyncSession, *, desired_plugins: object | None,
        apply_snapshot: EdictumPluginApplySnapshot | None,
    ) -> dict[str, Any]:
        """
        在已选定模型来源的作用域内执行原插件协调

        参数:
        - session: 当前实例
        - desired_plugins: 可选目标规格
        - apply_snapshot: 可选本次固定来源

        返回:
        - 原生命周期协调结果
        """
        timings = PhaseTimings()
        state = self._get_runtime_state(session)
        if state is None:
            return {"ok": False, "error": "Edictum 运行时状态不存在"}
        with timings.phase('config_read_ms'):
            if apply_snapshot is not None:
                catalog = apply_snapshot.sources.catalog
                desired_specs = apply_snapshot.specs_for(state.config_name)
                desired_specs = resolve_runtime_specs(session, desired_specs, catalog, snapshot=apply_snapshot.sources)
            else:
                if desired_plugins is None:
                    config = self.config_manager.get_config(state.config_name)
                    desired_plugins = config.get("plugins", []) if config is not None else []
                catalog = self.plugin_catalog.snapshot()
                desired_specs = parse_plugin_specs(desired_plugins, catalog, require_available=False)
                desired_specs = resolve_runtime_specs(session, desired_specs, catalog)
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
            session=session,
        )
        state.revision += 1
        self._refresh_runtime_fingerprint(state)
        result['timings_ms'] = {**result.get('timings_ms', {}), **timings.to_payload()}
        return {
            **result,
            "config_name": state.config_name,
            "revision": state.revision,
            "applied_fingerprint": state.applied_fingerprint,
            "desired_fingerprint": state.desired_fingerprint,
        }

    def capture_plugin_application(self, scope: dict[str, bool], *, desired_plugins: object | None = None,
                                   plugin_name: str | None = None, desired_config: dict[str, Any] | None = None,
                                   model_managers: tuple[ModelConfigManager, ...] = (), include_main_models: bool = True) -> EdictumPluginApplySnapshot:
        """
        准备当前平台直接应用的共享来源, 后端跨平台入口可提供同一快照

        参数:
        - scope: 候选配置名称及旧插件引用标记
        - desired_plugins: 可选临时目标规格
        - plugin_name: 可选引用过滤
        - desired_config: 可选完整临时运行目标
        - model_managers: 可选应用的只读模型来源
        - include_main_models: 完整应用捕获主模型, 仅插件应用按引用 schema 判断

        返回:
        - 命名配置与全局参数的同代快照
        """
        return EdictumPluginApplySnapshot.capture(self.config_manager, set(scope), desired_plugins=desired_plugins,
                                                plugin_name=plugin_name, retained_config_names={name for name, old in scope.items() if old}, desired_config=desired_config,
                                                model_managers=model_managers, include_main_models=include_main_models)

    def references_plugin(self, session: Session | AsyncSession, plugin_name: str, *, desired_plugins: object | None = None) -> bool:
        """
        检查当前目标或旧运行时是否引用指定插件, 不读取插件全局参数

        参数:
        - session: 活跃 Edictum 会话
        - plugin_name: 已选择的插件名称
        - desired_plugins: 可选目标插件列表, 默认读取命名冷配置

        返回:
        - 新旧任一引用存在时为 True, 移除引用仍进入卸载流程
        """
        state = self._get_runtime_state(session)
        if state is None:
            return False
        if any(item.name == plugin_name for item in state.plugins):
            return True
        if desired_plugins is None:
            config = self.config_manager.get_config(state.config_name)
            desired_plugins = config.get("plugins", []) if config is not None else []
        if not isinstance(desired_plugins, list):
            return False
        return any((item if isinstance(item, str) else item.get("name") if isinstance(item, dict) else None) == plugin_name for item in desired_plugins)

    def plugin_config_name(self, session: Session | AsyncSession) -> str | None:
        """
        返回运行时命名配置标识, 不读取冷配置或全局参数

        参数:
        - session: 活跃 Edictum 实例

        返回:
        - 配置名称, 无运行状态时为 None
        """
        state = self._get_runtime_state(session)
        return state.config_name if state is not None else None

    def preview_session_plugins(
        self,
        session: Session | AsyncSession,
        *,
        desired_plugins: object | None = None,
        apply_snapshot: EdictumPluginApplySnapshot | None = None,
        target_config_name: str | None = None,
    ) -> dict[str, Any]:
        """
        预览一个活跃会话的插件协调影响

        参数:
        - session: 活跃 Edictum 会话
        - desired_plugins: 可选目标插件配置, None 时读取当前冷配置
        - apply_snapshot: 可选固定应用来源, 默认实时读取参数
        - target_config_name: 可选新的命名配置标识

        返回:
        - dict[str, Any]: 逐插件影响和运行时版本
        """
        state = self._get_runtime_state(session)
        if state is None:
            return {"ok": False, "error": "Edictum 运行时状态不存在"}
        if desired_plugins is None:
            config = self.config_manager.get_config(state.config_name)
            desired_plugins = config.get("plugins", []) if config is not None else []
        if apply_snapshot is not None:
            with self._application_models(session, apply_snapshot):
                desired_specs = resolve_runtime_specs(session, apply_snapshot.specs_for(target_config_name or state.config_name),
                                                      apply_snapshot.sources.catalog, snapshot=apply_snapshot.sources)
        else:
            desired_specs = parse_plugin_specs(desired_plugins, self.plugin_catalog, require_available=False)
            desired_specs = resolve_runtime_specs(session, desired_specs, self.plugin_catalog)
        return {
            "ok": True,
            "config_name": state.config_name,
            "revision": state.revision,
            "applied_fingerprint": state.applied_fingerprint,
            "desired_fingerprint": plugin_specs_fingerprint(desired_specs),
            "plugins": preview_plugin_reconciliation(state.plugins, desired_specs, session=session),
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
        if all(not item.drift and not item.error for item in state.plugins):
            revision = hashlib.sha256((state.applied_config_fingerprint + ":" + state.applied_fingerprint).encode()).hexdigest()
            if revision != state.applied_revision:
                state.applied_revision = revision
                state.config_applied_at = time.time()
