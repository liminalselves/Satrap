"""
同步用户询问与命令执行工具

复用公共业务规则, 根据操作性质协调会话交互与文件执行
"""

from __future__ import annotations

from typing import Any

from satrap.core.approval.policy import PermissionEngine
from satrap.core.APICall.LLMCall import LLM
from satrap.core.utils.TCBuilder import Tool, ToolsManager
from satrap.edictum import SimpleSession
from .contracts import require_bound_session
from .constants import _SUBAGENT_PROMPT
from .subagent import _CodingSubAgent
from .approval import _parse_integer_argument, _ask_user_sync, _approve_sync
from .paths import _tool_root, _resolve_path, _execution_guard, _full_access
from .shell import _prepare_shell

from satrap.core.log import logger

def _shell_definition(definition: dict[str, Any], default_timeout: int) -> dict[str, Any]:
    """
    补全 Shell 可省略参数与整数超时的模型调用定义

    参数:
    - definition: 通用工具生成器的结果
    - default_timeout: 当前实例的默认超时秒数

    返回:
    - dict[str, Any]: 仅 command 必填, 超时声明与执行范围一致的定义
    """
    if definition:
        parameters = definition["function"]["parameters"]
        parameters["required"] = ["command"]
        parameters["properties"]["timeout"].update(
            type="integer", minimum=1, maximum=3600, default=default_timeout
        )
        parameters["properties"]["shell"].update(
            enum=["auto", "powershell", "cmd", "bash", "sh"], default="auto"
        )
    return definition


class AskUserTool(Tool):
    """向用户询问请求, 等待用户回复"""

    tool_name = "ask_user"
    description = "缺少必要信息或需要用户确认时，提出一个问题并等待回答；可提供 2-3 个常用选项"
    params_dict = {
        "question": {"type": "string", "minLength": 1, "description": "要问用户的问题"},
        "options": {"type": "array", "maxItems": 3, "items": {"type": "string", "minLength": 1}, "description": "可选回答；不需要选项时省略"},
    }

    def __init__(self) -> None:
        """初始化 AskUserTool"""
        super().__init__()
        self._session: SimpleSession | None = None

    def execute(self, question: str, options: list[str] | None = None) -> str:
        """
        同步向用户询问并返回回复

        参数:
        - question: 需要用户回答的问题
        - options: 推荐选项, 默认 None 表示不提供选项

        返回:
        - 用户回复或需要回复的说明; 未绑定会话时抛出 RuntimeError
        """
        session = require_bound_session(self._session, self.tool_name)
        answer = _ask_user_sync(session, question, options)
        if answer is None:
            return (
                "需要用户回复: "
                + question
                + " (未配置 user_input_provider, 请回复后继续)"
            )
        return f"用户回复: {answer}"

    def _bind(self, session: SimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class ShellTool(Tool):
    """执行本机 shell 命令, 按 user/auto/full-access 决定审批"""

    tool_name = "shell"
    description = "在本机执行 Shell 命令, 默认按系统选择 PowerShell 或 bash/sh; 只读取文件时优先用文件工具, 是否审批由当前模式决定"
    params_dict = {
        "command": {"type": "string", "minLength": 1, "description": "要执行的完整命令"},
        "cwd": {"type": "string", "default": "", "description": "工作目录；省略表示当前会话工作区"},
        "timeout": {"type": "integer", "minimum": 1, "maximum": 3600, "default": 120, "description": "超时秒数"},
        "shell": {"type": "string", "enum": ["auto", "powershell", "cmd", "bash", "sh"], "default": "auto", "description": "命令解释器"},
    }

    def __init__(
        self, engine: PermissionEngine, allowed_env: frozenset[str] = frozenset(),
        default_timeout: int = 120,
    ) -> None:
        """
        初始化 ShellTool

        参数:
        - engine: 执行引擎
        - allowed_env: 本会话 shell 子进程显式放行的环境变量名
        - default_timeout: 省略执行参数时使用的超时秒数
        """
        super().__init__()
        self.engine = engine
        self.allowed_env = allowed_env
        self.default_timeout = default_timeout
        self.params_dict = {**self.params_dict, "timeout": {"type": "integer", "minimum": 1, "maximum": 3600, "default": default_timeout, "description": "超时秒数"}}
        self._session: SimpleSession | None = None

    def get_tool_defined(self) -> dict[str, Any]:
        """
        返回当前实例的 Shell 模型调用定义

        返回:
        - dict[str, Any]: 仅命令必填的工具定义
        """
        return _shell_definition(super().get_tool_defined(), self.default_timeout)

    def execute(
        self,
        command: str,
        cwd: str = "",
        timeout: int | float | str | None = None,
        shell: str = "auto",
    ) -> str:
        """
        同步审批并执行 Shell 命令

        参数:
        - command: 待审批的 Shell 命令
        - cwd: 工作目录, 默认空字符串表示工作区根目录
        - timeout: 超时秒数, None 表示使用实例配置, 范围 1-3600
        - shell: Shell 类型, 默认 auto 按系统选择

        返回:
        - 执行结果, 参数错误或审批拒绝说明; 未绑定会话时抛出 RuntimeError
        """
        # Step.1 检查会话绑定并准备命令参数
        session = require_bound_session(self._session, self.tool_name)
        from . import _run_shell

        timeout_value, error = _parse_integer_argument(
            self.default_timeout if timeout is None else timeout,
            "timeout",
            minimum=1,
            maximum=3600,
        )
        if error is not None or timeout_value is None:
            return error or "错误: timeout 无效"
        try:
            root = _tool_root(self)
            workdir_path = _resolve_path(cwd, root, full_access=_full_access(self)) if cwd else root
            args, risk, description = _prepare_shell(command, shell, root, workdir_path)
            check = _execution_guard(self, self.engine, workdir_path)
            check()
            allowed_env = self.allowed_env
        except (OSError, ValueError) as e:
            logger.warning(f"[satrap_coding] Shell 准备失败: {e}")
            return f"错误: {e}"
        # Step.2 请求命令执行审批
        allowed, message = _approve_sync(
            session, self.engine, "shell", risk, description,
            action={"command": command, "argv": args, "interpreter": args[0], "cwd": str(workdir_path),
                    "timeout": timeout_value, "allowed_env": sorted(allowed_env)}, tool_name=self.tool_name
        )
        if not allowed:
            return message
        # Step.3 在实际执行线程内核验来源与权限后运行固定参数
        def run() -> str:
            """紧邻启动本机进程执行最终复核"""
            try:
                check()
                return _run_shell(args, workdir_path, timeout_value, allow=allowed_env)
            except (OSError, ValueError) as error:
                logger.warning(f"[satrap_coding] Shell 执行取消: {error}")
                return f"执行已取消: {error}"
        return run()

    def _bind(self, session: SimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class SubAgentTool(Tool):
    """子代理: 独立上下文处理任务, 继承主会话工具与审批策略"""

    tool_name = "subagent"
    description = "把可独立完成的子任务交给独立上下文并返回结果摘要；任务说明需包含必要背景和预期输出"
    params_dict = {
        "task": {"type": "string", "minLength": 1, "description": "完整的子任务说明"},
        "tools": {"type": "array", "items": {"type": "string"}, "description": "允许使用的工具名白名单；省略表示继承全部工具"},
        "system_prompt": {"type": "string", "description": "需要覆盖默认行为时填写的子代理系统提示"},
        "max_turns": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "最大工具迭代轮数"},
    }

    def __init__(self, llm: LLM, tools_manager: ToolsManager) -> None:
        """
        初始化 SubAgentTool

        参数:
        - llm: 模型实例
        - tools_manager: 工具管理器实例
        """
        super().__init__()
        self.llm = llm
        self.tools_manager = tools_manager

    def execute(
        self,
        task: str,
        tools: list[str] | None = None,
        system_prompt: str = "",
        max_turns: int | float | str = 20,
    ) -> str:
        """
        执行

        参数:
        - task: 任务描述
        - tools: 工具集合
        - system_prompt: 系统prompt
        - max_turns: 最大工具迭代轮数, 范围 1-100, 默认 20

        返回:
        - str: 执行
        """
        max_turns_value, error = _parse_integer_argument(
            max_turns,
            "max_turns",
            minimum=1,
            maximum=100,
        )
        if error is not None or max_turns_value is None:
            return error or "错误: max_turns 无效"
        agent = _CodingSubAgent(
            self.llm,
            self.tools_manager,
            system_prompt or _SUBAGENT_PROMPT,
            tools,
        )
        return agent.forward(task, max_turns_value)
