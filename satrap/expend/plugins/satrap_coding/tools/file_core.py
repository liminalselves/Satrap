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
    description = "按行分页读取代码或纯文本；PDF、Office 文档、图片和视频使用 read_document（若已提供）"
    params_dict = {
        "path": {"type": "string", "minLength": 1, "description": "文件路径，绝对路径或相对工作区"},
        "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "起始行偏移"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200, "description": "最多读取多少行"},
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
    description = "查看目录内容；不传 path 时列出工作区根目录"
    params_dict = {
        "path": {"type": "string", "default": "", "description": "目录路径；省略表示工作区根目录"},
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
    description = "按 glob 模式查找文件名，例如 **/*.py；搜索文件内容用 grep_files"
    params_dict = {
        "pattern": {"type": "string", "minLength": 1, "description": "glob 模式，默认相对工作区"},
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
    description = "用正则搜索文件内容，返回文件、行号和匹配行；只找文件名时用 glob_files"
    params_dict = {
        "pattern": {"type": "string", "minLength": 1, "description": "正则表达式"},
        "path": {"type": "string", "default": "", "description": "搜索目录；省略表示工作区根目录"},
        "glob": {"type": "string", "default": "", "description": "文件过滤模式，例如 *.py；省略表示全部文件"},
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
    description = "管理多步任务清单：add 添加，done 完成，list 查看，clear 清空"
    params_dict = {
        "operation": {"type": "string", "enum": ["add", "done", "list", "clear"], "description": "要执行的操作"},
        "item": {"type": "string", "description": "任务内容；operation=add 时填写"},
        "index": {"type": "integer", "minimum": 1, "description": "任务序号，从 1 开始；operation=done 时填写"},
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
