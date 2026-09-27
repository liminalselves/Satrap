"""平台会话范围与持久化路由标识, 独立于当前操作者权限"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
import hashlib
import json


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

    def __post_init__(self) -> None:
        """拒绝未知范围及缺少账号或群身份的隔离路由"""
        if self.scope not in {"legacy_user", "group_member", "group"}:
            raise ValueError("context_scope 必须为 legacy_user, group_member 或 group")
        if (self.scope != "legacy_user" or self.generation > 0) and (not self.self_id or not self.group_id):
            raise ValueError("群上下文隔离需要机器人账号和群 ID")
        if self.generation < 0:
            raise ValueError("路由代次不能为负数")

    @cached_property
    def key(self) -> str | None:
        """返回独立命名空间的上下文键, 旧范围返回 None 以保留旧键; 路由不可变, 结果按实例缓存"""
        if self.scope == "legacy_user" and self.generation == 0:
            return None
        parts: list[object] = [
            self.platform, self.provider, self.session_type, self.scope, self.self_id,
            self.group_id, self.user_id if self.scope != "group" else "",
        ]
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

    @property
    def owner(self) -> str:
        """返回存储归属, 共享群留空而不绑定首位发言者"""
        return "" if self.scope == "group" else self.user_id
