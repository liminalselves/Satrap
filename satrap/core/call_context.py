"""逐次调用的可信来源, 与模型参数和持久会话状态分离"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class CallOrigin:
    """在入站边界冻结的来源身份, 不包含可继承的管理员权限"""

    adapter_id: str
    self_id: str
    chat_type: str
    chat_id: str
    actor_id: str
    source_message_id: str
    request_id: str
    actor_kind: str = "platform_user"
    route_user_id: str = ""
    conversation_kind: str = ""
    conversation_id: str = ""
    agent_route_generation: int = 0
    group_route_generation: int = 0


@dataclass
class _CallScope:
    """作用域结束后撤销所有复制上下文中的身份访问"""

    origin: CallOrigin | None
    active: bool = True


_CURRENT_CALL: ContextVar[_CallScope | None] = ContextVar("satrap_current_call", default=None)
_TOOL_WORKFLOW: ContextVar[object | None] = ContextVar("satrap_tool_workflow", default=None)


def current_tool_workflow() -> object | None:
    """
    读取正在执行工具的工作流, 区分共享工具的主 Agent 和子 Agent

    返回:
    - 工作流对象, 非模型工具调用时为 None
    """
    return _TOOL_WORKFLOW.get()


@contextmanager
def bind_tool_workflow(workflow: object) -> Iterator[None]:
    """
    在工具调用边界绑定执行者, 嵌套子 Agent 结束后恢复主工作流

    参数:
    - workflow: 实际调用工具的工作流

    返回:
    - 工具执行上下文
    """
    token = _TOOL_WORKFLOW.set(workflow)
    try:
        yield
    finally:
        _TOOL_WORKFLOW.reset(token)


def current_call_origin() -> CallOrigin | None:
    """
    读取当前执行作用域的入站来源

    返回:
    - CallOrigin | None: 无入站身份或作用域已结束时返回 None
    """
    scope = _CURRENT_CALL.get()
    return scope.origin if scope is not None and scope.active else None


def require_call_origin() -> CallOrigin:
    """
    为需要平台身份的工具取得当前来源

    返回:
    - CallOrigin: 当前可信来源, 缺失时抛出 PermissionError
    """
    origin = current_call_origin()
    if origin is None:
        raise PermissionError("当前调用没有有效的平台来源身份")
    return origin


@contextmanager
def bind_call_origin(origin: CallOrigin | None) -> Iterator[None]:
    """
    为一次会话执行绑定身份, 完成或取消后撤销

    参数:
    - origin: 入站来源, None 显式屏蔽外层身份

    返回:
    - Iterator[None]: 仅供运行时调用边界使用的上下文管理器
    """
    scope = _CallScope(origin)
    token = _CURRENT_CALL.set(scope)
    try:
        yield
    finally:
        scope.active = False
        _CURRENT_CALL.reset(token)
