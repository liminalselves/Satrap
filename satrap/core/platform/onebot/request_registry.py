"""
request 事件 flag 登记: 审批动作的对象归属核验与防重放

群请求与好友请求分表登记, 键为事件上报的 flag;
校验与占用在首次网络等待之前同步完成, 状态只向终态迁移,
超时或传输异常记 unknown 且永不自动恢复可用
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic

REQUEST_FLAG_LIMIT = 512
"""每张登记表的 flag 容量, 超出淘汰最旧"""
REQUEST_FLAG_TTL = 600.0
"""flag 保留秒数, OneBot flag 本身短期有效, 过期登记一并清除"""


@dataclass
class RequestFlagEntry:
    """一条已登记请求, state 仅 available→executing→completed/unknown 单向迁移"""

    group_id: str
    sub_type: str
    user_id: str
    received_at: float
    state: str = "available"


class RequestFlagRegistry:
    """按请求类别分表的有界 flag 登记"""

    def __init__(self, limit: int = REQUEST_FLAG_LIMIT, ttl: float = REQUEST_FLAG_TTL) -> None:
        """
        初始化登记表

        参数:
        - limit: 每表最大 flag 数, 必须为正
        - ttl: flag 保留秒数, 必须为正
        """
        if limit <= 0 or ttl <= 0:
            raise ValueError("登记容量和 TTL 必须为正数")
        self.limit, self.ttl = limit, ttl
        self._tables: dict[str, OrderedDict[str, RequestFlagEntry]] = {"group": OrderedDict(), "friend": OrderedDict()}

    def _sweep(self, table: OrderedDict[str, RequestFlagEntry], now: float) -> None:
        """惰性清除过期登记"""
        expired = [flag for flag, entry in table.items() if now - entry.received_at >= self.ttl]
        for flag in expired:
            del table[flag]

    def register(self, kind: str, flag: str, *, group_id: str = "", sub_type: str = "", user_id: str = "", now: float | None = None) -> bool:
        """
        登记入站 request 事件; 重复入站不得重置已占用状态

        参数:
        - kind: group 或 friend, 分表隔离
        - flag: 事件上报的审批标识
        - group_id: 群请求的群号, 好友请求为空
        - sub_type: 群请求的 add/invite, 好友请求为空
        - user_id: 请求来源用户
        - now: 单调时钟, 默认读取当前时间

        返回:
        - bool: 新登记为 True; 同 flag 已存在时不改写状态, 仅 available 刷新时间
        """
        now = monotonic() if now is None else now
        table = self._tables[kind]
        self._sweep(table, now)
        existing = table.get(flag)
        if existing is not None:
            if existing.state == "available":
                existing.received_at = now
                table.move_to_end(flag)
            return False
        table[flag] = RequestFlagEntry(group_id=group_id, sub_type=sub_type, user_id=user_id, received_at=now)
        while len(table) > self.limit:
            table.popitem(last=False)
        return True

    def occupy(self, kind: str, flag: str, *, group_id: str = "", sub_type: str = "", now: float | None = None) -> RequestFlagEntry:
        """
        同步校验并原子置 executing, 全程无 await, 并发至多一个成功

        参数:
        - kind: group 或 friend
        - flag: 待核验标识
        - group_id: 调用方声明的群号, 必须与登记一致
        - sub_type: 调用方声明的子类型, 必须与登记一致
        - now: 单调时钟

        返回:
        - RequestFlagEntry: 已占用的登记项

        异常:
        - LookupError: flag 未登记/已过期/已占用/归属不符, message 为用户可读原因
        """
        now = monotonic() if now is None else now
        table = self._tables[kind]
        self._sweep(table, now)
        entry = table.get(flag)
        if entry is None:
            raise LookupError("请求标识未登记或已过期, 无法确认归属")
        if entry.state != "available":
            raise LookupError("请求标识已被处理或结果未知, 拒绝重复执行")
        if entry.group_id != group_id or entry.sub_type != sub_type:
            raise LookupError("请求标识归属与参数不符")
        entry.state = "executing"
        return entry

    def settle(self, kind: str, flag: str, state: str) -> None:
        """
        将 executing 登记迁移到终态

        参数:
        - kind: group 或 friend
        - flag: 待迁移标识
        - state: completed (动作已有明确结果) 或 unknown (超时/传输异常, 不可重试)
        """
        if state not in {"completed", "unknown"}:
            raise ValueError("终态必须为 completed 或 unknown")
        entry = self._tables[kind].get(flag)
        if entry is not None and entry.state == "executing":
            entry.state = state
