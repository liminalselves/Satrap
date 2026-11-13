"""会话路由、历史窗口和单次调用来源契约"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
import hashlib
import json
import math


@dataclass(frozen=True)
class ConversationRoute:
    """一次平台请求的不可变路由, 共享群不归属于任一成员"""

    user_id: str
    """请求发起者的平台身份, 共享群或共享对话的路由键不纳入此身份"""
    platform: str
    """路由使用的平台适配器标识, 用于隔离不同平台的会话"""
    session_type: str
    """绑定的命名会话配置, 与 provider 共同决定会话实现"""
    provider: str = "session_class"
    """提供会话的 Provider 名称, 默认使用传统 session_class"""
    scope: str = "legacy_user"
    """上下文隔离范围, 默认 legacy_user, 也可按群成员, 群, 私聊或对话隔离"""
    self_id: str = ""
    """机器人账号标识, 默认空, 隔离路由必须提供"""
    group_id: str = ""
    """群标识, 默认空, 非版本化的群隔离路由必须提供"""
    generation: int = 0
    """群路由代次, 默认 0 表示无代次, 大于 0 时参与隔离键且不得为负"""
    conversation_kind: str = ""
    """实际对话类型, 默认空, 版本化绑定路由必须提供"""
    conversation_id: str = ""
    """实际对话标识, 默认空, 版本化绑定路由必须提供"""
    binding_generation: int = 0
    """Agent 对话绑定代次, 默认 0 表示旧路由, 大于 0 时使用 v2 隔离键"""

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
    """所属会话的非空标识, 与起点共同构成分页和快照指纹"""
    started_at: float
    """会话档案接收时间下界, Unix 秒数, 必须为有限正数且包含边界"""

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


@dataclass(frozen=True)
class CallOrigin:
    """在入站边界冻结的来源身份, 不包含可继承的管理员权限"""

    adapter_id: str
    """请求进入的平台适配器实例标识, 在入站边界冻结"""
    self_id: str
    """处理请求的机器人账号标识, 非消息入口可能为空"""
    chat_type: str
    """来源平台事件类型, 用于区分群消息, 私聊及其他入口"""
    chat_id: str
    """来源群或私聊对端标识, 缺少实际对话信息时由事件身份回退"""
    actor_id: str
    """触发本次调用的实际操作者标识, 系统入口可能为空"""
    source_message_id: str
    """来源平台消息标识, 无消息来源时为空字符串"""
    request_id: str
    """本次入站请求的关联标识, 非消息入口可能为空"""
    actor_kind: str = "platform_user"
    """操作者类别, 默认 platform_user, 不等同于管理员权限"""
    route_user_id: str = ""
    """会话路由使用的用户身份, 默认空表示未单独指定"""
    conversation_kind: str = ""
    """冻结的实际对话类型, 默认空表示没有版本化对话身份"""
    conversation_id: str = ""
    """冻结的实际对话标识, 默认空表示没有版本化对话身份"""
    agent_route_generation: int = 0
    """入站时 Agent 对话绑定代次, 默认 0 表示未使用版本化绑定"""
    group_route_generation: int = 0
    """入站时群路由代次, 默认 0 表示未使用版本化群路由"""
    group_role: str = ""
    """支持的平台群消息提供的发送者角色快照, 默认空表示未知或不适用"""
