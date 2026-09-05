"""插件运行协调器: 统一安装插件并通过插件命名空间应用子能力"""
from __future__ import annotations

from dataclasses import dataclass, field
import inspect
from typing import Any, Awaitable, Callable, Iterable
import time

from satrap.edictum.plugin_spec import PluginSpec
from satrap.edictum.plugin import CAPABILITY_KINDS


_CAPABILITY_SINGULAR = {
    "tools": "tool",
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
    plugin = installer(spec.path, spec.config)
    try:
        changes = apply_plugin_capabilities(plugin, spec)
    except Exception as error:
        raise PluginInstallationError(spec.name, plugin, error) from error
    return plugin, changes


async def install_plugin_spec_async(
    installer: AsyncPluginInstaller,
    spec: PluginSpec,
) -> tuple[object, list[str]]:
    """
    异步安装插件并应用全部子能力状态

    参数:
    - installer: 插件安装器
    - spec: 插件运行规格

    返回:
    - tuple[object, list[str]]: 插件句柄和能力变更
    """
    if not spec.path:
        raise ValueError(f"插件不存在: {spec.name}")
    plugin = installer(spec.path, spec.config)
    if inspect.isawaitable(plugin):
        plugin = await plugin
    try:
        changes = await apply_plugin_capabilities_async(plugin, spec)
    except Exception as error:
        raise PluginInstallationError(spec.name, plugin, error) from error
    return plugin, changes


def preview_plugin_reconciliation(
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
                or applied.path != target.path
                or applied.version != target.version
            ):
                action = "reinstall" if target.enabled else "update_disabled"
            elif applied.capabilities != target.capabilities:
                action = "capabilities"
            elif state is not None and (state.drift or state.status == "error"):
                action = "retry"
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
) -> tuple[object, list[str]]:
    """
    重新安装旧规格完成生命周期回滚

    参数:
    - installer: 插件安装器
    - previous: 旧插件规格

    返回:
    - tuple[object, list[str]]: 恢复后的句柄和能力变更
    """
    return await install_plugin_spec_async(installer, previous)


async def reconcile_plugin_states_async(
    states: list[PluginRuntimeState],
    desired_specs: Iterable[PluginSpec],
    installer: AsyncPluginInstaller,
    uninstaller: AsyncPluginUninstaller | None,
) -> dict[str, Any]:
    """
    协调一个会话的完整插件生命周期并在失败时回滚

    参数:
    - states: 可原地更新的插件运行状态列表
    - desired_specs: 目标插件规格
    - installer: 插件安装器
    - uninstaller: 插件卸载器

    返回:
    - dict[str, Any]: 逐插件结果与漂移汇总
    """
    desired_by_name = {item.name: item for item in desired_specs}
    state_by_name = {item.name: item for item in states if item.name}
    results: list[dict[str, Any]] = []

    for name in sorted(set(state_by_name) | set(desired_by_name)):
        state = state_by_name.get(name)
        desired = desired_by_name.get(name)
        if state is None:
            state = PluginRuntimeState(desired_spec=desired)
            states.append(state)
            state_by_name[name] = state
        else:
            state.desired_spec = desired
        previous = state.applied_spec
        impact = preview_plugin_reconciliation([state], [desired] if desired is not None else [])
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
                await _uninstall_plugin_async(uninstaller, name)
                state.handle = None
                try:
                    handle, changes = await install_plugin_spec_async(installer, desired)
                except Exception as install_error:
                    if isinstance(install_error, PluginInstallationError):
                        try:
                            await _uninstall_plugin_async(uninstaller, name)
                        except Exception as cleanup_error:
                            message = f"{install_error}; 清理失败插件失败: {cleanup_error}"
                            state.applied_spec = None
                            state.mark("rollback_error", status="error", error=message)
                            result.update(status="rollback_error", error=message)
                            results.append(result)
                            continue
                    try:
                        rollback_handle, _ = await _rollback_install_async(installer, previous)
                        state.handle = rollback_handle
                        state.applied_spec = previous
                        state.mark("rolled_back", status="loaded", error=str(install_error))
                        result.update(status="rolled_back", error=str(install_error))
                    except Exception as rollback_error:
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
                    try:
                        await apply_plugin_capabilities_async(state.handle, previous, desired)
                        state.applied_spec = previous
                        state.mark("rolled_back", status="loaded", error=str(apply_error))
                        result.update(status="rolled_back", error=str(apply_error))
                    except Exception as rollback_error:
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
                state.mark("restart_required", error=str(error))
                result.update(status="restart_required", error=str(error))
            elif operation in {"disable", "remove", "reinstall"} and previous is not None:
                state.mark(
                    "rolled_back",
                    status="loaded" if previous.enabled else "disabled",
                    error=str(error),
                )
                result.update(status="rolled_back", error=str(error))
            else:
                state.mark("error", status="error", error=str(error))
                result.update(status="error", error=str(error))
        except Exception as error:
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
    return {
        "ok": failed == 0 and drift == 0,
        "plugins": results,
        "failed": failed,
        "drift": drift,
        "restart_required": any(
            item["status"] == "restart_required" for item in results
        ),
    }
