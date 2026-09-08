"""
模型工作流与会话基础框架

定义同步和异步工作流的模型调用, 工具执行与上下文处理流程,
并为会话提供命令, 状态检查点和持久化能力
"""

from __future__ import annotations
from typing import TypeVar
from typing import TYPE_CHECKING
from satrap.core.utils.context import (
    AsyncContextManager,
    ContextManager,
    PreparedModelContext,
)
from satrap.core.type import ModelContextRequestStats, TokenUsage


if TYPE_CHECKING:
    from satrap.core.config.session_overrides import SessionOverrideStore
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.core.storage.layout import StorageLayout
    from satrap.core.framework.SessionManager import SessionManager
    from satrap.core.framework.UserManager import UserManager

_WorkflowT = TypeVar("_WorkflowT")

"""工作流类泛型, 用于 create 工厂与 await 工具"""


def _build_context_request_stats(
    prepared: PreparedModelContext,
    usage: TokenUsage | None,
) -> ModelContextRequestStats:
    """
    将上下文准备结果和 API usage 合并为一次请求统计

    参数:
    - prepared: 上下文准备结果
    - usage: API 返回的真实 token 用量

    返回:
    - ModelContextRequestStats: 请求统计
    """
    return ModelContextRequestStats(
        model=prepared.model,
        strategy=prepared.strategy,
        compressed=prepared.compressed,
        original_turns=prepared.original_turns,
        prepared_turns=prepared.prepared_turns,
        original_estimated_input_tokens=prepared.original_estimated_input_tokens,
        estimated_input_tokens=prepared.estimated_input_tokens,
        effective_input_tokens=prepared.effective_input_tokens,
        preflight_token_source=prepared.token_source,
        history_budget=prepared.history_budget,
        trigger_tokens=prepared.trigger_tokens,
        floor_tokens=prepared.floor_tokens,
        api_input_tokens=usage.input_tokens if usage is not None else None,
        api_output_tokens=usage.output_tokens if usage is not None else None,
        api_total_tokens=usage.total_tokens if usage is not None else None,
        api_cached_tokens=usage.cached_tokens if usage is not None else None,
    )


def _new_context(context_id: str, *, db_path: str) -> ContextManager:
    """保留兼容入口的上下文构造注入点"""
    from . import ContextManager

    return ContextManager(context_id, db_path=db_path)


def _new_async_context(context_id: str, *, db_path: str) -> AsyncContextManager:
    """保留兼容入口的异步上下文构造注入点"""
    from . import AsyncContextManager

    return AsyncContextManager(context_id, db_path=db_path)
