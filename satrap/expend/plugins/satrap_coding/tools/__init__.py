"""
编程插件工具构造入口

应用插件配置并构造同步或异步工具, 绑定会话权限引擎与工作区,
将环境变量放行列表和额外保护目录按工具实例注入, 不保存为跨会话共享权限
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
from satrap.core.type import safe_getattr, safe_getattr_callable
from .sync_interaction import AskUserTool, ShellTool, SubAgentTool
from satrap.edictum import AsyncSimpleSession, SimpleSession
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
    DATA_ROOT,
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


def _apply_config(config: dict[str, Any]) -> None:
    """
    应用工作区与数据路径的全局兜底配置, 不保存会话权限策略

    参数:
    - config: 配置信息

    注: 这些死参是模块级常量, 作为**全局兜底**被路径辅助函数 (_resolve_path 等) 引用;
    在 get_tools 工厂调用时更新为配置值 (全局单例语义);
    项目会话的独立工作区不经此处 -- 由会话鸭子属性 coding_workspace_root 按会话覆盖
    (_workspace_root/_tool_root 优先读会话属性, 回落此处全局值);
    空配置不动任何模块变量 (保持代码默认/测试 monkeypatch)
    """
    if not config:
        return
    global WORKSPACE_ROOT, DATA_ROOT, DEFAULT_SANDBOX_ROOT
    if config.get("workspace_root"):
        WORKSPACE_ROOT = Path(str(config["workspace_root"])).resolve()
        DATA_ROOT = WORKSPACE_ROOT / ".satrap" / "coding"
    if config.get("data_root"):
        DATA_ROOT = Path(str(config["data_root"])).resolve()
    if config.get("sandbox_root"):
        DEFAULT_SANDBOX_ROOT = Path(str(config["sandbox_root"])).resolve()


def get_tools(
    session: SimpleSession | AsyncSimpleSession, config: dict[str, Any] | None = None
) -> list[Any]:
    """
    按会话形态构建全部工具 (注入 llm / 权限引擎 / 会话引用 + 应用插件配置)

    参数:
    - session: 会话
    - config: 配置信息

    注: search/fetch_page/memory 已移交 base_take 插件, 本插件只保留 coding 专属能力

    返回:
    - list[Any]: 按会话形态构建全部工具 (注入 llm / 权限引擎 / 会话引用 + 应用插件配置)
    """
    _apply_config(config or {})
    allowed_env = parse_allowed_env_vars(config or {})
    protected_dirs = frozenset(
        name.strip().lower()
        for name in str((config or {}).get("protected_dirs") or "").split(",")
        if name.strip()
    )

    session_cache_root = safe_getattr(session, "coding_cache_root")
    state_root = (
        Path(str(session_cache_root)) / "satrap_coding" if session_cache_root else None
    )
    state = get_plugin_state(session, state_root)
    engine = cast(PermissionEngine, state["engine"])
    todos = cast(dict[str, Any], state["todos"])

    if isinstance(session, AsyncSimpleSession):
        tools: list[Any] = [
            AsyncAskUserTool(),
            AsyncShellTool(engine, allowed_env),
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
            ShellTool(engine, allowed_env),
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
        if isinstance(tool, _FileProtectionMixin):
            tool.protected_dirs = protected_dirs
        bind = safe_getattr_callable(tool, "_bind")
        if bind is not None:
            bind(session)
    return tools
