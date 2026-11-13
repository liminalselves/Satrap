"""平台会话路由的上下文绑定与读取"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

from satrap.core.type.conversation import ConversationRoute, HistoryWindow


@dataclass
class _RouteScope:
    """只在真实会话执行期间公开宿主路由, 退出后撤销复制上下文中的绑定"""

    route: ConversationRoute | None
    history_window: HistoryWindow | None = None
    active: bool = True


_CURRENT_ROUTE: ContextVar[_RouteScope | None] = ContextVar("satrap_conversation_route", default=None)


def current_conversation_route() -> ConversationRoute | None:
    """
    获取当前宿主执行绑定的真实路由

    返回:
    - 有效范围内的路由, 未绑定或执行结束返回 None
    """
    scope = _CURRENT_ROUTE.get()
    return scope.route if scope is not None and scope.active else None


def current_history_window() -> HistoryWindow | None:
    """
    读取本轮宿主冻结的会话档案范围

    返回:
    - 执行仍有效时的会话边界, 缺少宿主绑定时返回 None
    """
    scope = _CURRENT_ROUTE.get()
    return scope.history_window if scope is not None and scope.active else None


@contextmanager
def bind_conversation_route(route: ConversationRoute | None, history_window: HistoryWindow | None = None) -> Iterator[None]:
    """
    为同步或异步会话命令绑定可信路由

    参数:
    - route: 由宿主解析的不可变路由, 不从会话 ID 推断
    - history_window: 真实会话的持久起点, None 不提供档案范围证明

    返回:
    - 执行结束后撤销的路由上下文
    """
    scope = _RouteScope(route, history_window)
    token = _CURRENT_ROUTE.set(scope)
    try:
        yield
    finally:
        scope.active = False
        _CURRENT_ROUTE.reset(token)
