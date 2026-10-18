"""
编程工具的工作区路径解析与访问保护

宿主环境快照优先于工具实例配置, 在读写前检查保护目录和沙箱边界
"""

from __future__ import annotations
from pathlib import Path
from typing import Callable, cast
import time

from satrap.core.plugin_interop.environment import EnvironmentSnapshot, environment_snapshot
from satrap.core.utils.TCBuilder.tool_base import _ToolBase
from satrap.core.plugin_authorization import PluginEntryBinding, require_plugin_entry_permission
from satrap.core.file_access.reads import source_check as _source_check
from satrap.core.file_access.paths import protection_reason
from satrap.core.approval.policy import PermissionEngine
from satrap.core.call_context import current_call_origin, current_tool_workflow
from satrap.edictum import AsyncSimpleSession, SimpleSession, PluginHost, PluginInteractionError
from .approval import _turn_identity

from satrap.core.log import logger


def _workspace_root(session: SimpleSession | AsyncSimpleSession | None, configured_root: Path | None = None) -> Path:
    """
    解析工作区, 宿主环境优先于工具配置和固定默认值

    参数:
    - session: 当前会话, 宿主在项目改绑时更新统一环境
    - configured_root: 工具实例保存的插件配置

    返回:
    - Path: 当前调用使用的绝对工作区根
    """
    from . import WORKSPACE_ROOT

    return Path(
        environment_snapshot(session).workspace_root or configured_root or WORKSPACE_ROOT
    ).resolve()


def _tool_root(tool: _ToolBase) -> Path:
    """
    取当前宿主工作区, 未注入时使用工具实例配置

    参数:
    - tool: 工具

    返回:
    - Path: 当前调用使用的绝对工作区根, 无会话或实例路径时使用固定默认值
    """
    from . import WORKSPACE_ROOT

    snapshot = _tool_environment(tool)
    root = Path(snapshot.workspace_root or getattr(tool, "workspace_root", None) or WORKSPACE_ROOT).resolve()
    sandbox = snapshot.sandbox_root
    if sandbox and root == Path(sandbox).resolve():
        root.mkdir(parents=True, exist_ok=True)
    return root


def _tool_environment(tool: _ToolBase) -> EnvironmentSnapshot:
    """
    从插件宿主取得本次可用的冻结环境

    参数:
    - tool: 当前工具实例, 独立构造时允许没有插件宿主

    返回:
    - 当前环境快照, 过期插件宿主直接拒绝访问
    """
    plugin_host = getattr(tool, "_plugin_host", None)
    return (plugin_host.environment_snapshot() if isinstance(plugin_host, PluginHost)
            else environment_snapshot(getattr(tool, "_session", None)))


def _resolve_path(path: str, root: Path | None = None, *, full_access: bool = False) -> Path:
    """
    解析相对或绝对路径, 普通模式校验工作区边界, full-access 允许显式越界

    参数:
    - path: 路径
    - root: 根目录
    - full_access: True 允许显式路径越出工作区, 默认 False

    返回:
    - 已解析的绝对路径, 普通模式越界抛出 ValueError
    """
    from . import WORKSPACE_ROOT

    root = root or WORKSPACE_ROOT
    raw = Path(path)
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.resolve()
    if not full_access and resolved != root and not resolved.is_relative_to(root):
        raise ValueError(f"路径越出工作区: {resolved}")
    return resolved


def _resolve_grep_file(path: Path, root: Path, *, full_access: bool = False) -> Path | None:
    """
    解析 grep 候选文件并拒绝工作区外的符号链接目标

    参数:
    - path: 遍历得到的候选路径
    - root: 已解析的工作区根目录
    - full_access: True 允许工作区外候选文件

    返回:
    - Path | None: 安全的真实文件路径, 越界或不可访问时返回 None
    """
    try:
        resolved = path.resolve()
        if (not full_access and not resolved.is_relative_to(root)) or not resolved.is_file():
            return None
        return resolved
    except OSError:
        return None


class _FileProtectionMixin:
    """文件工具的实例级额外保护目录, 直接构造时只启用内置保护"""

    protected_dirs: frozenset[str] = frozenset()
    sandbox_root: Path | None = None

    def _read_in_environment(self, read: Callable[[], str], environment: EnvironmentSnapshot | None = None) -> str:
        """
        在冻结的宿主环境内读取, 改绑后不返回旧请求的文件内容

        参数:
        - read: 已固定业务参数的同步读取函数
        - environment: 调度前快照, 默认在当前同步入口冻结

        返回:
        - 读取结果; 执行前后环境不一致时抛出 capability_stale
        """
        tool = cast(_ToolBase, self)
        environment = environment or _tool_environment(tool)
        if _tool_environment(tool) != environment:
            raise PluginInteractionError("capability_stale", "文件读取环境已经改变")
        result = read()
        if _tool_environment(tool) != environment:
            raise PluginInteractionError("capability_stale", "文件读取期间宿主环境已经改变")
        return result

    def _protection_reason(self, path: Path, root: Path) -> str | None:
        """
        按当前工具配置检查保护目录

        参数:
        - path: 待访问路径
        - root: 当前工作区根目录

        返回:
        - 命中内置或实例保护规则时返回原因, 否则返回 None
        """
        session = getattr(self, "_session", None)
        sandbox_root = _session_sandbox_root(session, self.sandbox_root)
        return _protection_reason(path, root, self.protected_dirs, sandbox_root=sandbox_root)


def _protection_reason(
    path: Path, root: Path | None = None, protected_dirs: frozenset[str] = frozenset(),
    *, sandbox_root: Path | None = None,
) -> str | None:
    """
    命中保护路径返回原因 (敏感目录/文件), 沙箱边界由 _in_sandbox 单独判断

    参数:
    - path: 路径
    - root: 根目录
    - protected_dirs: 当前工具的额外保护目录名, 默认为空且不替换内置保护
    - sandbox_root: 实际生效沙箱, 仅放行工作区内且位于该沙箱中的普通文件

    返回:
    - str | None: 原因 (敏感目录/文件)
    """
    from . import WORKSPACE_ROOT, _SYSTEM_PROTECTED_ROOT

    return protection_reason(path, root or WORKSPACE_ROOT, protected_dirs=protected_dirs,
                             sandbox_root=sandbox_root, system_root=_SYSTEM_PROTECTED_ROOT)


def _session_sandbox_root(
    session: SimpleSession | AsyncSimpleSession | None, configured_root: Path | None = None
) -> Path:
    """
    获取会话沙箱根 (宿主环境优先, 否则使用工具配置和默认值)

    参数:
    - session: 当前会话, 未绑定时为 None
    - configured_root: 工具实例的沙箱配置

    返回:
    - Path: 宿主环境, 工具配置, 固定默认值按顺序选取的沙箱根
    """
    from . import DEFAULT_SANDBOX_ROOT

    return Path(
        environment_snapshot(session).sandbox_root or configured_root or DEFAULT_SANDBOX_ROOT
    ).resolve()


def _in_sandbox(
    path: Path,
    session: SimpleSession | AsyncSimpleSession,
    configured_root: Path | None = None,
) -> bool:
    """
    目标路径是否位于会话沙箱根内 (沙箱 = 免审批区)

    参数:
    - path: 路径
    - session: 会话
    - configured_root: 工具实例的沙箱配置

    返回:
    - bool: 目标路径是否位于会话沙箱根内 (沙箱 = 免审批区)
    """
    root = _session_sandbox_root(session, configured_root)
    return path == root or path.is_relative_to(root)


def _full_access(tool: _ToolBase) -> bool:
    """
    读取工具所属会话当前模式, 直接构造的只读工具默认受工作区限制

    参数:
    - tool: 当前文件或 Shell 工具

    返回:
    - 共享引擎明确为 full-access 时返回 True
    """
    engine = getattr(tool, "engine", None)
    return isinstance(engine, PermissionEngine) and engine.mode == "full-access"


def _execution_guard(tool: _ToolBase, engine: PermissionEngine, path: Path) -> Callable[[], None]:
    """
    冻结执行边界, 为等待审批和线程调度后的副作用提供复核

    参数:
    - tool: 当前真实工具实例
    - engine: 会话共享权限状态
    - path: 已解析的目标文件或工作目录

    返回:
    - 可重复调用的检查函数, 边界改变或入口失效时抛出 PermissionError
    """
    session = cast(SimpleSession | AsyncSimpleSession, getattr(tool, "_session", None))
    environment = _tool_environment(tool)
    root = _tool_root(tool)
    sandbox = _session_sandbox_root(session, getattr(tool, "sandbox_root", None))
    deadline = time.monotonic() + 600
    tool_name = tool.get_tool_name()
    revision, mode = engine.revision, engine.mode
    review_model = engine.approval_model
    turn_identity = _turn_identity(session)
    session_id = getattr(session, "session_id", "")
    origin = current_call_origin()
    workflow = current_tool_workflow()
    source_check = _source_check(origin)
    binding = getattr(tool, "_plugin_entry_binding", None)
    protected = getattr(tool, "protected_dirs", frozenset())
    allowed_env = getattr(tool, "allowed_env", frozenset())

    def check() -> None:
        """副作用前核验原始权限, 路径和真实插件入口"""
        if engine.plan_mode:
            logger.warning("[satrap_coding] 计划模式已拒绝文件修改和 Shell")
            raise PermissionError("计划模式已拒绝文件修改和 Shell")
        if (time.monotonic() >= deadline or getattr(tool, "engine", None) is not engine
                or tool.get_tool_name() != tool_name or not tool.is_available_for_call()
                or engine.revision != revision or engine.mode != mode or engine.approval_model != review_model
                or getattr(tool, "_session", None) is not session or _turn_identity(session) != turn_identity
                or getattr(session, "session_id", "") != session_id
                or _tool_environment(tool) != environment
                or _tool_root(tool) != root or path.resolve() != path
                or _session_sandbox_root(session, getattr(tool, "sandbox_root", None)) != sandbox
                or current_call_origin() is not origin or current_tool_workflow() is not workflow or not tool.is_enabled()
                or getattr(tool, "protected_dirs", frozenset()) != protected
                or getattr(tool, "allowed_env", frozenset()) != allowed_env
                or getattr(tool, "_plugin_entry_binding", None) is not binding):
            logger.warning("[satrap_coding] 执行取消: 审批期间来源, 权限, 工具或路径发生变化")
            raise PermissionError("执行已取消: 审批期间来源, 权限, 工具或路径发生变化")
        source_check()
        if binding is not None:
            require_plugin_entry_permission(cast(PluginEntryBinding, binding), refresh=True)
        if tool.get_tool_name() != "shell":
            reason = _protection_reason(path, root, protected, sandbox_root=sandbox)
            if reason is not None:
                logger.warning(f"[satrap_coding] 执行取消: {reason}")
                raise PermissionError(f"拒绝访问: {reason}")
    return check


def _read_file_page(path: Path, start: int, page_size: int) -> str:
    """流式统计总行数, 仅保留目标页; 单行与总输出均限制 UTF-8 字节数"""
    from . import _FILE_LINE_BREAK

    line_number = 0
    line_bytes = 0
    output_bytes = 0
    has_tail = False
    truncated = False
    pieces: list[str] = []
    lines: list[str] = []

    def retain(piece: str) -> None:
        nonlocal line_bytes, output_bytes, truncated
        if not start <= line_number < start + page_size:
            return
        budget = min(16 * 1024 - line_bytes, 1024 * 1024 - output_bytes)
        raw = piece.encode("utf-8")
        if len(raw) > budget:
            raw = raw[:budget]
            truncated = True
        text = raw.decode("utf-8", errors="ignore")  # 截断时保留完整多字节字符
        if text:
            pieces.append(text)
        line_bytes += len(raw)
        output_bytes += len(raw)

    def finish_line() -> None:
        nonlocal line_number, line_bytes
        if start <= line_number < start + page_size:
            lines.append("".join(pieces))
        pieces.clear()
        line_bytes = 0
        line_number += 1

    try:
        with path.open("r", encoding="utf-8", errors="replace", newline=None) as stream:
            while chunk := stream.read(64 * 1024):
                position = 0
                for match in _FILE_LINE_BREAK.finditer(chunk):
                    retain(chunk[position : match.start()])
                    finish_line()
                    position = match.end()
                    has_tail = False
                if position < len(chunk):
                    retain(chunk[position:])
                    has_tail = True
            if has_tail:
                finish_line()
    except OSError as error:
        return f"错误: 读取失败: {error}"
    end = min(line_number, start + page_size)
    body = "\n".join(lines)
    notice = "\n[输出已截断: 单行上限 16 KiB, 总内容上限 1 MiB]" if truncated else ""
    return f"{path} 行 {start}-{end}/{line_number}:\n{body}{notice}"
