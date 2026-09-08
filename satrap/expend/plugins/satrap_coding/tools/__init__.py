"""编程插件工具兼容入口, 配置仍以此模块为唯一来源"""

from __future__ import annotations
import subprocess
import asyncio
import inspect
from pathlib import Path
import shutil
from typing import Any, Awaitable, Callable, cast
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
from satrap.core.type import safe_getattr, safe_getattr_callable
from satrap.edictum import AsyncSimpleSession, SimpleSession
from satrap.core.log import logger
from .utils import (
    WORKSPACE_ROOT,
    DATA_ROOT,
    _APPROVAL_PROMPT,
    _PROTECTED_DIRS,
    _PROTECTED_FILES,
    _SYSTEM_PROTECTED_ROOT,
    _FILE_LINE_BREAK,
    _SUBAGENT_PROMPT,
    DEFAULT_SANDBOX_ROOT,
    _parse_integer_argument,
    _make_sync_judge,
    _make_async_judge,
    _ask_user_sync,
    _ask_user_async,
    _call_user_input_provider,
    _approve_sync,
    _approve_async,
    _workspace_root,
    _tool_root,
    _resolve_path,
    _resolve_grep_file,
    _protection_reason,
    _protection_reason_full,
    _session_sandbox_root,
    _in_sandbox,
    _approve_file_write,
    _approve_file_write_async,
    _read_file_page,
    _resolve_shell_executable,
    _prepare_shell,
    _run_shell,
)
from .sync_read import (
    ReadFileTool,
    ListDirTool,
    GlobFilesTool,
    GrepFilesTool,
    TodoWriteTool,
)
from .sync_write import WriteFileTool, EditFileTool, SearchReplaceTool
from .sync_interaction import AskUserTool, ShellTool, SubAgentTool
from .async_read import (
    AsyncReadFileTool,
    AsyncListDirTool,
    AsyncGlobFilesTool,
    AsyncGrepFilesTool,
    AsyncTodoWriteTool,
)
from .async_write import AsyncWriteFileTool, AsyncEditFileTool, AsyncSearchReplaceTool
from .async_interaction import AsyncAskUserTool, AsyncShellTool, AsyncSubAgentTool
from .subagent import _CodingSubAgent, _AsyncCodingSubAgent


def _apply_config(config: dict[str, Any]) -> None:
    """
    把合成配置应用到模块级死参 (WORKSPACE_ROOT/DATA_ROOT/SANDBOX_ROOT/超时/保护目录)

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
    global WORKSPACE_ROOT, DATA_ROOT, DEFAULT_SANDBOX_ROOT, _PROTECTED_DIRS
    if config.get("workspace_root"):
        WORKSPACE_ROOT = Path(str(config["workspace_root"])).resolve()
        DATA_ROOT = WORKSPACE_ROOT / ".satrap" / "coding"
    if config.get("data_root"):
        DATA_ROOT = Path(str(config["data_root"])).resolve()
    if config.get("sandbox_root"):
        DEFAULT_SANDBOX_ROOT = Path(str(config["sandbox_root"])).resolve()
    if config.get("protected_dirs"):
        extra = tuple(
            d.strip() for d in str(config["protected_dirs"]).split(",") if d.strip()
        )
        _PROTECTED_DIRS = (".satrap", ".git", "node_modules") + extra


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
            AsyncShellTool(engine),
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
            ShellTool(engine),
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
        bind = safe_getattr_callable(tool, "_bind")
        if bind is not None:
            bind(session)
    return tools
