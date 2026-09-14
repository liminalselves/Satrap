"""
编程工具的工作区路径解析与访问保护

优先使用会话工作区, 保留包级配置兜底, 在读写前检查保护目录和沙箱边界
"""

from __future__ import annotations
from pathlib import Path
from typing import Any

from satrap.expend.plugins.satrap_coding.core.permission import (
    PermissionEngine,
    RiskLevel,
)
from satrap.core.type import safe_getattr
from satrap.edictum import AsyncSimpleSession, SimpleSession
from .constants import (
    WORKSPACE_ROOT,
    _PROTECTED_DIRS,
    _PROTECTED_FILES,
    _SYSTEM_PROTECTED_ROOT,
    _FILE_LINE_BREAK,
    DEFAULT_SANDBOX_ROOT,
)
from .approval import _approve_async, _approve_sync


def _workspace_root(session: Any) -> Path:
    """
    按会话解析工作区根: 会话鸭子属性 coding_workspace_root 优先, 否则全局 WORKSPACE_ROOT

    参数:
    - session: 会话

    项目功能: 项目会话由 ChatService 在建会话/改绑时赋值 session.coding_workspace_root;
    无项目会话该属性不存在, 回落全局 -- 行为与引入项目前一致

    返回:
    - Path: 按会话解析工作区根: 会话鸭子属性 coding_workspace_root 优先, 否则全局 WORKSPACE_ROOT
    """
    from . import WORKSPACE_ROOT

    if session is None:
        return WORKSPACE_ROOT
    return Path(
        safe_getattr(session, "coding_workspace_root") or WORKSPACE_ROOT
    ).resolve()


def _tool_root(tool: Any) -> Path:
    """
    取工具所属会话的工作区根 (未绑会话时回落全局)

    参数:
    - tool: 工具

    返回:
    - Path: 取工具所属会话的工作区根 (未绑会话时回落全局)
    """
    session = getattr(tool, "_session", None)
    root = _workspace_root(session)
    sandbox = getattr(session, "coding_sandbox_root", None)
    if sandbox and root == Path(sandbox).resolve():
        root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_path(path: str, root: Path | None = None) -> Path:
    """
    解析路径并校验在工作区内 (相对路径以工作区为基准), 越界抛 ValueError

    参数:
    - path: 路径
    - root: 根目录

    返回:
    - Path: 解析路径并校验在工作区内 (相对路径以工作区为基准), 越界抛 ValueError
    """
    from . import WORKSPACE_ROOT

    root = root or WORKSPACE_ROOT
    raw = Path(path)
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise ValueError(f"路径越出工作区: {resolved}")
    return resolved


def _resolve_grep_file(path: Path, root: Path) -> Path | None:
    """
    解析 grep 候选文件并拒绝工作区外的符号链接目标

    参数:
    - path: 遍历得到的候选路径
    - root: 已解析的工作区根目录

    返回:
    - Path | None: 安全的真实文件路径, 越界或不可访问时返回 None
    """
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        return resolved
    except OSError:
        return None


def _protection_reason(path: Path, root: Path | None = None) -> str | None:
    """
    命中保护路径返回原因 (敏感目录/文件), 沙箱边界由 _in_sandbox 单独判断

    参数:
    - path: 路径
    - root: 根目录

    返回:
    - str | None: 原因 (敏感目录/文件)
    """
    from . import (
        WORKSPACE_ROOT,
        _PROTECTED_DIRS,
        _PROTECTED_FILES,
        _SYSTEM_PROTECTED_ROOT,
    )

    root = root or WORKSPACE_ROOT
    resolved = path.resolve()
    if resolved == _SYSTEM_PROTECTED_ROOT or resolved.is_relative_to(
        _SYSTEM_PROTECTED_ROOT
    ):
        return "路径位于受保护目录 .satrap/ 下"
    rel = resolved.relative_to(root) if resolved.is_relative_to(root) else resolved
    parts = [p.lower() for p in rel.parts]
    for name in _PROTECTED_DIRS:
        if name in parts:
            return f"路径位于受保护目录 {name}/ 下"
    for name in _PROTECTED_FILES:
        if path.name.lower() == name:
            return f"路径命中受保护文件 {name}"
    return None


def _session_sandbox_root(session: SimpleSession | AsyncSimpleSession) -> Path:
    """
    获取会话沙箱根 (会话属性优先, 否则默认)

    参数:
    - session: 会话

    返回:
    - Path: 会话沙箱根 (会话属性优先, 否则默认)
    """
    from . import DEFAULT_SANDBOX_ROOT

    return Path(
        safe_getattr(session, "coding_sandbox_root") or DEFAULT_SANDBOX_ROOT
    ).resolve()


def _in_sandbox(path: Path, session: SimpleSession | AsyncSimpleSession) -> bool:
    """
    目标路径是否位于会话沙箱根内 (沙箱 = 免审批区)

    参数:
    - path: 路径
    - session: 会话

    返回:
    - bool: 目标路径是否位于会话沙箱根内 (沙箱 = 免审批区)
    """
    root = _session_sandbox_root(session)
    return path == root or path.is_relative_to(root)


def _approve_file_write(
    session: SimpleSession,
    engine: PermissionEngine,
    path: Path,
    action: str,
) -> tuple[bool, str]:
    """
    文件写审批: 目标在沙箱内免审批 (沙箱=免审批区), 其余按策略审批

    参数:
    - session: 会话
    - engine: 执行引擎
    - path: 路径
    - action: 操作类型

    返回:
    - tuple[bool, str]: 文件写审批: 目标在沙箱内免审批 (沙箱=免审批区), 其余按策略审批
    """
    if _in_sandbox(path, session):
        return True, ""
    return _approve_sync(
        session, engine, "file_write", RiskLevel.WRITE, f"{action} {path}"
    )


async def _approve_file_write_async(
    session: AsyncSimpleSession,
    engine: PermissionEngine,
    path: Path,
    action: str,
) -> tuple[bool, str]:
    """
    异步文件写审批: 沙箱内免审批, 其余按策略审批

    参数:
    - session: 会话
    - engine: 执行引擎
    - path: 路径
    - action: 操作类型

    返回:
    - tuple[bool, str]: 异步文件写审批: 沙箱内免审批, 其余按策略审批
    """
    if _in_sandbox(path, session):
        return True, ""
    return await _approve_async(
        session, engine, "file_write", RiskLevel.WRITE, f"{action} {path}"
    )


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
