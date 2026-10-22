"""平台会话范围与持久化路由标识, 独立于当前操作者权限"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator, Mapping
import math


@dataclass(frozen=True)
class ConversationRoute:
    """一次平台请求的不可变路由, 共享群不归属于任一成员"""

    user_id: str
    platform: str
    session_type: str
    provider: str = "session_class"
    scope: str = "legacy_user"
    self_id: str = ""
    group_id: str = ""
    generation: int = 0
    conversation_kind: str = ""
    conversation_id: str = ""
    binding_generation: int = 0

    def __post_init__(self) -> None:
        """拒绝未知范围及缺少账号或群身份的隔离路由"""
        if self.scope not in {"legacy_user", "group_member", "group", "private", "conversation"}:
            raise ValueError("context_scope 无效")
        if self.binding_generation > 0 and (not self.self_id or not self.conversation_kind or not self.conversation_id):
            raise ValueError("隔离路由需要账号, 对话类型和对话 ID")
        if not self.binding_generation and (self.scope != "legacy_user" or self.generation > 0) and (not self.self_id or not self.group_id):
            raise ValueError("群上下文隔离需要机器人账号和群 ID")
        if self.generation < 0 or self.binding_generation < 0:
            raise ValueError("路由代次不能为负数")
        if self.scope in {"private", "conversation"} and not self.binding_generation:
            raise ValueError("对话范围需要版本化的隔离路由")

    @cached_property
    def key(self) -> str | None:
        """返回独立命名空间的上下文键, 旧范围返回 None 以保留旧键; 路由不可变, 结果按实例缓存"""
        if self.scope == "legacy_user" and self.generation == 0 and not self.binding_generation:
            return None
        parts: list[object] = [
            self.platform, self.provider, self.session_type, self.scope, self.self_id,
            self.group_id, self.user_id if self.scope not in {"group", "conversation"} else "",
        ]
        if self.binding_generation:
            parts.extend([self.generation, self.conversation_kind, self.conversation_id, self.binding_generation])
            return "scoped:v2:" + json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
        if self.generation:
            parts.append(self.generation)
        return "scoped:v1:" + json.dumps(
            parts,
            ensure_ascii=True, separators=(",", ":"),
        )

    @cached_property
    def context_value(self) -> str:
        """返回供 Provider 创建会话的稳定标识, 不使用成员身份冒充共享群"""
        key = self.key
        return self.user_id if key is None else "scope_" + hashlib.sha256(key.encode("utf-8")).hexdigest()

    @cached_property
    def lock_key(self) -> str:
        """返回会话切换前后保持不变的路由锁键, 包含旧用户范围的完整身份"""
        return self.key or "legacy:v1:" + json.dumps([self.platform, self.provider, self.session_type, self.user_id], separators=(",", ":"))

    @property
    def owner(self) -> str:
        """返回存储归属, 共享群留空而不绑定首位发言者"""
        return "" if self.scope in {"group", "conversation"} else self.user_id


@dataclass(frozen=True)
class HistoryWindow:
    """宿主提供的会话档案下界, 不接受模型参数覆盖"""

    session_id: str
    started_at: float

    def __post_init__(self) -> None:
        """拒绝缺失身份、非有限时间或未知会话边界"""
        if not isinstance(self.session_id, str) or not self.session_id.strip() or type(self.started_at) not in {int, float} or not math.isfinite(self.started_at) or self.started_at <= 0:
            raise ValueError("会话档案边界无效")

    @property
    def key(self) -> str:
        """返回绑定会话身份和起点的分页及快照指纹"""
        return json.dumps([self.session_id, self.started_at], separators=(",", ":"))

    def sql(self, prefix: str = "") -> tuple[str, list[object]]:
        """
        生成数据库下推筛选, 排除回源取得的历史记录

        参数:
        - prefix: 内部查询的表别名, 仅接受空值或 m.

        返回:
        - 固定 SQL 条件及绑定参数
        """
        if prefix not in {"", "m."}:
            raise ValueError("历史查询表别名无效")
        return f"typeof({prefix}received_at) IN ('integer','real') AND {prefix}received_at>=? AND {prefix}source<>'adapter_lookup'", [self.started_at]

    def contains(self, item: Mapping[str, object]) -> bool:
        """
        判断单条已核验档案是否属于本次会话

        参数:
        - item: 档案原始结果, 不使用模型提供的时间

        返回:
        - 接收时间明确且位于会话内时为 True
        """
        received = item.get("received_at")
        return isinstance(received, (int, float)) and not isinstance(received, bool) and math.isfinite(received) and received >= self.started_at and item.get("source") != "adapter_lookup"


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
