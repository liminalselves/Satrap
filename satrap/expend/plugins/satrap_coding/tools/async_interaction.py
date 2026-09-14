"""
异步用户询问与命令执行工具

复用公共业务规则, 根据操作性质协调会话交互与文件执行
"""

from __future__ import annotations

import asyncio
from typing import Any

from satrap.expend.plugins.satrap_coding.core.permission import PermissionEngine
from satrap.core.utils.TCBuilder import AsyncTool
from .sync_interaction import ShellTool
from satrap.edictum import AsyncSimpleSession
from .contracts import require_bound_session
from .subagent import _AsyncCodingSubAgent
from .utils import (
    _SUBAGENT_PROMPT,
    _parse_integer_argument,
    _ask_user_async,
    _approve_async,
    _tool_root,
    _resolve_path,
    _prepare_shell,
    _run_shell,
)

class AsyncAskUserTool(AsyncTool):
    """向用户询问请求 (异步)"""

    tool_name = "ask_user"
    description = (
        "向用户提出一个问题并等待回复, 用于获取缺失信息或确认意图; "
        "建议提供 2-3 个推荐选项 (options), 用户可直接输入序号选择; "
        "需要宿主通过 session.user_input_provider 适配用户输入通道"
    )
    params_dict = {
        "question": ("string", "要询问的问题"),
        "options": ("array", "推荐回答选项 (2-3 个), 如 ['方案A', '方案B']"),
    }

    def __init__(self) -> None:
        """初始化 AsyncAskUserTool"""
        super().__init__()
        self._session: AsyncSimpleSession | None = None

    async def execute(self, question: str, options: list[str] | None = None) -> str:
        """
        异步向用户询问并返回回复

        参数:
        - question: 需要用户回答的问题
        - options: 推荐选项, 默认 None 表示不提供选项

        返回:
        - 用户回复或需要回复的说明; 未绑定会话时抛出 RuntimeError
        """
        session = require_bound_session(self._session, self.tool_name)
        answer = await _ask_user_async(session, question, options)
        if answer is None:
            return (
                "需要用户回复: "
                + question
                + " (未配置 user_input_provider, 请回复后继续)"
            )
        return f"用户回复: {answer}"

    def _bind(self, session: AsyncSimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class AsyncShellTool(AsyncTool):
    """执行本机 shell 命令 (异步), 每次执行均需批准"""

    tool_name = "shell"
    description = ShellTool.description
    params_dict = {
        "command": ("string", "要执行的命令"),
        "cwd": ("string", "工作目录, 默认项目根"),
        "timeout": ("number", "超时秒数, 范围 1-3600, 默认 120"),
        "shell": ("string", "shell 类型: powershell / cmd, 默认 powershell"),
    }

    def __init__(self, engine: PermissionEngine) -> None:
        """
        初始化 AsyncShellTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine
        self._session: AsyncSimpleSession | None = None

    async def execute(
        self,
        command: str,
        cwd: str = "",
        timeout: int | float | str = 120,
        shell: str = "powershell",
    ) -> str:
        """
        异步审批并执行 Shell 命令

        参数:
        - command: 待审批的 Shell 命令
        - cwd: 工作目录, 默认空字符串表示工作区根目录
        - timeout: 超时秒数, 默认 120, 范围 1-3600
        - shell: Shell 类型, 默认 powershell

        返回:
        - 执行结果, 参数错误或审批拒绝说明; 未绑定会话时抛出 RuntimeError
        """
        # Step.1 检查会话绑定并准备命令参数
        session = require_bound_session(self._session, self.tool_name)
        from . import _run_shell

        timeout_value, error = _parse_integer_argument(
            timeout,
            "timeout",
            minimum=1,
            maximum=3600,
        )
        if error is not None or timeout_value is None:
            return error or "错误: timeout 无效"
        try:
            root = _tool_root(self)
            workdir_path = _resolve_path(cwd, root) if cwd else root
            args, risk, description = _prepare_shell(command, shell, root, workdir_path)
        except ValueError as e:
            return f"错误: {e}"
        # Step.2 请求命令执行审批
        allowed, message = await _approve_async(
            session, self.engine, "shell", risk, description
        )
        if not allowed:
            return message
        # Step.3 复核执行环境并运行已批准的命令
        if (
            self.engine.plan_mode
            or _tool_root(self) != root
            or workdir_path.resolve() != workdir_path
        ):
            return "执行已取消: 审批期间计划模式或工作区发生变化"
        return await asyncio.to_thread(_run_shell, args, workdir_path, timeout_value)

    def _bind(self, session: AsyncSimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class AsyncSubAgentTool(AsyncTool):
    """子代理 (异步): 独立上下文, 继承主会话工具与审批策略"""

    tool_name = "subagent"
    description = "在独立上下文中运行子代理处理任务, 返回结果; 用于并行调研/独立子任务"
    params_dict = {
        "task": ("string", "子代理要完成的任务描述"),
        "tools": ("array", "允许使用的工具名白名单, 缺省使用全部工具"),
        "system_prompt": ("string", "自定义子代理系统提示词"),
        "max_turns": ("number", "最大工具迭代轮数, 范围 1-100, 默认 20"),
    }

    def __init__(self, llm: Any, tools_manager: Any) -> None:
        """
        初始化 AsyncSubAgentTool

        参数:
        - llm: 模型实例
        - tools_manager: 工具管理器实例
        """
        super().__init__()
        self.llm = llm
        self.tools_manager = tools_manager

    async def execute(
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
        agent = _AsyncCodingSubAgent(
            self.llm,
            self.tools_manager,
            system_prompt or _SUBAGENT_PROMPT,
            tools,
        )
        return await agent.forward(task, max_turns_value)
