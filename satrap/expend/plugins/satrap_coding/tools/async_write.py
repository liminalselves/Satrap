"""
异步文件修改与批量替换工具

复用公共业务规则, 根据操作性质协调会话交互与文件执行
"""

from __future__ import annotations

import asyncio

from satrap.core.approval.policy import PermissionEngine, RiskLevel
from satrap.core.utils.TCBuilder import AsyncTool
from satrap.edictum import AsyncSimpleSession
from .contracts import require_bound_session
from .approval import _approve_async
from .file_io import prepare_change
from .paths import _tool_root, _resolve_path, _FileProtectionMixin, _execution_guard, _full_access, _in_sandbox

from satrap.core.log import logger

class AsyncWriteFileTool(_FileProtectionMixin, AsyncTool):
    """写入/追加工作区内文件 (异步, 写操作走审批)"""

    tool_name = "write_file"
    description = "写入或追加文件; 沙箱内免审批, 其余遵守 user/auto/full-access 模式"
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
        异步执行文件修改, 全部校验通过后请求审批

        参数:
        - path: 文件路径, 绝对路径或相对工作区路径
        - content: 写入内容
        - append: 是否追加, 默认 False 表示覆盖

        返回:
        - 成功说明, 输入或读写错误, 或审批拒绝说明; 未绑定会话时抛出 RuntimeError
        """
        # Step.1 冻结实际修改内容和执行边界
        session = require_bound_session(self._session, self.tool_name)
        try:
            root = _tool_root(self)
            abs_path = _resolve_path(path, root, full_access=_full_access(self))
            check = _execution_guard(self, self.engine, abs_path)
            check()
            change = await asyncio.to_thread(prepare_change, abs_path, self.tool_name, content=content, append=append)
        except (OSError, ValueError) as error:
            logger.warning(f"[satrap_coding] 文件准备失败: {error}")
            return f"错误: {error}"
        # Step.2 沙箱内免审批, 其余提交精确动作
        if not _in_sandbox(abs_path, session, self.sandbox_root):
            allowed, message = await _approve_async(
                session, self.engine, "file_write", RiskLevel.WRITE, f"{self.tool_name} {abs_path}",
                action=change.action(), tool_name=self.tool_name)
            if not allowed:
                return message
        # Step.3 在实际写入线程内重新检查并提交
        return await asyncio.to_thread(change.commit, check)

    def _bind(self, session: AsyncSimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class AsyncEditFileTool(_FileProtectionMixin, AsyncTool):
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
        异步执行文件修改, 全部校验通过后请求审批

        参数:
        - path: 文件路径, 绝对路径或相对工作区路径
        - old: 需要匹配的原文
        - new: 替换后的文本
        - replace_all: 是否替换全部匹配, 默认 False 仅替换首处

        返回:
        - 成功说明, 输入或读写错误, 或审批拒绝说明; 未绑定会话时抛出 RuntimeError
        """
        # Step.1 冻结实际修改内容和执行边界
        session = require_bound_session(self._session, self.tool_name)
        try:
            root = _tool_root(self)
            abs_path = _resolve_path(path, root, full_access=_full_access(self))
            check = _execution_guard(self, self.engine, abs_path)
            check()
            change = await asyncio.to_thread(prepare_change, abs_path, self.tool_name, old=old, new=new, replace_all=replace_all)
        except (OSError, ValueError) as error:
            logger.warning(f"[satrap_coding] 文件准备失败: {error}")
            return f"错误: {error}"
        # Step.2 沙箱内免审批, 其余提交精确动作
        if not _in_sandbox(abs_path, session, self.sandbox_root):
            allowed, message = await _approve_async(
                session, self.engine, "file_write", RiskLevel.WRITE, f"{self.tool_name} {abs_path}",
                action=change.action(), tool_name=self.tool_name)
            if not allowed:
                return message
        # Step.3 在实际写入线程内重新检查并提交
        return await asyncio.to_thread(change.commit, check)

    def _bind(self, session: AsyncSimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class AsyncSearchReplaceTool(_FileProtectionMixin, AsyncTool):
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

    async def execute(self, path: str, replacements: list[dict[str, object]]) -> str:
        """
        异步执行文件修改, 全部校验通过后请求审批

        参数:
        - path: 文件路径, 绝对路径或相对工作区路径
        - replacements: 替换项列表, 全部通过校验后才审批和写入

        返回:
        - 成功说明, 输入或读写错误, 或审批拒绝说明; 未绑定会话时抛出 RuntimeError
        """
        # Step.1 冻结实际修改内容和执行边界
        session = require_bound_session(self._session, self.tool_name)
        try:
            root = _tool_root(self)
            abs_path = _resolve_path(path, root, full_access=_full_access(self))
            check = _execution_guard(self, self.engine, abs_path)
            check()
            change = await asyncio.to_thread(prepare_change, abs_path, self.tool_name, replacements=replacements)
        except (OSError, ValueError) as error:
            logger.warning(f"[satrap_coding] 文件准备失败: {error}")
            return f"错误: {error}"
        # Step.2 沙箱内免审批, 其余提交精确动作
        if not _in_sandbox(abs_path, session, self.sandbox_root):
            allowed, message = await _approve_async(
                session, self.engine, "file_write", RiskLevel.WRITE, f"{self.tool_name} {abs_path}",
                action=change.action(), tool_name=self.tool_name)
            if not allowed:
                return message
        # Step.3 在实际写入线程内重新检查并提交
        return await asyncio.to_thread(change.commit, check)

    def _bind(self, session: AsyncSimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session
