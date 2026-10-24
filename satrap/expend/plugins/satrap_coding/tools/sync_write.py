"""
同步文件修改与批量替换工具

复用公共业务规则, 根据操作性质协调会话交互与文件执行
"""

from __future__ import annotations


from satrap.core.approval.policy import PermissionEngine, RiskLevel
from satrap.core.utils.TCBuilder import Tool
from satrap.edictum import SimpleSession
from .contracts import require_bound_session
from .approval import _approve_sync
from .file_io import prepare_change
from .paths import _tool_root, _resolve_path, _FileProtectionMixin, _execution_guard, _full_access, _in_sandbox

from satrap.core.log import logger

class WriteFileTool(_FileProtectionMixin, Tool):
    """写入/追加工作区内文件 (写操作走审批)"""

    tool_name = "write_file"
    description = "新建或覆盖文件；append=true 时追加到末尾。只改局部内容时优先用 edit_file"
    params_dict = {
        "path": {"type": "string", "minLength": 1, "description": "文件路径，绝对路径或相对工作区"},
        "content": {"type": "string", "description": "要写入的完整内容"},
        "append": {"type": "boolean", "default": False, "description": "true 追加，false 覆盖"},
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
        # Step.1 冻结实际修改内容和执行边界
        session = require_bound_session(self._session, self.tool_name)
        try:
            root = _tool_root(self)
            abs_path = _resolve_path(path, root, full_access=_full_access(self))
            check = _execution_guard(self, self.engine, abs_path)
            check()
            change = prepare_change(abs_path, self.tool_name, content=content, append=append)
        except (OSError, ValueError) as error:
            logger.warning(f"[satrap_coding] 文件准备失败: {error}")
            return f"错误: {error}"
        # Step.2 沙箱内免审批, 其余提交精确动作
        if not _in_sandbox(abs_path, session, self.sandbox_root):
            allowed, message = _approve_sync(
                session, self.engine, "file_write", RiskLevel.WRITE, f"{self.tool_name} {abs_path}",
                action=change.action(), tool_name=self.tool_name)
            if not allowed:
                return message
        # Step.3 在实际写入线程内重新检查并提交
        return change.commit(check)

    def _bind(self, session: SimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class EditFileTool(_FileProtectionMixin, Tool):
    """精确替换工作区内文件内容 (写操作走审批)"""

    tool_name = "edit_file"
    description = "把文件中的精确原文 old 替换为 new，适合小范围修改；多处不同位置修改可用 search_replace"
    params_dict = {
        "path": {"type": "string", "minLength": 1, "description": "文件路径，绝对路径或相对工作区"},
        "old": {"type": "string", "description": "必须精确匹配的原文"},
        "new": {"type": "string", "description": "替换后的内容"},
        "replace_all": {"type": "boolean", "default": False, "description": "true 替换全部匹配，false 只替换第一处"},
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
        # Step.1 冻结实际修改内容和执行边界
        session = require_bound_session(self._session, self.tool_name)
        try:
            root = _tool_root(self)
            abs_path = _resolve_path(path, root, full_access=_full_access(self))
            check = _execution_guard(self, self.engine, abs_path)
            check()
            change = prepare_change(abs_path, self.tool_name, old=old, new=new, replace_all=replace_all)
        except (OSError, ValueError) as error:
            logger.warning(f"[satrap_coding] 文件准备失败: {error}")
            return f"错误: {error}"
        # Step.2 沙箱内免审批, 其余提交精确动作
        if not _in_sandbox(abs_path, session, self.sandbox_root):
            allowed, message = _approve_sync(
                session, self.engine, "file_write", RiskLevel.WRITE, f"{self.tool_name} {abs_path}",
                action=change.action(), tool_name=self.tool_name)
            if not allowed:
                return message
        # Step.3 在实际写入线程内重新检查并提交
        return change.commit(check)

    def _bind(self, session: SimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class SearchReplaceTool(_FileProtectionMixin, Tool):
    """批量精确替换: 一个文件内多对 old->new (类似 IDE search & replace)"""

    tool_name = "search_replace"
    description = "在同一文件中批量替换多组精确原文；每组可单独选择替换首处或全部匹配"
    params_dict = {
        "path": {"type": "string", "minLength": 1, "description": "文件路径，绝对路径或相对工作区"},
        "replacements": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "old": {"type": "string", "minLength": 1, "description": "必须精确匹配的原文"},
                    "new": {"type": "string", "description": "替换后的内容"},
                    "replace_all": {"type": "boolean", "default": False, "description": "是否替换全部匹配"},
                },
                "required": ["old", "new"], "additionalProperties": False,
            },
            "description": "替换列表，按顺序执行",
        },
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

    def execute(self, path: str, replacements: list[dict[str, object]]) -> str:
        """
        同步执行文件修改, 全部校验通过后请求审批

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
            change = prepare_change(abs_path, self.tool_name, replacements=replacements)
        except (OSError, ValueError) as error:
            logger.warning(f"[satrap_coding] 文件准备失败: {error}")
            return f"错误: {error}"
        # Step.2 沙箱内免审批, 其余提交精确动作
        if not _in_sandbox(abs_path, session, self.sandbox_root):
            allowed, message = _approve_sync(
                session, self.engine, "file_write", RiskLevel.WRITE, f"{self.tool_name} {abs_path}",
                action=change.action(), tool_name=self.tool_name)
            if not allowed:
                return message
        # Step.3 在实际写入线程内重新检查并提交
        return change.commit(check)

    def _bind(self, session: SimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session
