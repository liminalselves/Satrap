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


class ModelActionAuthorizationError(PermissionError):
    """执行前无法确认原模型工具仍获授权"""


_CURRENT: ContextVar[ModelActionAuthorization | None] = ContextVar(
    "satrap_model_group_action_authorization", default=None,
)


def current_model_action_authorization() -> ModelActionAuthorization | None:
    """取得当前模型工具交给管理服务的授权来源"""
    return _CURRENT.get()


@contextmanager
def bind_model_action_authorization(source: ModelActionAuthorization) -> Iterator[None]:
    """仅在工具提交动作的调用范围内传递可信来源"""
    token = _CURRENT.set(source)
    try:
        yield
    finally:
        _CURRENT.reset(token)
