"""
同步文件修改与批量替换工具

复用公共业务规则, 根据操作性质协调会话交互与文件执行
"""

from __future__ import annotations

from typing import Any

from satrap.expend.plugins.satrap_coding.core.permission import PermissionEngine
from satrap.core.utils.TCBuilder import Tool
from satrap.edictum import SimpleSession
from .contracts import require_bound_session, prepare_replacements
from .file_io import write_file, edit_file, replace_file
from .paths import (
    _tool_root,
    _resolve_path,
    _protection_reason,
    _approve_file_write,
)

class WriteFileTool(Tool):
    """写入/追加工作区内文件 (写操作走审批)"""

    tool_name = "write_file"
    description = "写入或追加内容到工作区内文件 (需用户批准)"
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "content": ("string", "要写入的内容"),
        "append": ("boolean", "是否追加, 默认 False (覆盖)"),
    }

    def __init__(self, engine: PermissionEngine) -> None:
        """
        初始化 WriteFileTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine
        self._session: SimpleSession | None = None

    def execute(self, path: str, content: str, append: bool = False) -> str:
        """
        同步执行文件修改, 全部校验通过后请求审批

        参数:
        - path: 文件路径, 绝对路径或相对工作区路径
        - content: 写入内容
        - append: 是否追加, 默认 False 表示覆盖

        返回:
        - 成功说明, 输入或读写错误, 或审批拒绝说明; 未绑定会话时抛出 RuntimeError
        """
        # Step.1 检查会话绑定并校验路径和修改输入
        session = require_bound_session(self._session, self.tool_name)
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason(abs_path, _tool_root(self))
        if reason is not None:
            return f"拒绝写入: {reason}"
        # Step.2 请求文件修改审批
        allowed, message = _approve_file_write(
            session, self.engine, abs_path, "写入文件"
        )
        if not allowed:
            return message
        # Step.3 执行已批准的文件修改
        return write_file(abs_path, content, append)

    def _bind(self, session: SimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class EditFileTool(Tool):
    """精确替换工作区内文件内容 (写操作走审批)"""

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
        初始化 EditFileTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine
        self._session: SimpleSession | None = None

    def execute(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        """
        同步执行文件修改, 全部校验通过后请求审批

        参数:
        - path: 文件路径, 绝对路径或相对工作区路径
        - old: 需要匹配的原文
        - new: 替换后的文本
        - replace_all: 是否替换全部匹配, 默认 False 仅替换首处

        返回:
        - 成功说明, 输入或读写错误, 或审批拒绝说明; 未绑定会话时抛出 RuntimeError
        """
        # Step.1 检查会话绑定并校验路径和修改输入
        session = require_bound_session(self._session, self.tool_name)
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason(abs_path, _tool_root(self))
        if reason is not None:
            return f"拒绝修改: {reason}"
        if not abs_path.is_file():
            return f"错误: 文件不存在: {abs_path}"
        try:
            content = abs_path.read_text(encoding="utf-8")
        except OSError as e:
            return f"错误: 读取失败: {e}"
        if old not in content:
            return f"错误: 未找到匹配文本: {old[:80]}"
        # Step.2 请求文件修改审批
        allowed, message = _approve_file_write(
            session, self.engine, abs_path, "编辑文件"
        )
        if not allowed:
            return message
        # Step.3 执行已批准的文件修改
        return edit_file(abs_path, content, old, new, replace_all)

    def _bind(self, session: SimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class SearchReplaceTool(Tool):
    """批量精确替换: 一个文件内多对 old->new (类似 IDE search & replace)"""

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
        初始化 SearchReplaceTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine
        self._session: SimpleSession | None = None

    def execute(self, path: str, replacements: list[dict[str, Any]]) -> str:
        """
        同步执行文件修改, 全部校验通过后请求审批

        参数:
        - path: 文件路径, 绝对路径或相对工作区路径
        - replacements: 替换项列表, 全部通过校验后才审批和写入

        返回:
        - 成功说明, 输入或读写错误, 或审批拒绝说明; 未绑定会话时抛出 RuntimeError
        """
        # Step.1 检查会话绑定并校验路径和修改输入
        session = require_bound_session(self._session, self.tool_name)
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason(abs_path, _tool_root(self))
        if reason is not None:
            return f"拒绝修改: {reason}"
        if not abs_path.is_file():
            return f"错误: 文件不存在: {abs_path}"
        try:
            content = abs_path.read_text(encoding="utf-8")
        except OSError as e:
            return f"错误: 读取失败: {e}"
        pairs = prepare_replacements(content, replacements)
        if isinstance(pairs, str):
            return pairs
        # Step.2 请求文件修改审批
        allowed, message = _approve_file_write(
            session, self.engine, abs_path, "批量替换"
        )
        if not allowed:
            return message
        # Step.3 执行已批准的文件修改
        return replace_file(abs_path, content, pairs)

    def _bind(self, session: SimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session
