"""基础插件工具兼容入口, 共用文档和记忆业务"""

from __future__ import annotations
import asyncio
import inspect
from pathlib import Path
from typing import Any, cast
from typing import Awaitable, Callable
import sys
from satrap.expend.plugins.base_take.core.docread import extract_text
from satrap.expend.plugins.base_take.state import get_plugin_state
from satrap.expend.tools.memory_store import MemoryStore
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.core.utils.sandbox import CodeSandbox
from satrap.core.utils.paths import get_project_root
from satrap.expend.tools import (
    AsyncCodeSandboxTool,
    AsyncFetchPageTool,
    AsyncSearchTool,
    CodeSandboxTool,
    FetchPageTool,
    SearchTool,
)
from satrap.core.type import safe_getattr, safe_getattr_callable
from satrap.edictum import AsyncSimpleSession, SimpleSession
from .utils import (
    SessionType,
    DEFAULT_SANDBOX_ROOT,
    _call_user_input_provider,
    _execution_approved,
    _make_sync_execution_authorizer,
    _make_async_execution_authorizer,
    _resolve_doc_path,
    _doc_workspace_root,
    _doc_upload_root,
)
from .sync import (
    ReadDocumentTool,
    _MemoryToolBase,
    AddMemoryTool,
    UpdateMemoryTool,
    DeleteMemoryTool,
    ListMemoriesTool,
)
from .async_ import (
    AsyncReadDocumentTool,
    _AsyncMemoryToolBase,
    AsyncAddMemoryTool,
    AsyncUpdateMemoryTool,
    AsyncDeleteMemoryTool,
    AsyncListMemoriesTool,
)


def get_tools(session: SessionType, config: dict[str, Any] | None = None) -> list[Any]:
    """
    按会话形态构建全部工具 (注入配置: timeout/sandbox_root/workspace_root/memory)

    参数:
    - session: 会话
    - config: 配置信息

    返回:
    - list[Any]: 按会话形态构建全部工具 (注入配置: timeout/sandbox_root/workspace_root/memory)
    """
    cfg = config or {}
    timeout = int(cfg.get("search_timeout") or 10)
    # 项目会话: 会话鸭子属性 (ChatService 建会话时赋值) 优先于插件配置
    ws_override = safe_getattr(session, "coding_workspace_root")
    sb_override = safe_getattr(session, "coding_sandbox_root")
    sandbox_root = Path(
        str(sb_override or cfg.get("sandbox_root") or DEFAULT_SANDBOX_ROOT)
    )
    workspace_root = Path(
        str(ws_override or cfg.get("workspace_root") or get_project_root())
    )

    state = get_plugin_state(session, cfg)
    store = state["store"]
    assert isinstance(store, MemoryStore)

    sandbox = CodeSandbox(str(sandbox_root), sys.executable)

    if isinstance(session, AsyncSimpleSession):
        tools: list[Any] = [
            AsyncSearchTool(timeout=timeout),
            AsyncFetchPageTool(timeout=timeout),
            AsyncCodeSandboxTool(sandbox, _make_async_execution_authorizer(session)),
            AsyncReadDocumentTool(workspace_root),
            AsyncAddMemoryTool(store),
            AsyncUpdateMemoryTool(store),
            AsyncDeleteMemoryTool(store),
            AsyncListMemoriesTool(store),
        ]
    else:
        tools = [
            SearchTool(timeout=timeout),
            FetchPageTool(timeout=timeout),
            CodeSandboxTool(sandbox, _make_sync_execution_authorizer(session)),
            ReadDocumentTool(workspace_root),
            AddMemoryTool(store),
            UpdateMemoryTool(store),
            DeleteMemoryTool(store),
            ListMemoriesTool(store),
        ]
    # 绑定会话 (read_document 等工具按会话解析工作区)
    for tool in tools:
        bind = safe_getattr_callable(tool, "_bind")
        if bind is not None:
            bind(session)
    return tools
