"""简易会话兼容入口, 注册表和业务实现按职责拆分"""

from __future__ import annotations
from dataclasses import dataclass
import threading
import asyncio
import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
import time
from uuid import uuid4
import sys
from satrap.edictum.plugin_config import (
    PluginConfigManager,
    parse_config_schema,
    schema_to_payload,
)
from satrap.edictum.plugin_settings import (
    EffectivePluginConfig,
    resolve_session_plugin_config,
)
from satrap.edictum.plugin_resources import PluginResources, MODEL_TYPES
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.utils.TCBuilder import AsyncTool, AsyncToolsManager, Tool, ToolsManager
from satrap.core.framework.Base import (
    AsyncModelWorkflowFramework,
    AsyncSession,
    ModelWorkflowFramework,
    Session,
)
from satrap.core.utils.skills import SkillsManager
from satrap.core.utils.paths import get_db_path
from satrap.edictum.plugin import (
    Plugin,
    collect_cleanup,
    collect_commands,
    collect_handlers,
    collect_mcp_clients,
    collect_skills,
    collect_tools,
    load_plugin_meta,
    parse_capability_descriptions,
)
from satrap.core.type import CommandAction, safe_getattr_callable
from satrap.core.log import logger
from .utils import (
    SyncUserInputProvider,
    AsyncUserInputProvider,
    HandlerResult,
    HandlerAbortError,
    BeforeUserSend,
    AfterUserSend,
    BeforeModelReply,
    AfterModelReply,
    HandlerConfig,
    HandlerContext,
    HANDLER_TIMEOUT,
    _assert_sync_callbacks,
    _command_intro,
    _add_plugin_sys_path,
    _remove_plugin_sys_path,
    _warn_undeclared_capabilities,
    SessionHandler,
)
from .handlers import _HandlerRegistryMixin
from .sync import SimpleSession
from .async_ import AsyncSimpleSession
