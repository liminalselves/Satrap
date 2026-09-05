"""
状态变更审计上下文

基于 ContextVar 在作用域内提供统一的变更来源与原因,
StateStore 创建检查点时自动记录当前上下文, 用于追踪每次状态变更的来源
"""
from contextvars import ContextVar, Token
from contextlib import contextmanager
from typing import Generator, Optional, Union
import uuid

from satrap.core.type import MutationContext

_MUTATION_CONTEXT: ContextVar[Optional[MutationContext]] = ContextVar(
    "satrap_state_mutation_context",
    default=None,
)


def current_mutation_context() -> Optional[MutationContext]:
    """
    读取当前状态变更上下文, 无上下文时返回 None

    返回:
    - Optional[MutationContext]: 读取当前状态变更上下文, 无上下文时返回 None
    """
    return _MUTATION_CONTEXT.get()


@contextmanager
def state_mutation_context(*, source: str, reason: str = "") -> Generator[MutationContext, None, None]:
    """
    在作用域内提供状态变更审计上下文

    参数:
    - source: 变更来源, 如 "checkpoint_rollback" / "checkpoint_fork"
    - reason: 变更原因说明

    返回:
    - Generator[MutationContext, None, None]: 在作用域内提供状态变更审计上下文
    """
    context = MutationContext(
        source=source.strip() or "manual",
        reason=reason.strip(),
        change_set_id=uuid.uuid4().hex,
    )
    token: Token[Optional[MutationContext]] = _MUTATION_CONTEXT.set(context)
    try:
        yield context
    finally:
        _MUTATION_CONTEXT.reset(token)
