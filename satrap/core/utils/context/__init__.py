"""上下文管理兼容入口, 按算法与持久化职责组织同步异步实现"""

from __future__ import annotations
from dataclasses import dataclass
import aiosqlite
import threading
import asyncio
from pathlib import Path
import sqlite3
from typing import List, Dict, Union, Optional, Any, cast, TYPE_CHECKING, Literal
from types import TracebackType
import copy
import json
import time
from satrap.core.utils.tokenizer import tokenizer_estimate, experience_estimate
from satrap.core.state.mutation import state_mutation_context
from satrap.core.utils.vision import (
    DEFAULT_IMAGE_TOKEN_COST,
    build_multimodal_content,
    content_text_projection,
    estimate_content_image_count,
)
from satrap.core.utils.paths import get_db_path
from satrap.core.state import StateStore
from satrap.core.type import (
    ContextUsageSnapshot,
    JsonRow,
    RestoreOptions,
    SnapshotDomain,
    StateCheckpoint,
    StateScope,
    TokenUsage,
)
from satrap.core.log import logger
from .utils import (
    TokenEstimateMethod,
    ContextStrategy,
    _SUMMARY_PROMPT_VERSION,
    _MIN_SUMMARY_OUTPUT_TOKENS,
    _MAX_SUMMARY_OUTPUT_TOKENS,
    _SUMMARY_RETRY_OUTPUT_TOKENS,
    _summary_output_budget,
    ContextOverflowError,
    PreparedModelContext,
    _ContextRuntimeState,
    _estimate_text,
    _estimate_request_tokens,
    _llm_model_name,
    _summary_lines,
    _split_summary_chunks,
    _messages_domain,
    _message_content_json,
    _load_message_content,
    _load_tool_calls,
    add_user_message,
    add_bot_message,
    add_tool_message,
    add_tools_call_flow,
    clear_reasoning_content,
)
from .sync import ContextManager
from .async_ import AsyncContextManager
