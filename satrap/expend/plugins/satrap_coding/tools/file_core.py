from __future__ import annotations
from typing import Any
import os
import re
from .utils import (
    _parse_integer_argument,
    _tool_root,
    _resolve_path,
    _resolve_grep_file,
    _protection_reason_full,
    _read_file_page,
)
from satrap.core.utils.TCBuilder.tool_base import _ToolBase


class _ReadFileToolCore(_ToolBase):
    """读取工作区内文件 (支持分页)"""

    recovery_policy = "retry"

    tool_name = "read_file"
    description = "读取工作区内文件内容, offset/limit 支持分页读取大文件"
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "offset": ("number", "起始行号, 默认 0"),
        "limit": ("number", "读取行数上限, 默认 200"),
    }

    def __init__(self) -> None:
        """初始化 ReadFileTool"""
        super().__init__()

    def _execute(self, path: str, offset: int = 0, limit: int = 200) -> str:
        """
        执行

        参数:
        - path: 路径
        - offset: 偏移量
        - limit: 数量上限

        返回:
        - str: 执行
        """
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path, _tool_root(self))
        if reason is not None:
            return f"拒绝读取: {reason}"
        if not abs_path.is_file():
            return f"错误: 文件不存在: {abs_path}"
        start, error = _parse_integer_argument(
            offset, "offset", minimum=0, maximum=10_000_000
        )
        if error is not None or start is None:
            return error or "错误: offset 无效"
        page_size, error = _parse_integer_argument(
            limit, "limit", minimum=1, maximum=2000
        )
        if error is not None or page_size is None:
            return error or "错误: limit 无效"
        return _read_file_page(abs_path, start, page_size)


class _ListDirToolCore(_ToolBase):
    """列出工作区内目录内容"""

    recovery_policy = "retry"

    tool_name = "list_dir"
    description = "列出工作区内目录下的文件与子目录"
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
            base = _resolve_path(path, root) if path else root
        except ValueError as e:
            return f"错误: {e}"
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


class _GlobFilesToolCore(_ToolBase):
    """按 glob 模式搜索工作区内文件"""

    recovery_policy = "retry"

    tool_name = "glob_files"
    description = "按 glob 模式递归搜索文件, 如 **/*.py"
    params_dict = {
        "pattern": ("string", "glob 模式 (相对工作区)"),
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
        matches = [
            str(p.relative_to(root)).replace(os.sep, "/")
            for p in root.glob(pattern)
            if p.is_file()
            and p.resolve().is_relative_to(root)
            and _protection_reason_full(p, root) is None
        ]
        matches.sort()
        if not matches:
            return "未找到匹配文件"
        return "\n".join(matches[:100]) + (
            f"\n... 共 {len(matches)} 个" if len(matches) > 100 else ""
        )


class _GrepFilesToolCore(_ToolBase):
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
            base = _resolve_path(path, root) if path else root
        except ValueError as e:
            return f"错误: {e}"
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"错误: 正则无效: {e}"
        hits: list[str] = []
        for p in base.rglob(glob or "*"):
            resolved = _resolve_grep_file(p, root)
            if resolved is None or _protection_reason_full(resolved, root) is not None:
                continue
            try:
                text = resolved.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{p.relative_to(root)}:{lineno}: {line[:200]}")
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
