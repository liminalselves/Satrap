"""
逐次调用的可信来源与工具执行证据

入站身份和待提交的可见消息通过可撤销作用域传递, 不进入持久会话状态,
嵌套子代理可以定位主代理证据, 不把模型参数或隐藏思考当成用户授权
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator, Mapping, Sequence
import copy
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
    group_role: str = ""


@dataclass
class _CallScope:
    """作用域结束后撤销所有复制上下文中的身份访问"""

    origin: CallOrigin | None
    active: bool = True


_CURRENT_CALL: ContextVar[_CallScope | None] = ContextVar("satrap_current_call", default=None)


@dataclass
class _ToolScope:
    """当前工具执行的可撤销工作流与可见待提交消息, 父作用域用于嵌套子代理"""

    workflow: object
    messages: tuple[dict[str, object], ...]
    parent: _ToolScope | None
    active: bool = True


_TOOL_WORKFLOW: ContextVar[_ToolScope | None] = ContextVar("satrap_tool_workflow", default=None)


def current_tool_workflow() -> object | None:
    """
    读取正在执行工具的工作流, 区分共享工具的主 Agent 和子 Agent

    返回:
    - 工作流对象, 非模型工具调用时为 None
    """
    scope = _TOOL_WORKFLOW.get()
    return scope.workflow if scope is not None and scope.active else None


def current_tool_messages(workflow: object) -> list[dict[str, object]]:
    """
    读取主代理或嵌套子代理当前尚未提交的可见消息

    参数:
    - workflow: 宿主已知的实际工作流对象

    返回:
    - 仍有效的匹配作用域消息副本, 不包含隐藏思考; 未绑定或已结束返回空列表
    """
    scope = _TOOL_WORKFLOW.get()
    while scope is not None and scope.active:
        if scope.workflow is workflow:
            return copy.deepcopy(list(scope.messages))
        scope = scope.parent
    return []


@contextmanager
def bind_tool_workflow(workflow: object, messages: Sequence[Mapping[str, object]] = ()) -> Iterator[None]:
    """
    在工具调用边界绑定执行者, 嵌套子 Agent 结束后恢复主工作流

    参数:
    - workflow: 实际调用工具的工作流
    - messages: 本轮待提交消息, 默认空序列; 仅冻结可见角色, 正文和工具证据

    返回:
    - 工具执行上下文
    """
    visible = tuple({key: copy.deepcopy(row[key]) for key in ("role", "content", "tool_calls", "tool_call_id", "name")
                     if key in row} for row in messages)
    scope = _ToolScope(workflow, visible, _TOOL_WORKFLOW.get())
    token = _TOOL_WORKFLOW.set(scope)
    try:
        yield
    finally:
        scope.active = False
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


def is_group_origin(origin: CallOrigin | None) -> bool:
    """
    判断来源是否属于群聊, conversation_kind 优先, 为空时才回退到 chat_type

    参数:
    - origin: 入站来源, None 视为非群聊

    返回:
    - 来源为群聊时返回 True
    """
    if origin is None:
        return False
    return origin.conversation_kind == "group" or not origin.conversation_kind and origin.chat_type == "GroupMessage"


def is_private_origin(origin: CallOrigin | None) -> bool:
    """
    判断来源是否属于私聊, conversation_kind 优先, 为空时才回退到 chat_type

    参数:
    - origin: 入站来源, None 视为非私聊

    返回:
    - 来源为私聊时返回 True
    """
    if origin is None:
        return False
    return origin.conversation_kind == "private" or not origin.conversation_kind and origin.chat_type == "FriendMessage"
