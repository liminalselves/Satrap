"""
宿主单文件读取的准备, 审批与执行复核

在读取内容前固定真实路径, 文件指纹和读取参数, 外部文件按三档策略审批;
等待和线程调度后复核来源与环境, 不缓存文件或父目录的后续访问许可
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import traceback
from pathlib import Path
import asyncio
from typing import TypeVar, cast
import json
import stat
import time

from satrap.core.plugin_interop.environment import EnvironmentSnapshot
from satrap.core.utils.TCBuilder.tool_base import _ToolBase
from satrap.core.plugin_interop.contracts import PluginInteractionError
from satrap.core.approval.coordinator import approve_sync, approve_async, _turn_identity
from satrap.core.plugin_authorization import PluginEntryBinding, require_plugin_entry_permission
from satrap.core.utils.file_snapshot import FileFingerprint, file_fingerprint
from satrap.core.file_access.paths import protection_reason
from satrap.core.approval.policy import PermissionEngine, RiskLevel
from satrap.core.approval.state import session_approval
from satrap.core.call_context import CallOrigin, current_call_origin, current_tool_workflow
from satrap.core.platform import current_adapter_manager
from satrap.edictum import SimpleSession, AsyncSimpleSession

from satrap.core.log import logger


_T = TypeVar("_T")


def read_error(error: Exception) -> str:
    """
    在文件工具边界记录失败并转换为错误文本

    参数:
    - error: 准备, 审批或读取阶段的异常

    返回:
    - 明确的错误说明; 宿主失效继续交给工具分派边界处理, 取消信号不被转换
    """
    if isinstance(error, PluginInteractionError):
        raise error
    if isinstance(error, (OSError, ValueError, RuntimeError)):
        logger.warning(f"[文件读取] 操作失败: {error}")
    else:
        logger.error(f"[文件读取] 非预期失败: {error}\n{traceback.format_exc()}")
    return f"错误: {error}"


def source_check(origin: CallOrigin | None) -> Callable[[], None]:
    """
    固定平台账号与路由代次, 在实际文件操作前复核

    参数:
    - origin: 真实调用来源, 本地操作可为 None

    返回:
    - 无副作用的复核函数, 来源失效时抛出 PermissionError
    """
    manager = current_adapter_manager()
    adapter = manager.get_adapter(origin.adapter_id) if manager and origin else None

    def check() -> None:
        """拒绝审批等待期间发生的账号或会话路由变化"""
        if origin is None:
            return
        if (manager is None or current_adapter_manager() is not manager or adapter is None
                or manager.get_adapter(origin.adapter_id) is not adapter
                or not adapter.config.enable or adapter.client_self_id != origin.self_id):
            raise PermissionError("来源平台或机器人账号已经失效")
        if origin.conversation_kind:
            store = adapter.agent_route_store
            if store is not None:
                revision = store.current_revision(origin.self_id, origin.conversation_kind, origin.chat_id)
            else:
                state = adapter._agent_route_memory.get((origin.self_id, origin.conversation_kind, origin.chat_id))
                revision = state[1] if state else 0
            if revision != origin.agent_route_generation:
                raise PermissionError("来源 Agent 路由已经变化")
            if origin.conversation_kind == "group":
                route = getattr(adapter, "group_route", None)
                if callable(route):
                    current = route(origin.chat_id)
                    if not isinstance(current, tuple) or len(current) != 2 or current[1] != origin.group_route_generation:
                        raise PermissionError("来源群会话路由已经变化")
    return check


@dataclass(frozen=True)
class FileRead:
    """本次读取的固定目标与复核入口, 不代表可重复使用的授权"""

    path: Path
    external: bool
    action_json: str
    tool_name: str
    session: SimpleSession | AsyncSimpleSession | None
    engine: PermissionEngine | None
    check: Callable[[], None]

    def _read(self, reader: Callable[[Path], _T]) -> _T:
        """
        在当前执行线程内复核后读取, 文件变化时丢弃结果

        参数:
        - reader: 已准备的解析函数, 只接收本次固定路径

        返回:
        - 通过前后复核的读取结果
        """
        self.check()
        result = reader(self.path)
        self.check()
        return result

    def read_sync(self, reader: Callable[[Path], _T]) -> _T:
        """
        同步审批并读取单个文件

        参数:
        - reader: 固定解析参数的读取函数

        返回:
        - 读取结果; 无审批通道, 被拒或来源变化时抛出权限错误
        """
        if self.external:
            if not isinstance(self.session, SimpleSession) or self.engine is None:
                raise PermissionError("approval_required: 路径越出当前会话可见范围, 缺少同步审批通道")
            allowed, reason = approve_sync(self.session, self.engine, "file_read_external", RiskLevel.READ,
                                           f"读取工作区外文件: {self.path}", action=json.loads(self.action_json), tool_name=self.tool_name)
            if not allowed:
                raise PermissionError(reason)
        return self._read(reader)

    async def read_async(self, reader: Callable[[Path], _T]) -> _T:
        """
        在事件循环完成审批, 再在线程内复核与读取

        参数:
        - reader: 固定解析参数的同步读取函数

        返回:
        - 读取结果; 取消直接传播, 不在工作线程等待异步人工审批
        """
        if self.external:
            if not isinstance(self.session, AsyncSimpleSession) or self.engine is None:
                raise PermissionError("approval_required: 路径越出当前会话可见范围, 缺少异步审批通道")
            allowed, reason = await approve_async(self.session, self.engine, "file_read_external", RiskLevel.READ,
                                                 f"读取工作区外文件: {self.path}", action=json.loads(self.action_json), tool_name=self.tool_name)
            if not allowed:
                raise PermissionError(reason)
        return await asyncio.to_thread(self._read, reader)


def prepare_file_read(
    tool: _ToolBase, path: str, *, workspace: Path, environment: EnvironmentSnapshot,
    current_environment: Callable[[], EnvironmentSnapshot], parameters: dict[str, object],
    sandbox_root: Path | None = None, system_root: Path | None = None,
) -> FileRead:
    """
    在内容读取前准备单文件目标, 共用保护与审批边界

    参数:
    - tool: 实际执行的工具实例
    - path: 显式文件路径, 相对路径基于工作区
    - workspace: 当前工作区根
    - environment: 调用入口冻结的宿主环境
    - current_environment: 执行时重新核验宿主环境的回调
    - parameters: 本次读取范围和解析参数, 序列化后固定
    - sandbox_root: 独立工具的沙箱兜底, 宿主绑定优先
    - system_root: 可选系统保护目录, 默认使用宿主规则

    返回:
    - 固定读取计划; 路径无效, 目标非普通文件或命中保护时抛出异常
    """
    # Step.1 固定真实文件, 只在相对文件名未找到时匹配当前会话上传目录
    if not isinstance(path, str) or not path.strip() or "\0" in path or path.startswith(("\\\\", "//")):
        raise ValueError("请提供本地单个普通文件路径")
    workspace = workspace.resolve()
    raw = Path(path)
    requested = raw if raw.is_absolute() else workspace / raw
    uploads = environment.upload_root
    if not raw.is_absolute() and raw.parent == Path(".") and not requested.is_file() and uploads is not None and uploads.is_dir():
        matches = sorted(candidate for candidate in uploads.iterdir()
                         if candidate.is_file() and (candidate.name.lower() == raw.name.lower()
                                                    or candidate.name.lower().endswith("_" + raw.name.lower())))
        if matches:
            requested = matches[0]
    target = requested.resolve()
    sandbox = environment.sandbox_root or sandbox_root
    protected = cast(frozenset[str], getattr(tool, "protected_dirs", frozenset()))
    reason = protection_reason(target, workspace, protected_dirs=protected, sandbox_root=sandbox,
                               upload_root=uploads, system_root=system_root, sandbox_workspace_only=False)
    if reason is not None:
        raise PermissionError(f"拒绝读取: {reason}")
    try:
        metadata = target.stat()
    except FileNotFoundError as error:
        raise FileNotFoundError(f"文件不存在: {target}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("只支持读取单个普通文件, 不授予目录枚举权限")
    fingerprint: FileFingerprint = file_fingerprint(target)
    roots = (workspace, uploads, sandbox)
    external = not any(root is not None and target.is_relative_to(root.resolve()) for root in roots)

    # Step.2 固定会话策略, 真实来源与读取参数, 不提前读取内容作为审批证据
    bound_session = getattr(tool, "_session", None)
    session = bound_session if isinstance(bound_session, (SimpleSession, AsyncSimpleSession)) else None
    engine = session_approval(session) if session is not None else None
    revision = engine.revision if engine is not None else 0
    origin, workflow = current_call_origin(), current_tool_workflow()
    turn = _turn_identity(session) if session is not None else ("", "")
    binding = getattr(tool, "_plugin_entry_binding", None)
    source = source_check(origin)
    deadline = time.monotonic() + 600
    configured_roots = tuple(getattr(tool, key, None) for key in ("workspace_root", "sandbox_root", "_base_root"))
    name = tool.get_tool_name()
    qualified = f"{binding.plugin_name}.{name}" if isinstance(binding, PluginEntryBinding) else name
    action = json.dumps({"path": str(target), "requested_path": str(requested), "fingerprint": list(fingerprint),
                         "parameters": parameters}, ensure_ascii=False, sort_keys=True)

    def check() -> None:
        """复核文件, 环境, 策略和原始工具入口, 任何漂移均不返回旧读取结果"""
        if current_environment() != environment:
            raise PluginInteractionError("capability_stale", "文件读取期间宿主环境已经变化")
        if (requested.resolve() != target or file_fingerprint(target) != fingerprint
                or not tool.is_enabled() or not tool.is_available_for_call() or tool.get_tool_name() != name
                or getattr(tool, "_plugin_entry_binding", None) is not binding
                or getattr(tool, "_session", None) is not bound_session
                or current_call_origin() is not origin or current_tool_workflow() is not workflow
                or time.monotonic() >= deadline or getattr(tool, "protected_dirs", frozenset()) != protected
                or tuple(getattr(tool, key, None) for key in ("workspace_root", "sandbox_root", "_base_root")) != configured_roots):
            raise PermissionError("读取取消: 文件, 环境或工具来源已经变化")
        if session is not None:
            current = session_approval(session)
            if current is not engine or current.revision != revision or _turn_identity(session) != turn:
                raise PermissionError("读取取消: 审批策略或来源轮次已经变化")
        if isinstance(binding, PluginEntryBinding):
            require_plugin_entry_permission(binding, refresh=True)
        if external:
            source()
    check()
    return FileRead(target, external, action, qualified, session, engine, check)
