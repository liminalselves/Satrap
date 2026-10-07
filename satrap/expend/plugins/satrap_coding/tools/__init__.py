"""
编程插件工具构造入口

应用插件配置并构造同步或异步工具, 绑定会话权限引擎与工作区,
路径, 默认超时, 环境放行列表和保护目录按实例注入, 不保存为跨会话共享权限
"""

from __future__ import annotations
import subprocess
import asyncio
import inspect
from pathlib import Path
import shutil
from typing import (
    Any,
    Awaitable,
    Callable,
    cast,
)
import uuid
import os
import re

from satrap.expend.plugins.satrap_coding.core.command_gate import classify_command
from satrap.expend.plugins.satrap_coding.core.permission import (
    PermissionDecision,
    PermissionEngine,
    RiskLevel,
)
from satrap.expend.plugins.satrap_coding.state import get_plugin_state
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.core.framework.Base import (
    AsyncModelWorkflowFramework,
    ModelWorkflowFramework,
)
from satrap.core.utils.paths import get_project_root
from .async_interaction import AsyncAskUserTool, AsyncShellTool, AsyncSubAgentTool
from satrap.core.type import safe_getattr_callable
from .sync_interaction import AskUserTool, ShellTool, SubAgentTool
from satrap.edictum import AsyncSimpleSession, SimpleSession
from satrap.edictum.plugin_resources import PluginResources
from .async_write import AsyncWriteFileTool, AsyncEditFileTool, AsyncSearchReplaceTool
from .sync_write import WriteFileTool, EditFileTool, SearchReplaceTool
from .async_read import (
    AsyncReadFileTool,
    AsyncListDirTool,
    AsyncGlobFilesTool,
    AsyncGrepFilesTool,
    AsyncTodoWriteTool,
)
from .constants import (
    WORKSPACE_ROOT,
    _APPROVAL_PROMPT,
    _PROTECTED_DIRS,
    _PROTECTED_FILES,
    _SYSTEM_PROTECTED_ROOT,
    _FILE_LINE_BREAK,
    _SUBAGENT_PROMPT,
    DEFAULT_SANDBOX_ROOT,
)
from .sync_read import (
    ReadFileTool,
    ListDirTool,
    GlobFilesTool,
    GrepFilesTool,
    TodoWriteTool,
)
from .approval import (
    _parse_integer_argument,
    _make_sync_judge,
    _make_async_judge,
    _ask_user_sync,
    _ask_user_async,
    _call_user_input_provider,
    _approve_sync,
    _approve_async,
)
from .subagent import _CodingSubAgent, _AsyncCodingSubAgent
from .paths import (
    _workspace_root,
    _tool_root,
    _resolve_path,
    _resolve_grep_file,
    _protection_reason,
    _FileProtectionMixin,
    _session_sandbox_root,
    _in_sandbox,
    _approve_file_write,
    _approve_file_write_async,
    _read_file_page,
)
from .shell import _resolve_shell_executable, _prepare_shell, _run_shell

from satrap.core.log import logger


def parse_allowed_env_vars(config: dict[str, Any]) -> frozenset[str]:
    """
    解析插件配置中的 shell 环境变量放行列表

    参数:
    - config: 插件配置

    返回:
    - frozenset[str]: 显式放行的环境变量名, 未配置或为空时返回空集合
    """
    return frozenset(
        name.strip()
        for name in str(config.get("allowed_env_vars") or "").split(",")
        if name.strip()
    )


def get_tools(
    session: SimpleSession | AsyncSimpleSession, config: dict[str, Any] | None = None
) -> list[Any]:
    """
    按会话形态构建全部工具 (注入 llm / 权限引擎 / 会话引用 + 应用插件配置)

    参数:
    - session: 会话
    - config: 配置信息

    注: search/fetch_page 由 base_take 提供, memory 由独立记忆插件提供, 本插件只保留 coding 专属能力

    返回:
    - list[Any]: 按会话形态构建全部工具 (注入 llm / 权限引擎 / 会话引用 + 应用插件配置)
    """
    cfg = config or {}
    workspace_root = Path(str(cfg.get("workspace_root") or WORKSPACE_ROOT)).resolve()
    sandbox_root = Path(str(cfg.get("sandbox_root") or DEFAULT_SANDBOX_ROOT)).resolve()
    shell_timeout, error = _parse_integer_argument(
        cfg.get("shell_timeout", 120), "shell_timeout", minimum=1, maximum=3600
    )
    if error is not None or shell_timeout is None:
        raise ValueError(error or "shell_timeout 无效")
    allowed_env = parse_allowed_env_vars(cfg)
    protected_dirs = frozenset(
        name.strip().lower()
        for name in str(cfg.get("protected_dirs") or "").split(",")
        if name.strip()
    )

    state = get_plugin_state(session, config=cfg)
    engine = cast(PermissionEngine, state["engine"])
    todos = cast(dict[str, Any], state["todos"])

    if isinstance(session, AsyncSimpleSession):
        tools: list[Any] = [
            AsyncAskUserTool(),
            AsyncShellTool(engine, allowed_env, shell_timeout),
            AsyncSubAgentTool(session.llm, session.tools_manager),
            AsyncReadFileTool(),
            AsyncWriteFileTool(engine),
            AsyncEditFileTool(engine),
            AsyncSearchReplaceTool(engine),
            AsyncTodoWriteTool(todos),
            AsyncListDirTool(),
            AsyncGlobFilesTool(),
            AsyncGrepFilesTool(),
        ]
    else:
        tools = [
            AskUserTool(),
            ShellTool(engine, allowed_env, shell_timeout),
            SubAgentTool(session.llm, session.tools_manager),
            ReadFileTool(),
            WriteFileTool(engine),
            EditFileTool(engine),
            SearchReplaceTool(engine),
            TodoWriteTool(todos),
            ListDirTool(),
            GlobFilesTool(),
            GrepFilesTool(),
        ]
    for tool in tools:
        tool.workspace_root = workspace_root
        tool.sandbox_root = sandbox_root
        if isinstance(tool, _FileProtectionMixin):
            tool.protected_dirs = protected_dirs
        bind = safe_getattr_callable(tool, "_bind")
        if bind is not None:
            bind(session)
    return tools
