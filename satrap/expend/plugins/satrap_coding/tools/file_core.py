"""
编程插件文件读取和待办工具的共用业务实现

统一参数校验, 工作区路径保护和内容读取, 供同步与异步工具入口复用
"""

from __future__ import annotations
from pathlib import Path
from typing import Any
import os
import re

from satrap.core.utils.TCBuilder.tool_base import _ToolBase
from satrap.core.file_access.reads import FileRead, prepare_file_read, read_error
from .approval import _parse_integer_argument
from .paths import (
    _tool_root,
    _full_access,
    _resolve_path,
    _resolve_grep_file,
    _FileProtectionMixin,
    _read_file_page,
    _tool_environment,
    _session_sandbox_root,
)


class _ReadFileToolCore(_FileProtectionMixin, _ToolBase):
    """分页读取单个文件, 目录外读取使用宿主审批"""

    recovery_policy = "retry"

    tool_name = "read_file"
    description = "分页读取单个文件; 工作区和本会话上传/沙箱内直接读取, 外部文件按 user/auto/full-access 审批"
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "offset": ("number", "起始行号, 默认 0"),
        "limit": ("number", "读取行数上限, 默认 200"),
    }

    def __init__(self) -> None:
        """初始化 ReadFileTool"""
        super().__init__()

    def _prepare_read(self, path: str, offset: int, limit: int) -> tuple[FileRead, int, int]:
        """
        校验分页并固定宿主文件读取目标

        参数:
        - path: 文件路径, 相对路径基于工作区
        - offset: 起始行偏移
        - limit: 最大返回行数

        返回:
        - 读取计划与规范化分页参数, 非法参数或保护路径抛出异常
        """
        from . import _SYSTEM_PROTECTED_ROOT

        start, error = _parse_integer_argument(offset, "offset", minimum=0, maximum=10_000_000)
        if error is not None or start is None:
            raise ValueError(error or "offset 无效")
        page_size, error = _parse_integer_argument(limit, "limit", minimum=1, maximum=2000)
        if error is not None or page_size is None:
            raise ValueError(error or "limit 无效")
        environment = _tool_environment(self)
        read = prepare_file_read(
            self, path, workspace=_tool_root(self), environment=environment,
            current_environment=lambda: _tool_environment(self),
            parameters={"offset": start, "limit": page_size},
            sandbox_root=_session_sandbox_root(getattr(self, "_session", None), self.sandbox_root),
            system_root=_SYSTEM_PROTECTED_ROOT,
        )
        return read, start, page_size

    def _execute(self, path: str, offset: int = 0, limit: int = 200) -> str:
        """
        同步审批并读取本次固定文件

        参数:
        - path: 文件路径
        - offset: 起始行偏移, 默认 0
        - limit: 返回行数, 默认 200

        返回:
        - 分页结果; 无批准或目标失效时返回明确错误
        """
        try:
            read, start, page_size = self._prepare_read(path, offset, limit)
            return read.read_sync(lambda target: _read_file_page(target, start, page_size))
        except Exception as error:
            return read_error(error)


class _ListDirToolCore(_FileProtectionMixin, _ToolBase):
    """列出工作区内目录内容"""

    recovery_policy = "retry"

    tool_name = "list_dir"
    description = "列出目录内容; 默认工作区, full-access 允许显式外部路径"
    params_dict = {
        "path": ("string", "目录路径, 默认工作区根"),
    }

    def _execute(self, path: str = "") -> str:
        """
        执行

        参数:
        - path: 路径

        返回:
        - str: 执行
        """
        try:
            root = _tool_root(self)
            base = _resolve_path(path, root, full_access=_full_access(self)) if path else root
        except ValueError as e:
            return f"错误: {e}"
        reason = self._protection_reason(base, root)
        if reason is not None:
            return f"拒绝读取: {reason}"
        if not base.is_dir():
            return f"错误: 目录不存在: {base}"
        entries = sorted(base.iterdir())
        lines = [f"{base}/:"]
        for entry in entries[:200]:
            tag = "/" if entry.is_dir() else ""
            lines.append(f"  {entry.name}{tag}")
        if len(entries) > 200:
            lines.append(f"  ... 共 {len(entries)} 项")
        return "\n".join(lines)


class _GlobFilesToolCore(_FileProtectionMixin, _ToolBase):
    """按 glob 模式搜索工作区内文件"""

    recovery_policy = "retry"

    tool_name = "glob_files"
    description = "按 glob 模式递归搜索文件, 如 **/*.py"
    params_dict = {
        "pattern": ("string", "glob 模式, 默认相对工作区; full-access 允许绝对模式"),
    }

    def _execute(self, pattern: str) -> str:
        """
        执行

        参数:
        - pattern: 匹配模式

        返回:
        - str: 执行
        """
        root = _tool_root(self)
        base, search = root, pattern
        if Path(pattern).is_absolute():
            if not _full_access(self):
                return "错误: 绝对 glob 路径需要 full-access"
            parts = Path(pattern).parts
            index = next((i for i, part in enumerate(parts) if any(c in part for c in "*?[")), len(parts) - 1)
            base, search = Path(*parts[:index]), str(Path(*parts[index:]))
        matches = [
            str(p.relative_to(root) if p.is_relative_to(root) else p).replace(os.sep, "/")
            for p in base.glob(search)
            if p.is_file()
            and (_full_access(self) or p.resolve().is_relative_to(root))
            and self._protection_reason(p, root) is None
        ]
        matches.sort()
        if not matches:
            return "未找到匹配文件"
        return "\n".join(matches[:100]) + (
            f"\n... 共 {len(matches)} 个" if len(matches) > 100 else ""
        )


class _GrepFilesToolCore(_FileProtectionMixin, _ToolBase):
    """正则搜索工作区内文件内容"""

    recovery_policy = "retry"

    tool_name = "grep_files"
    description = "在指定目录内按正则表达式搜索文件内容, 返回匹配行"
    params_dict = {
        "pattern": ("string", "正则表达式"),
        "path": ("string", "搜索目录, 默认工作区根"),
        "glob": ("string", "文件过滤, 如 *.py"),
    }

    def _execute(self, pattern: str, path: str = "", glob: str = "") -> str:
        """
        执行

        参数:
        - pattern: 匹配模式
        - path: 路径
        - glob: 文件匹配模式

        返回:
        - str: 执行
        """
        try:
            root = _tool_root(self)
            base = _resolve_path(path, root, full_access=_full_access(self)) if path else root
        except ValueError as e:
            return f"错误: {e}"
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"错误: 正则无效: {e}"
        hits: list[str] = []
        for p in base.rglob(glob or "*"):
            resolved = _resolve_grep_file(p, root, full_access=_full_access(self))
            if resolved is None or self._protection_reason(resolved, root) is not None:
                continue
            try:
                text = resolved.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{p.relative_to(root) if p.is_relative_to(root) else p}:{lineno}: {line[:200]}")
                    if len(hits) >= 100:
                        break
            if len(hits) >= 100:
                break
        if not hits:
            return "未找到匹配内容"
        return "\n".join(hits) + ("\n... 结果截断" if len(hits) >= 100 else "")


class _TodoWriteToolCore(_ToolBase):
    """任务清单: 多步任务跟踪 (会话级状态)"""

    tool_name = "todo_write"
    description = (
        "管理任务清单, 用于多步任务跟踪: add 添加, done 完成, list 查看, clear 清空"
    )
    params_dict = {
        "operation": ("string", "操作: add / done / list / clear"),
        "item": ("string", "任务内容 (add 时必填)"),
        "index": ("number", "任务序号, 从 1 开始 (done 时必填)"),
    }

    def __init__(self, todos: dict[str, Any]) -> None:
        """
        初始化 TodoWriteTool

        参数:
        - todos: 待办事项列表
        """
        super().__init__()
        self.todos = todos

    def _execute(self, operation: str, item: str = "", index: int = 0) -> str:
        """
        执行

        参数:
        - operation: 操作信息
        - item: 条目
        - index: 索引

        返回:
        - str: 执行
        """
        op = (operation or "").strip().lower()
        items = self.todos["items"]
        if op == "add":
            text = item.strip()
            if not text:
                return "用法: todo_write operation=add item=<任务内容>"
            items.append({"text": text, "done": False})
            return f"任务已添加 ({len(items)}): {text}"
        if op == "done":
            parsed_index, error = _parse_integer_argument(
                index,
                "index",
                minimum=1,
                maximum=max(1, len(items)),
            )
            if error is not None or parsed_index is None or parsed_index > len(items):
                return f"任务序号无效: {index} (共 {len(items)} 项)"
            items[parsed_index - 1]["done"] = True
            return f"任务 {parsed_index} 已完成: {items[parsed_index - 1]['text']}"
        if op == "list":
            if not items:
                return "任务清单为空"
            return "\n".join(
                f"{i}. [{'x' if t['done'] else ' '}] {t['text']}"
                for i, t in enumerate(items, 1)
            )
        if op == "clear":
            count = len(items)
            items.clear()
            return f"任务清单已清空 ({count} 项)"
        return "用法: todo_write operation=add|done|list|clear (add 需 item, done 需 index)"
