"""
宿主文件保护规则

工作区与当前会话上传目录, 沙箱分别判定;
敏感文件和保护目录在所有审批模式下均拒绝访问
"""
from __future__ import annotations

from pathlib import Path

from satrap.core.utils.paths import get_project_root


PROTECTED_DIRS = (".satrap", ".git", "node_modules")
PROTECTED_FILES = (".env", ".env.local", "api-token", "model_config.json")


def protection_reason(
    path: Path, workspace: Path, *, protected_dirs: frozenset[str] = frozenset(),
    sandbox_root: Path | None = None, upload_root: Path | None = None,
    system_root: Path | None = None, sandbox_workspace_only: bool = True,
) -> str | None:
    """
    判断真实路径是否命中共享保护规则

    参数:
    - path: 已解析的真实目标路径
    - workspace: 当前工作区根
    - protected_dirs: 工具额外保护目录名, 默认空集合
    - sandbox_root: 当前会话沙箱, 默认无例外
    - upload_root: 当前会话上传目录, 默认无例外
    - system_root: 系统数据目录, 默认项目的 .satrap
    - sandbox_workspace_only: 默认保留写工具的工作区约束, 受控只读操作可使用独立会话沙箱

    返回:
    - 拒绝原因, 普通文件返回 None; 会话目录例外不放开其中的敏感文件
    """
    resolved = path.resolve()
    sandbox = sandbox_root if (sandbox_root is not None and resolved.is_relative_to(sandbox_root)
                               and (not sandbox_workspace_only or resolved.is_relative_to(workspace))) else None
    upload = upload_root if upload_root is not None and resolved.is_relative_to(upload_root) else None
    exception = upload or sandbox
    system = system_root if system_root is not None else (get_project_root() / ".satrap").resolve()
    if exception is None and (resolved == system or resolved.is_relative_to(system)):
        return "路径位于受保护目录 .satrap/ 下"
    root = exception or workspace
    relative = resolved.relative_to(root) if resolved.is_relative_to(root) else resolved
    parts = [part.lower() for part in relative.parts]
    if sandbox is not None and sandbox.is_relative_to(workspace):
        parts.extend(part.lower() for part in sandbox.relative_to(workspace).parts if part.lower() != ".satrap")
    if exception is not None and (upload is not None or not sandbox_workspace_only):
        parts.extend(part.lower() for part in exception.parts if part.lower() in PROTECTED_DIRS and part.lower() != ".satrap")
    for name in (*PROTECTED_DIRS, *sorted(protected_dirs)):
        if name in parts:
            return f"路径位于受保护目录 {name}/ 下"
    if resolved.name.lower() in PROTECTED_FILES:
        return f"路径命中受保护文件 {resolved.name}"
    return None
