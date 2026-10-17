"""插件运行协调器: 统一安装插件并通过插件命名空间应用子能力"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import inspect
import traceback
from typing import Any, Awaitable, Callable, Iterable, cast
from pathlib import Path
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from satrap.edictum.plugin_spec import PluginSpec
from satrap.edictum.plugin_settings import EffectivePluginConfig, PluginInstallConfig
from satrap.edictum.plugin import CAPABILITY_KINDS, Plugin, load_plugin_meta
from satrap.core.plugin_interop.capabilities import PluginInterop, _RelationNode, _validate_relation_target
from satrap.core.plugin_interop.contracts import CapabilityKind, InteropSpec, parse_interop
from satrap.core.utils.performance import PhaseTimings
from satrap.core.utils.application_progress import progress_phase

from satrap.core.log import logger
from satrap.core.config.model_snapshot import ModelConfigReader
from satrap.edictum.plugin_resources import PluginResources


_CAPABILITY_SINGULAR = {
    "tools": "tool",
    "services": "service",
    "skills": "skill",
    "mcp": "mcp",
    "handlers": "handler",
    "commands": "command",
}
"""能力类别到插件命名空间方法后缀的映射"""


PluginInstaller = Callable[[str, dict[str, Any] | None], object]
AsyncPluginInstaller = Callable[[str, dict[str, Any] | None], object | Awaitable[object]]
AsyncPluginUninstaller = Callable[[str], bool | Awaitable[bool]]
"""插件安装器签名"""


def reconcile_plugin_states(
    states: list[PluginRuntimeState], desired_specs: Iterable[PluginSpec],
    installer: PluginInstaller, uninstaller: AsyncPluginUninstaller | None,
    *, session: object | None = None,
) -> dict[str, Any]:
    """同步入口复用同一协调状态机, 已有事件循环时隔离运行临时循环"""
    def run() -> dict[str, Any]:
        return asyncio.run(reconcile_plugin_states_async(states, desired_specs, installer, uninstaller, session=session))
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run()
    context = copy_context()
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(context.run, run).result()


class PluginInstallationError(RuntimeError):
    """插件主体已安装但子能力状态应用失败"""

    def __init__(self, plugin_name: str, handle: object, cause: Exception) -> None:
        """
        初始化包含失败插件句柄的异常

        参数:
        - plugin_name: 插件名称
        - handle: 已创建的插件句柄
        - cause: 子能力应用异常
        """
        super().__init__(str(cause))
        self.plugin_name = plugin_name
        self.handle = handle
        self.cause = cause


@dataclass
class PluginRuntimeState:
    """单个插件的已应用状态, 目标状态和最近一次协调结果"""

    applied_spec: PluginSpec | None = None
    desired_spec: PluginSpec | None = None
    handle: object | None = field(default=None, repr=False)
    status: str = "pending"
    error: str | None = None
    last_error: str | None = None
    last_operation_status: str = "pending"
    revision: int = 0
    updated_at: float = field(default_factory=time.time)

    @property
    def name(self) -> str:
        """返回插件名称"""
        spec = self.desired_spec or self.applied_spec
        return spec.name if spec is not None else ""

    @property
    def enabled(self) -> bool:
        """返回已经实际应用的聚合启用状态"""
        return bool(self.applied_spec is not None and self.applied_spec.enabled)

    @property
    def drift(self) -> bool:
        """判断目标规格与实际规格是否存在漂移"""
        return self.desired_spec != self.applied_spec

    @property
    def restart_required(self) -> bool:
        """判断当前漂移是否只能通过重新激活会话恢复"""
        return self.last_operation_status == "restart_required"

    def to_payload(self) -> dict[str, Any]:
        """
        返回不包含参数值的运行快照, 区分声明开关与实际加载能力

        返回:
        - 聚合状态、错误、漂移及能力状态; 非标准句柄不推测其加载能力
        """
        loaded = {kind: dict(getattr(self.handle, kind)) for kind in CAPABILITY_KINDS} if isinstance(self.handle, Plugin) else {}
        effective = self.handle.list_capabilities() if isinstance(self.handle, Plugin) else {}
        return {"name": self.name, "enabled": self.enabled, "status": self.status, "error": self.error,
                "last_error": self.last_error, "last_operation_status": self.last_operation_status, "revision": self.revision,
                "capabilities": {"applied": self.applied_spec.capabilities if self.applied_spec else {},
                                 "desired": self.desired_spec.capabilities if self.desired_spec else {}, "loaded": loaded,
                                 "effective": effective,
                                 "loaded_known": isinstance(self.handle, Plugin) or self.handle is None},
                "drift": self.drift, "restart_required": self.restart_required}

    def mark(
        self,
        operation_status: str,
        *,
        status: str | None = None,
        error: str | None = None,
    ) -> None:
        """
        记录一次协调结果

        参数:
        - operation_status: 协调结果状态
        - status: 可选运行状态
        - error: 可选错误详情
        """
        self.last_operation_status = operation_status
        if status is not None:
            self.status = status
        self.error = error
        if error is not None:
            self.last_error = error
        self.revision += 1
        self.updated_at = time.time()


def _capability_method(plugin: object, kind: str, enabled: bool) -> Callable[[str], Any]:
    """
    获取插件命名空间中的能力启停方法

    参数:
    - plugin: 已安装插件句柄
    - kind: 能力类别
    - enabled: 目标启用状态

    返回:
    - Callable[[str], Any]: 能力启停方法
    """
    singular = _CAPABILITY_SINGULAR[kind]
    method = getattr(plugin, f"{'enable' if enabled else 'disable'}_{singular}", None)
    if not callable(method):
        raise TypeError(f"插件句柄不支持能力启停: {kind}")
    return method


def capability_changes(
    previous: PluginSpec | None,
    desired: PluginSpec,
) -> list[tuple[str, str, bool]]:
    """
    计算需要应用的子能力差量

    参数:
    - previous: 已应用规格, None 表示新安装插件
    - desired: 目标规格

    返回:
    - list[tuple[str, str, bool]]: 能力类别, 名称和目标状态
    """
    changes: list[tuple[str, str, bool]] = []
    for kind in CAPABILITY_KINDS:
        desired_states = desired.capabilities.get(kind, {})
        previous_states = previous.capabilities.get(kind, {}) if previous is not None else {}
        for name, enabled in desired_states.items():
            if (previous is None and not enabled) or (
                previous is not None and previous_states.get(name, True) != enabled
            ):
                changes.append((kind, name, enabled))
        if previous is not None:
            for name, enabled in previous_states.items():
                if name not in desired_states and not enabled:
                    changes.append((kind, name, True))
    return changes


def apply_plugin_capabilities(
    plugin: object,
    desired: PluginSpec,
    previous: PluginSpec | None = None,
) -> list[str]:
    """
    同步应用插件子能力差量

    参数:
    - plugin: 已安装插件句柄
    - desired: 目标规格
    - previous: 已应用规格

    返回:
    - list[str]: 已执行的变更描述
    """
    applied: list[str] = []
    for kind, name, enabled in capability_changes(previous, desired):
        result = _capability_method(plugin, kind, enabled)(name)
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise TypeError(f"同步插件能力需要异步生命周期: {kind}.{name}")
        if result is False:
            raise ValueError(f"插件能力不存在或无法切换: {desired.name}.{kind}.{name}")
        applied.append(f"{'enable' if enabled else 'disable'} {kind}.{name}")
    return applied


async def apply_plugin_capabilities_async(
    plugin: object,
    desired: PluginSpec,
    previous: PluginSpec | None = None,
) -> list[str]:
    """
    异步应用插件子能力差量

    参数:
    - plugin: 已安装插件句柄
    - desired: 目标规格
    - previous: 已应用规格

    返回:
    - list[str]: 已执行的变更描述
    """
    applied: list[str] = []
    for kind, name, enabled in capability_changes(previous, desired):
        result = _capability_method(plugin, kind, enabled)(name)
        if inspect.isawaitable(result):
            result = await result
        if result is False:
            raise ValueError(f"插件能力不存在或无法切换: {desired.name}.{kind}.{name}")
        applied.append(f"{'enable' if enabled else 'disable'} {kind}.{name}")
    return applied


def install_plugin_spec(installer: PluginInstaller, spec: PluginSpec) -> tuple[object, list[str]]:
    """
    同步安装插件并应用全部子能力状态

    参数:
    - installer: 插件安装器
    - spec: 插件运行规格

    返回:
    - tuple[object, list[str]]: 插件句柄和能力变更
    """
    if not spec.path:
        raise ValueError(f"插件不存在: {spec.name}")
    config_type = EffectivePluginConfig if spec.config_resolved else PluginInstallConfig
    plugin = installer(spec.path, config_type(spec.config, initial_skills=spec.capabilities.get("skills"), initial_capabilities=spec.capabilities))
    try:
        changes = apply_plugin_capabilities(plugin, spec)
    except Exception as error:
        raise PluginInstallationError(spec.name, plugin, error) from error
    return plugin, changes


async def install_plugin_spec_async(
    installer: AsyncPluginInstaller,
    spec: PluginSpec,
    *, model_snapshot: ModelConfigReader | None = None,
    metadata_snapshot: dict[str, Any] | None = None,
) -> tuple[object, list[str]]:
    """
    异步安装插件并应用全部子能力状态

    参数:
    - installer: 插件安装器
    - spec: 插件运行规格
    - model_snapshot: 回滚时捕获的实际旧模型资源, 默认使用安装环境来源
    - metadata_snapshot: 回滚时捕获的旧插件元数据, 默认读取当前来源

    返回:
    - tuple[object, list[str]]: 插件句柄和能力变更
    """
    if not spec.path:
        raise ValueError(f"插件不存在: {spec.name}")
    config_type = EffectivePluginConfig if spec.config_resolved else PluginInstallConfig
    plugin = installer(spec.path, config_type(spec.config, initial_skills=spec.capabilities.get("skills"), model_snapshot=model_snapshot,
                                             initial_capabilities=spec.capabilities, metadata_snapshot=metadata_snapshot))
    if inspect.isawaitable(plugin):
        plugin = await plugin
    try:
        changes = await apply_plugin_capabilities_async(plugin, spec)
    except Exception as error:
        raise PluginInstallationError(spec.name, plugin, error) from error
    return plugin, changes


def _preview_plugin_changes(
    states: Iterable[PluginRuntimeState],
    desired_specs: Iterable[PluginSpec],
) -> list[dict[str, Any]]:
    """
    预览目标插件规格对一个运行时的影响

    参数:
    - states: 当前插件运行状态
    - desired_specs: 目标插件规格

    返回:
    - list[dict[str, Any]]: 逐插件影响列表
    """
    current = {item.name: item for item in states if item.name}
    desired = {item.name: item for item in desired_specs}
    impacts: list[dict[str, Any]] = []
    for name in sorted(set(current) | set(desired)):
        state = current.get(name)
        applied = state.applied_spec if state is not None else None
        target = desired.get(name)
        action = "none"
        if applied is None and target is not None:
            action = "add" if target.enabled else "track_disabled"
        elif applied is not None and target is None:
            action = "remove"
        elif applied is not None and target is not None:
            if applied.enabled and not target.enabled:
                action = "disable"
            elif not applied.enabled and target.enabled:
                action = "enable"
            elif (
                applied.config != target.config
                or applied.resources_revision != target.resources_revision
                or applied.path != target.path
                or applied.version != target.version
                or applied.interop != target.interop
            ):
                action = "reinstall" if target.enabled else "update_disabled"
            elif applied.capabilities != target.capabilities:
                action = "capabilities"
            elif state is not None and (state.drift or state.status == "error"):
                action = "retry"
        if target is not None and target.enabled and target.availability is not None and not target.availability.allowed:
            impacts.append({"plugin": name, "action": "blocked", "active": bool(applied and applied.enabled),
                            "reason_code": target.availability.reason_code, "error": target.availability.message})
            continue
        if action != "none":
            impacts.append({
                "plugin": name,
                "action": action,
                "active": bool(applied is not None and applied.enabled),
            })
    return impacts


async def _uninstall_plugin_async(
    uninstaller: AsyncPluginUninstaller | None,
    name: str,
) -> None:
    """
    卸载插件并统一校验结果

    参数:
    - uninstaller: 异步或同步卸载器
    - name: 插件名称
    """
    if uninstaller is None:
        raise RuntimeError("当前会话类型不支持插件热卸载")
    result = uninstaller(name)
    if inspect.isawaitable(result):
        result = await result
    if result is False:
        raise RuntimeError(f"插件卸载失败或不存在: {name}")


async def _rollback_install_async(
    installer: AsyncPluginInstaller,
    previous: PluginSpec,
    model_snapshot: ModelConfigReader | None,
) -> tuple[object, list[str]]:
    """
    重新安装旧规格完成生命周期回滚

    参数:
    - installer: 插件安装器
    - previous: 旧插件规格
    - model_snapshot: 旧句柄实际资源, 不重新查最新模型文件

    返回:
    - tuple[object, list[str]]: 恢复后的句柄和能力变更
    """
    with progress_phase("rollback_ms"):
        return await install_plugin_spec_async(installer, previous, model_snapshot=model_snapshot)


async def _reconcile_plugin_states_async(
    states: list[PluginRuntimeState],
    desired_specs: Iterable[PluginSpec],
    installer: AsyncPluginInstaller,
    uninstaller: AsyncPluginUninstaller | None,
    *, operation_order: list[str] | None = None,
) -> dict[str, Any]:
    """
    协调一个会话的完整插件生命周期并在失败时回滚

    参数:
    - states: 可原地更新的插件运行状态列表
    - desired_specs: 目标插件规格
    - installer: 插件安装器
    - uninstaller: 插件卸载器
    - operation_order: 完整目标配置提供的依赖顺序, 默认沿用旧名称排序

    返回:
    - dict[str, Any]: 逐插件结果与漂移汇总
    """
    timings = PhaseTimings()
    original_installer, original_uninstaller = installer, uninstaller

    async def timed_install(path: str, config: dict[str, Any] | None) -> object:
        """
        记录插件安装及回滚安装耗时

        参数:
        - path: 插件入口路径
        - config: 本次有效配置

        返回:
        - 原安装器的插件句柄
        """
        with timings.phase('install_ms'):
            result = original_installer(path, config)
            return await result if inspect.isawaitable(result) else result

    async def timed_uninstall(name: str) -> bool:
        """
        记录卸载及失败清理耗时

        参数:
        - name: 插件名称

        返回:
        - 原卸载器的结果
        """
        if original_uninstaller is None:
            raise RuntimeError('不支持插件热卸载')
        with timings.phase('uninstall_ms'):
            result = original_uninstaller(name)
            return await result if inspect.isawaitable(result) else result

    installer = timed_install
    uninstaller = timed_uninstall if original_uninstaller is not None else None
    desired_by_name = {item.name: item for item in desired_specs}
    state_by_name = {item.name: item for item in states if item.name}
    results: list[dict[str, Any]] = []

    for name in operation_order if operation_order is not None else sorted(set(state_by_name) | set(desired_by_name)):
        state = state_by_name.get(name)
        desired = desired_by_name.get(name)
        if state is None:
            state = PluginRuntimeState(desired_spec=desired)
            states.append(state)
            state_by_name[name] = state
        else:
            state.desired_spec = desired
        previous = state.applied_spec
        impact = _preview_plugin_changes([state], [desired] if desired is not None else [])
        action = str(impact[0]["action"]) if impact else "none"
        if action == "none":
            state.error = None
            state.last_operation_status = "unchanged"
            continue

        result: dict[str, Any] = {"plugin": name, "action": action, "status": "applied"}
        operation = action
        if action == "retry":
            if previous is None:
                operation = "add"
            elif previous.enabled:
                operation = "reinstall"
            else:
                operation = "update_disabled"
        try:
            if desired is not None and desired.enabled and desired.availability is not None:
                desired.availability.require()
            if operation in {"track_disabled", "update_disabled"}:
                state.applied_spec = desired
                state.handle = None
                state.mark("applied", status="disabled")
            elif operation == "add" or (
                operation == "enable" and previous is not None and not previous.enabled
            ):
                if desired is None:
                    raise RuntimeError(f"插件目标规格不存在: {name}")
                try:
                    handle, changes = await install_plugin_spec_async(installer, desired)
                except PluginInstallationError:
                    await _uninstall_plugin_async(uninstaller, name)
                    raise
                state.applied_spec = desired
                state.handle = handle
                state.mark("applied", status="loaded")
                result["changes"] = ["install", *changes]
            elif operation in {"disable", "remove"}:
                await _uninstall_plugin_async(uninstaller, name)
                state.handle = None
                state.applied_spec = desired
                state.mark("applied", status="disabled" if desired is not None else "removed")
                result["changes"] = ["uninstall"]
            elif operation == "reinstall":
                if previous is None or desired is None:
                    raise RuntimeError(f"插件重装缺少规格: {name}")
                resources = getattr(state.handle, "resources", None)
                previous_models = resources.snapshot_models() if isinstance(resources, PluginResources) else None
                await _uninstall_plugin_async(uninstaller, name)
                state.handle = None
                try:
                    handle, changes = await install_plugin_spec_async(installer, desired)
                except Exception as install_error:
                    logger.error(f"[插件协调] 重装失败, 插件={name}\n{traceback.format_exc()}")
                    if isinstance(install_error, PluginInstallationError):
                        try:
                            await _uninstall_plugin_async(uninstaller, name)
                        except Exception as cleanup_error:
                            logger.error(f"[插件协调] 失败插件清理失败, 插件={name}\n{traceback.format_exc()}")
                            message = f"{install_error}; 清理失败插件失败: {cleanup_error}"
                            state.applied_spec = None
                            state.mark("rollback_error", status="error", error=message)
                            result.update(status="rollback_error", error=message)
                            results.append(result)
                            continue
                    try:
                        rollback_handle, _ = await _rollback_install_async(installer, previous, previous_models)
                        state.handle = rollback_handle
                        state.applied_spec = previous
                        state.mark("rolled_back", status="loaded", error=str(install_error))
                        result.update(status="rolled_back", error=str(install_error))
                    except Exception as rollback_error:
                        logger.error(f"[插件协调] 回滚失败, 插件={name}\n{traceback.format_exc()}")
                        if isinstance(rollback_error, PluginInstallationError):
                            try:
                                await _uninstall_plugin_async(uninstaller, name)
                            except Exception as cleanup_error:
                                rollback_error = RuntimeError(
                                    f"{rollback_error}; 清理回滚插件失败: {cleanup_error}"
                                )
                        message = f"{install_error}; 回滚失败: {rollback_error}"
                        state.applied_spec = None
                        state.mark("rollback_error", status="error", error=message)
                        result.update(status="rollback_error", error=message)
                    results.append(result)
                    continue
                state.handle = handle
                state.applied_spec = desired
                state.mark("applied", status="loaded")
                result["changes"] = ["reinstall", *changes]
            elif operation == "capabilities":
                if previous is None or desired is None or state.handle is None:
                    raise RuntimeError(f"插件未处于可更新状态: {name}")
                try:
                    changes = await apply_plugin_capabilities_async(state.handle, desired, previous)
                except Exception as apply_error:
                    logger.error(f"[插件协调] 能力更新失败, 插件={name}\n{traceback.format_exc()}")
                    try:
                        await apply_plugin_capabilities_async(state.handle, previous, desired)
                        state.applied_spec = previous
                        state.mark("rolled_back", status="loaded", error=str(apply_error))
                        result.update(status="rolled_back", error=str(apply_error))
                    except Exception as rollback_error:
                        logger.error(f"[插件协调] 能力回滚失败, 插件={name}\n{traceback.format_exc()}")
                        message = f"{apply_error}; 回滚失败: {rollback_error}"
                        state.mark("rollback_error", status="error", error=message)
                        result.update(status="rollback_error", error=message)
                    results.append(result)
                    continue
                state.applied_spec = desired
                state.mark("applied", status="loaded")
                result["changes"] = changes
            else:
                raise RuntimeError(f"未知插件协调动作: {operation}")
        except RuntimeError as error:
            if "不支持插件热卸载" in str(error):
                logger.warning(f"[插件协调] 插件={name}, 动作={operation}, 原因={error}")
                state.mark("restart_required", error=str(error))
                result.update(status="restart_required", error=str(error))
            elif operation in {"disable", "remove", "reinstall"} and previous is not None:
                logger.error(f"[插件协调] 插件={name}, 动作={operation}\n{traceback.format_exc()}")
                state.mark(
                    "rolled_back",
                    status="loaded" if previous.enabled else "disabled",
                    error=str(error),
                )
                result.update(status="rolled_back", error=str(error))
            else:
                logger.error(f"[插件协调] 插件={name}, 动作={operation}\n{traceback.format_exc()}")
                state.mark("error", status="error", error=str(error))
                result.update(status="error", error=str(error))
        except Exception as error:
            logger.error(f"[插件协调] 插件={name}, 动作={operation}\n{traceback.format_exc()}")
            state.mark("error", status="error", error=str(error))
            result.update(status="error", error=str(error))
        results.append(result)

    states[:] = [
        state
        for state in states
        if state.applied_spec is not None or state.desired_spec is not None
    ]
    failed_statuses = {"error", "rolled_back", "rollback_error", "restart_required"}
    failed = sum(item["status"] in failed_statuses for item in results)
    drift = sum(item.drift for item in states)
    timing_payload = timings.to_payload()
    logger.debug(f"[插件协调] 插件={len(desired_by_name)}, 变更={len(results)}, 失败={failed}, timings_ms={timing_payload}")
    return {
        "ok": failed == 0 and drift == 0,
        "plugins": results,
        "failed": failed,
        "drift": drift,
        "timings_ms": timing_payload,
        "restart_required": any(
            item["status"] == "restart_required" for item in results
        ),
    }


def _spec_interop(spec: PluginSpec) -> InteropSpec:
    """
    读取目标规格中的同代声明, 旧手写规格才回退到元数据

    参数:
    - spec: 插件目标规格

    返回:
    - 已严格解析的交互声明
    """
    if spec.interop is not None:
        return spec.interop
    return parse_interop(load_plugin_meta(Path(spec.path))) if spec.path else InteropSpec()


def _target_relation_nodes(
    runtime: PluginInterop | None, states: list[PluginRuntimeState], specs: list[PluginSpec],
) -> dict[str, _RelationNode]:
    """
    用完整最终开关构建预览, 未加载插件的实际名称在安装阶段再次校验

    参数:
    - runtime: 当前真实会话注册表
    - states: 协调器当前实际规格
    - specs: 本次完整目标规格

    返回:
    - 不包含用户权限或可变插件实例的关系节点
    """
    current = runtime._relation_nodes() if runtime is not None else {}
    managed = {state.name for state in states} | {spec.name for spec in specs}
    result = {name: node for name, node in current.items() if name not in managed}
    previous = {state.name: state.applied_spec for state in states}
    unresolved: set[str] = set()
    for spec in specs:
        if not spec.enabled:
            continue
        if spec.availability is not None:
            spec.availability.require()
        interop = _spec_interop(spec)
        old = previous.get(spec.name)
        reuse = (old is not None and spec.name in current and old.config == spec.config and old.path == spec.path
                 and old.version == spec.version and old.interop == spec.interop and old.resources_revision == spec.resources_revision)
        capabilities: dict[CapabilityKind, dict[str, bool]]
        if reuse:
            capabilities = {kind: dict(values) for kind, values in current[spec.name].capabilities.items()}
            for kind, name, enabled in capability_changes(old, spec):
                capabilities.setdefault(cast(CapabilityKind, kind), {})[name] = enabled
        else:
            capabilities = {kind: dict(spec.capabilities.get(kind, {})) for kind in CAPABILITY_KINDS}
            for kind, names in interop.exports.items():
                for name in names:
                    capabilities[kind].setdefault(name, True)
            for groups in interop.replaces.values():
                for kind, pairs in groups.items():
                    for name in pairs.values():
                        capabilities[kind].setdefault(name, True)
            unresolved.add(spec.name)
        result[spec.name] = _RelationNode(True, interop, capabilities)
    for node in tuple(result.values()):
        targets: list[tuple[str, CapabilityKind, str]] = []
        for provider, groups in node.spec.replaces.items():
            targets.extend((provider, kind, name) for kind, pairs in groups.items() for name in pairs)
        for provider, groups in node.spec.conflicts.items():
            if groups is not None:
                targets.extend((provider, kind, name) for kind, names in groups.items() for name in names)
        for provider, kind, name in targets:
            if provider in unresolved:
                target = result[provider]
                values = {category: dict(entries) for category, entries in target.capabilities.items()}
                values.setdefault(kind, {}).setdefault(name, True)
                result[provider] = replace(target, capabilities=values)
    return result


def _dependency_order(names: set[str], nodes: dict[str, _RelationNode]) -> list[str]:
    """
    先处理移除项, 再按必需依赖顺序激活提供者和消费者

    参数:
    - names: 本次协调涉及的全部插件
    - nodes: 已通过合并关系预检的完整目标

    返回:
    - 每个待协调插件恰好出现一次的执行顺序
    """
    result = sorted(names - set(nodes))
    visited = set(result)
    visiting: set[str] = set()

    def visit(name: str) -> None:
        """
        在当前插件之前加入其提供者

        参数:
        - name: 当前待协调插件
        """
        if name in visited or name not in names:
            return
        if name in visiting:
            raise ValueError(f"必需插件依赖形成循环: {name}")
        visiting.add(name)
        for provider, dependency in nodes[name].spec.requires.items():
            if not dependency.optional and provider in nodes:
                visit(provider)
        visiting.remove(name)
        visited.add(name)
        result.append(name)

    for name in sorted(names & set(nodes)):
        visit(name)
    return result


def _batch_rejection(states: list[PluginRuntimeState], specs: list[PluginSpec], error: Exception) -> dict[str, Any]:
    """
    记录完整目标被拒绝, 保留实际句柄与旧应用规格

    参数:
    - states: 当前运行状态, 保留已有实际对象
    - specs: 被拒绝的完整目标
    - error: 预检或安装失败原因

    返回:
    - 与原协调器一致的失败包装, 明确记录漂移
    """
    desired = {spec.name: spec for spec in specs}
    current = {state.name: state for state in states}
    reports: list[dict[str, Any]] = []
    for name in sorted(set(current) | set(desired)):
        state = current.get(name)
        if state is None:
            state = PluginRuntimeState(desired_spec=desired[name])
            states.append(state)
        state.desired_spec = desired.get(name)
        state.mark("error", status="loaded" if state.handle is not None else "blocked", error=str(error))
        reports.append({"plugin": name, "action": "blocked", "status": "error", "error": str(error),
                        "reason_code": getattr(error, "code", "invalid_configuration")})
    logger.warning(f"[插件协调] 完整目标配置被拒绝: {error}")
    return {"ok": False, "plugins": reports, "failed": max(1, len(reports)),
            "drift": sum(state.drift for state in states), "restart_required": False, "timings_ms": {}}


def preview_plugin_reconciliation(
    states: Iterable[PluginRuntimeState], desired_specs: Iterable[PluginSpec],
    *, session: object | None = None,
) -> list[dict[str, Any]]:
    """
    在不执行插件代码的条件下预览完整关系及依赖者影响

    参数:
    - states: 当前实际插件状态
    - desired_specs: 完整目标规格
    - session: 可选实际宿主, 默认从已加载句柄定位

    返回:
    - 开关差量和依赖影响, 不兼容目标返回明确阻断原因
    """
    current_states, specs = list(states), list(desired_specs)
    changes = _preview_plugin_changes(current_states, specs)
    candidate = session
    if candidate is None:
        candidate = next((state.handle._session for state in current_states if isinstance(state.handle, Plugin)), None)
    runtime: object = getattr(candidate, "_plugin_interop", None)
    declared_relations = any(spec.interop and (spec.interop.requires or spec.interop.replaces or spec.interop.conflicts) for spec in specs)
    loaded_relations = isinstance(runtime, PluginInterop) and any(
        binding.spec.requires or binding.spec.replaces or binding.spec.conflicts for binding in runtime.bindings.values()
    )
    if not declared_relations and not loaded_relations:
        return changes
    if not isinstance(runtime, PluginInterop):
        runtime = None
    try:
        nodes = _target_relation_nodes(runtime, current_states, specs)
        before = runtime._relation_nodes() if runtime is not None else _target_relation_nodes(
            None, [], [state.applied_spec for state in current_states if state.applied_spec is not None],
        )
        activating = {spec.name for spec in specs if spec.enabled and (spec.name not in before or not before[spec.name].enabled)}
        view = _validate_relation_target(nodes, before, activating)
        previous_view = _validate_relation_target(before, before)
        for owner, node in nodes.items():
            if owner not in before or not node.enabled:
                continue
            was_available = previous_view.value((owner, None, ""))
            available = view.value((owner, None, ""))
            if was_available != available:
                changes.append({"plugin": owner, "action": "dependency_restored" if available else "dependency_unavailable",
                                "active": was_available, "reason_code": "" if available else "dependency_unavailable"})
        return changes
    except (ValueError, RuntimeError) as error:
        logger.warning(f"[插件预览] 完整目标关系不兼容: {error}")
        reports = changes or [{"plugin": spec.name, "active": False} for spec in specs if spec.enabled]
        return [{**item, "action": "blocked", "error": str(error),
                 "reason_code": getattr(error, "code", "invalid_configuration")} for item in reports]


async def reconcile_plugin_states_async(
    states: list[PluginRuntimeState], desired_specs: Iterable[PluginSpec],
    installer: AsyncPluginInstaller, uninstaller: AsyncPluginUninstaller | None,
    *, session: object | None = None,
) -> dict[str, Any]:
    """
    在原协调状态机外统一预检完整交互关系, 失败时恢复已变更插件

    参数:
    - states: 可原地更新的插件运行状态
    - desired_specs: 完整目标规格
    - installer: 原同步或异步插件安装器
    - uninstaller: 原卸载器, None 保留原重启提示规则
    - session: 实际宿主会话, 默认从绑定安装器或已加载句柄定位

    返回:
    - 原协调结果; 关系拒绝时不提交部分配置, 回滚失败明确报告
    """
    specs = list(desired_specs)
    candidate = session if session is not None else getattr(installer, "__self__", None)
    if candidate is None:
        candidate = next((state.handle._session for state in states if isinstance(state.handle, Plugin)), None)
    runtime: object = getattr(candidate, "_plugin_interop", None)
    if not isinstance(runtime, PluginInterop):
        if any(spec.interop is not None and (spec.interop.requires or spec.interop.replaces or spec.interop.conflicts) for spec in specs):
            return _batch_rejection(states, specs, ValueError("当前会话未提供插件交互宿主"))
        return await _reconcile_plugin_states_async(states, specs, installer, uninstaller)
    before_nodes: dict[str, _RelationNode] = {}
    before_handles: dict[str, Plugin] = {}
    original_specs: list[PluginSpec] = []
    original_metadata: dict[str, dict[str, Any]] = {}
    original_models: dict[str, ModelConfigReader | None] = {}
    captured = False
    cancelled: asyncio.CancelledError | None = None
    try:
        nodes = _target_relation_nodes(runtime, states, specs)
        has_relations = any(node.spec.requires or node.spec.replaces or node.spec.conflicts for node in nodes.values())
        has_relations = has_relations or any(binding.spec.requires or binding.spec.replaces or binding.spec.conflicts for binding in runtime.bindings.values())
        if not has_relations:
            return await _reconcile_plugin_states_async(states, specs, installer, uninstaller)
        before_nodes = runtime._relation_nodes()
        before_handles = {name: binding.plugin for name, binding in runtime.bindings.items() if binding.plugin is not None}
        original_specs = [state.applied_spec for state in states if state.applied_spec is not None]
        original_metadata = {name: dict(plugin._metadata) for name, plugin in before_handles.items()}
        original_models = {name: plugin.resources.snapshot_models() if isinstance(plugin.resources, PluginResources) else None
                           for name, plugin in before_handles.items()}
        captured = True
        activating = {spec.name for spec in specs if spec.enabled and (spec.name not in before_nodes or not before_nodes[spec.name].enabled)}
        order = _dependency_order({state.name for state in states} | {spec.name for spec in specs}, nodes)
        with runtime.configuration_update(nodes, activating=activating):
            result = await _reconcile_plugin_states_async(states, specs, installer, uninstaller, operation_order=order)
        if result.get("ok", False):
            if runtime.async_:
                await runtime.synchronize_skills_async()
            else:
                runtime.synchronize_skills()
            return result
        error = RuntimeError("完整插件目标未能应用, 已尝试恢复原运行配置")
    except (Exception, asyncio.CancelledError) as failure:
        if isinstance(failure, asyncio.CancelledError):
            if not captured:
                raise
            cancelled = failure
            failure = RuntimeError("完整插件配置应用已取消, 正在恢复原运行配置")
        if not captured:
            return _batch_rejection(states, specs, failure)
        error = failure

    current_nodes = runtime._relation_nodes()
    current_handles = {name: binding.plugin for name, binding in runtime.bindings.items() if binding.plugin is not None}
    changed = {name for name in set(before_handles) | set(current_handles)
               if before_handles.get(name) is not current_handles.get(name) or before_nodes.get(name) != current_nodes.get(name)}
    if not changed:
        if cancelled is not None:
            raise cancelled
        return _batch_rejection(states, specs, error)
    try:
        with runtime.configuration_update(before_nodes, activating=set(), baseline=before_nodes):
            for name in reversed(sorted(changed)):
                if name in current_handles:
                    await _uninstall_plugin_async(uninstaller, name)
            for state in states:
                if state.name in changed:
                    state.applied_spec, state.handle = None, None

            async def restore(path: str, config: dict[str, Any] | None) -> object:
                """
                用原模型和元数据恢复安装, 不重新解释当前更新后的声明

                参数:
                - path: 原插件路径
                - config: 原有效配置及能力开关

                返回:
                - 恢复后的原插件形态句柄
                """
                name = next((spec.name for spec in original_specs if spec.path == path), "")
                if isinstance(config, PluginInstallConfig):
                    config.metadata_snapshot = original_metadata.get(name)
                    config.model_snapshot = original_models.get(name)
                restored = installer(path, config)
                return await restored if inspect.isawaitable(restored) else restored

            rollback_order = _dependency_order({state.name for state in states} | {spec.name for spec in original_specs}, before_nodes)
            rollback = await _reconcile_plugin_states_async(states, original_specs, restore, uninstaller, operation_order=rollback_order)
        if runtime.async_:
            await runtime.synchronize_skills_async()
        else:
            runtime.synchronize_skills()
        rejected = _batch_rejection(states, specs, error)
        rejected["rolled_back"] = bool(rollback.get("ok", False))
        for state in states:
            if state.handle is not None:
                state.mark("rolled_back", status="loaded", error=str(error))
        if not rollback.get("ok", False):
            rejected["rollback_error"] = "部分插件未能恢复, 请检查运行状态"
            rejected["restart_required"] = True
        if cancelled is not None:
            raise cancelled
        return rejected
    except Exception as rollback_error:
        logger.error(f"[插件协调] 完整目标回滚失败\n{traceback.format_exc()}")
        rejected = _batch_rejection(states, specs, error)
        rejected.update(rolled_back=False, rollback_error=str(rollback_error), restart_required=True)
        if cancelled is not None:
            raise cancelled from rollback_error
        return rejected
