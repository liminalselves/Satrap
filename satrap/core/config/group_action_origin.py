"""模型群动作提交期间的可信授权来源"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelActionAuthorization:
    """保存最少来源身份和读取当前工具权限的回调"""

    identity: Mapping[str, str]
    verify: Callable[[str], None]
    source_session: Callable[[], object | None] | None = None
    approval_required: bool = False
    administrator_invites_auto_approve: bool = False
    approval_exempt_inviter: str = ""
    approval_exempt_until: float = 0.0
    platform_group_role: str = ""
    refresh: Callable[[str], Awaitable[None]] | None = None


class ModelActionAuthorizationError(PermissionError):
    """执行前无法确认原模型工具仍获授权"""


_CURRENT: ContextVar[ModelActionAuthorization | None] = ContextVar(
    "satrap_model_group_action_authorization", default=None,
)
ActionPreflight = Callable[[], Awaitable[None] | None]
_PREFLIGHT: ContextVar[ActionPreflight | None] = ContextVar(
    "satrap_group_action_preflight", default=None,
)
_REQUEST_OCCUPANCY: ContextVar[tuple[str, str, str, str, str] | None] = ContextVar(
    "satrap_group_request_occupancy", default=None,
)


def current_model_action_authorization() -> ModelActionAuthorization | None:
    """取得当前模型工具交给管理服务的授权来源"""
    return _CURRENT.get()


def current_group_action_preflight() -> ActionPreflight | None:
    """
    取得平台写调用前的同步或异步授权复核

    返回:
    - 当前复核回调或 None, 调用结果为可等待对象时必须等待完成
    """
    return _PREFLIGHT.get()


def current_group_request_occupancy() -> tuple[str, str, str, str, str] | None:
    """取得当前执行路径成功占用的群请求标识摘要"""
    return _REQUEST_OCCUPANCY.get()


@contextmanager
def bind_model_action_authorization(source: ModelActionAuthorization) -> Iterator[None]:
    """仅在工具提交动作的调用范围内传递可信来源"""
    token = _CURRENT.set(source)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def bind_group_action_preflight(preflight: ActionPreflight | None) -> Iterator[None]:
    """
    仅在当前管理动作的网络调用中传递最终复核

    参数:
    - preflight: 同步或异步复核回调, None 仅在回源查询期间暂时清除

    返回:
    - 退出后恢复原复核回调的作用域
    """
    token = _PREFLIGHT.set(preflight)
    try:
        yield
    finally:
        _PREFLIGHT.reset(token)


@contextmanager
def bind_group_request_occupancy(
    adapter_id: str, self_id: str, group_id: str, sub_type: str, identity_digest: str,
) -> Iterator[None]:
    """
    保留成功占用的一次群请求身份, 原始处理凭据不进入上下文

    参数:
    - adapter_id: 固定平台实例
    - self_id: 固定机器人账号
    - group_id: 固定目标群
    - sub_type: add 或 invite
    - identity_digest: 固定申请摘要, 新版包含原始事件时间, 旧版沿用凭据摘要

    返回:
    - 当前执行路径的占用身份作用域, 退出后撤销
    """
    token = _REQUEST_OCCUPANCY.set((adapter_id, self_id, group_id, sub_type, identity_digest))
    try:
        yield
    finally:
        _REQUEST_OCCUPANCY.reset(token)
