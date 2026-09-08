"""编程工具公共配置和辅助函数导出"""

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
from .paths import (
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
)
from .shell import _resolve_shell_executable, _prepare_shell, _run_shell
