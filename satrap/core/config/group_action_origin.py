"""模型群动作提交期间的可信授权来源"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
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


class ModelActionAuthorizationError(PermissionError):
    """执行前无法确认原模型工具仍获授权"""


_CURRENT: ContextVar[ModelActionAuthorization | None] = ContextVar(
    "satrap_model_group_action_authorization", default=None,
)
_PREFLIGHT: ContextVar[Callable[[], None] | None] = ContextVar(
    "satrap_group_action_preflight", default=None,
)
_REQUEST_OCCUPANCY: ContextVar[tuple[str, str, str, str, str] | None] = ContextVar(
    "satrap_group_request_occupancy", default=None,
)


def current_model_action_authorization() -> ModelActionAuthorization | None:
    """取得当前模型工具交给管理服务的授权来源"""
    return _CURRENT.get()


def current_group_action_preflight() -> Callable[[], None] | None:
    """取得平台写调用前的同步授权复核"""
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
def bind_group_action_preflight(preflight: Callable[[], None]) -> Iterator[None]:
    """仅在当前管理动作的网络调用中传递最终复核"""
    token = _PREFLIGHT.set(preflight)
    try:
        yield
    finally:
        _PREFLIGHT.reset(token)


@contextmanager
def bind_group_request_occupancy(
    adapter_id: str, self_id: str, group_id: str, sub_type: str, flag_digest: str,
) -> Iterator[None]:
    """仅在成功占用群请求的执行路径中保留占用凭据"""
    token = _REQUEST_OCCUPANCY.set((adapter_id, self_id, group_id, sub_type, flag_digest))
    try:
        yield
    finally:
        _REQUEST_OCCUPANCY.reset(token)
