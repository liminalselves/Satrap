"""
基础插件的工具构建入口

按会话类型创建搜索, 沙箱和文档工具, 合并宿主路径与插件配置
沙箱能力由执行器统一约束, 文档工具在调用时继续解析当前会话的可见路径
"""

from __future__ import annotations
import asyncio
import inspect
from pathlib import Path
from typing import Any, cast
from typing import Awaitable, Callable
import sys

from satrap.expend.plugins.base_take.core.docread import extract_text
from satrap.core.plugin_interop.environment import environment_snapshot
from satrap.edictum.plugin_resources import PluginResources
from satrap.core.utils.config_text import comma_names
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
from satrap.core.type import safe_getattr_callable
from satrap.edictum import AsyncSimpleSession, SimpleSession, PluginHost
from .async_ import (
    AsyncReadDocumentTool,
)
from .utils import (
    SessionType,
    DEFAULT_SANDBOX_ROOT,
    _doc_workspace_root,
)
from .sync import (
    ReadDocumentTool,
)


def get_tools(session: SessionType, config: dict[str, Any] | None = None,
              resources: PluginResources | None = None, *, plugin_host: PluginHost | None = None) -> list[Any]:
    """
    按会话类型构建并绑定基础插件的同步或异步工具

    参数:
    - session: 当前会话, 宿主注入的工作区与沙箱根优先于插件配置
    - config: 默认 None, 提供搜索超时, 路径和沙箱公开模块等配置
    - resources: 默认 None, 保留插件工厂协议参数, 本工厂不直接读取
    - plugin_host: 可选宿主访问入口, 独立工厂调用时从统一会话绑定读取

    返回:
    - 与会话同步或异步形态一致的工具列表, 沙箱描述使用同一生效白名单
    """
    # Step.1 合并插件配置与宿主绑定的业务目录
    cfg = config or {}
    timeout = int(cfg.get("search_timeout") or 10)
    environment = plugin_host.environment_snapshot() if plugin_host is not None else environment_snapshot(session)
    ws_override, sb_override = environment.workspace_root, environment.sandbox_root
    # 项目会话的宿主绑定路径优先, 仅独立调用者使用插件配置或默认目录
    sandbox_root = Path(
        str(sb_override or cfg.get("sandbox_root") or DEFAULT_SANDBOX_ROOT)
    )
    workspace_root = Path(
        str(ws_override or cfg.get("workspace_root") or get_project_root())
    )

    # Step.2 冻结沙箱配置, 描述与实际执行共用公开模块集合
    allowed_env = comma_names(cfg.get("allowed_env_vars"))
    sandbox = CodeSandbox(str(sandbox_root), sys.executable, allowed_env=allowed_env,
                          allowed_modules=cfg.get("sandbox_allowed_modules"))

    # Step.3 按当前会话形态选择成组的同步或异步适配器
    if isinstance(session, AsyncSimpleSession):
        tools: list[Any] = [
            AsyncSearchTool(timeout=timeout),
            AsyncFetchPageTool(timeout=timeout),
            AsyncCodeSandboxTool(sandbox),
            AsyncReadDocumentTool(workspace_root),
        ]
    else:
        tools = [
            SearchTool(timeout=timeout),
            FetchPageTool(timeout=timeout),
            CodeSandboxTool(sandbox),
            ReadDocumentTool(workspace_root),
        ]
    # Step.4 绑定会话, 供文档工具在调用时解析当前可见目录
    for tool in tools:
        setattr(tool, "_plugin_host", plugin_host)
        bind = safe_getattr_callable(tool, "_bind")
        if bind is not None:
            bind(session)
    return tools
