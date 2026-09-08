"""模型工作流和会话的兼容入口"""

from __future__ import annotations
import asyncio
import inspect, copy, json, uuid
from pathlib import Path
from typing import Optional, Callable, Any, Awaitable, TypeVar, cast, Literal
from typing import TYPE_CHECKING
from satrap.core.utils.context_policy import apply_context_policy
from satrap.core.framework.command import CommandHandler, AsyncCommandHandler
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.core.utils.TCBuilder import (
    Tool,
    create_tool_defined,
    ToolsManager,
    AsyncToolsManager,
)
from satrap.core.state.mutation import state_mutation_context
from satrap.core.utils.context import (
    add_user_message,
    add_bot_message,
    add_tool_message,
    add_tools_call_flow,
    clear_reasoning_content,
)
from satrap.core.utils.context import (
    AsyncContextManager,
    ContextManager,
    PreparedModelContext,
    _messages_domain,
)
from satrap.core.utils.paths import get_db_path
from satrap.core.state import StateStore
from satrap.core.type import (
    LLMCallResponse,
    LLMConfig,
    ModelContextRequestStats,
    ModelContextTurnStats,
    StateCheckpoint,
    TokenUsage,
)
from satrap.core.log import logger
from .utils import _WorkflowT, _build_context_request_stats

if TYPE_CHECKING:
    from satrap.core.config.session_overrides import SessionOverrideStore
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.core.storage.layout import StorageLayout
    from satrap.core.framework.SessionManager import SessionManager
    from satrap.core.framework.UserManager import UserManager

from .sync_workflow import ModelWorkflowFramework
from .async_workflow import AsyncModelWorkflowFramework
from .sync_session import Session
from .async_session import AsyncSession
