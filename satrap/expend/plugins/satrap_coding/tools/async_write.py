from __future__ import annotations
import asyncio
from typing import Any
from satrap.expend.plugins.satrap_coding.core.permission import PermissionEngine
from satrap.core.utils.TCBuilder import AsyncTool
from satrap.edictum import AsyncSimpleSession
from .utils import (
    _tool_root,
    _resolve_path,
    _protection_reason_full,
    _approve_file_write_async,
)
from .file_io import write_file, edit_file, replace_file


class AsyncWriteFileTool(AsyncTool):
    """写入/追加工作区内文件 (异步, 写操作走审批)"""

    tool_name = "write_file"
    description = "写入或追加内容到工作区内文件 (需用户批准)"
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "content": ("string", "要写入的内容"),
        "append": ("boolean", "是否追加, 默认 False (覆盖)"),
    }

    def __init__(self, engine: PermissionEngine) -> None:
        """
        初始化 AsyncWriteFileTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine
        self._session: AsyncSimpleSession | None = None

    async def execute(self, path: str, content: str, append: bool = False) -> str:
        """
        执行

        参数:
        - path: 路径
        - content: 内容
        - append: 是否追加

        返回:
        - str: 执行
        """
        assert self._session is not None, "write_file 未绑定会话"
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path, _tool_root(self))
        if reason is not None:
            return f"拒绝写入: {reason}"
        allowed, message = await _approve_file_write_async(
            self._session,
            self.engine,
            abs_path,
            "写入文件",
        )
        if not allowed:
            return message

        return await asyncio.to_thread(write_file, abs_path, content, append)

    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session


class AsyncEditFileTool(AsyncTool):
    """精确替换工作区内文件内容 (异步, 写操作走审批)"""

    tool_name = "edit_file"
    description = "在文件中精确替换一段文本 (old -> new), 用于定向修改"
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "old": ("string", "要替换的原文 (必须精确匹配)"),
        "new": ("string", "替换后的内容"),
        "replace_all": ("boolean", "是否替换全部匹配, 默认 False (仅首处)"),
    }

    def __init__(self, engine: PermissionEngine) -> None:
        """
        初始化 AsyncEditFileTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine
        self._session: AsyncSimpleSession | None = None

    async def execute(
        self, path: str, old: str, new: str, replace_all: bool = False
    ) -> str:
        """
        执行

        参数:
        - path: 路径
        - old: 原值
        - new: 新值
        - replace_all: 是否全部替换

        返回:
        - str: 执行
        """
        assert self._session is not None, "edit_file 未绑定会话"
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path, _tool_root(self))
        if reason is not None:
            return f"拒绝修改: {reason}"
        if not abs_path.is_file():
            return f"错误: 文件不存在: {abs_path}"
        try:
            content = await asyncio.to_thread(abs_path.read_text, encoding="utf-8")
        except OSError as e:
            return f"错误: 读取失败: {e}"
        if old not in content:
            return f"错误: 未找到匹配文本: {old[:80]}"
        allowed, message = await _approve_file_write_async(
            self._session,
            self.engine,
            abs_path,
            "编辑文件",
        )
        if not allowed:
            return message

        return await asyncio.to_thread(
            edit_file, abs_path, content, old, new, replace_all
        )

    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session


class AsyncSearchReplaceTool(AsyncTool):
    """批量精确替换 (异步): 一个文件内多对 old->new"""

    tool_name = "search_replace"
    description = (
        "在一个文件中一次性执行多对精确文本替换 (类似 IDE 的 search & replace), "
        "每对可独立控制是否替换全部匹配; 审批规则同 edit_file"
    )
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "replacements": (
            "array",
            "替换列表, 每项为对象 {old: 原文, new: 新文, replace_all?: 是否替换全部匹配(默认 False)}",
        ),
    }

    def __init__(self, engine: PermissionEngine) -> None:
        """
        初始化 AsyncSearchReplaceTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine
        self._session: AsyncSimpleSession | None = None

    async def execute(self, path: str, replacements: list[dict[str, Any]]) -> str:
        """
        执行

        参数:
        - path: 路径
        - replacements: 替换项列表

        返回:
        - str: 执行
        """
        assert self._session is not None, "search_replace 未绑定会话"
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path, _tool_root(self))
        if reason is not None:
            return f"拒绝修改: {reason}"
        if not abs_path.is_file():
            return f"错误: 文件不存在: {abs_path}"
        try:
            content = await asyncio.to_thread(abs_path.read_text, encoding="utf-8")
        except OSError as e:
            return f"错误: 读取失败: {e}"
        pairs: list[tuple[str, str, bool]] = []
        for i, rep in enumerate(replacements or [], 1):
            if not isinstance(rep, dict) or not str(rep.get("old") or ""):
                return f"错误: 第 {i} 个替换项格式无效 (需 {{old, new, replace_all?}})"
            old = str(rep["old"])
            if old not in content:
                return f"错误: 第 {i} 个替换项未找到匹配: {old[:80]}"
            pairs.append((old, str(rep.get("new") or ""), bool(rep.get("replace_all"))))
        allowed, message = await _approve_file_write_async(
            self._session,
            self.engine,
            abs_path,
            "批量替换",
        )
        if not allowed:
            return message

        return await asyncio.to_thread(replace_file, abs_path, content, pairs)

    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session
