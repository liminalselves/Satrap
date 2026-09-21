"""手动唤醒的受信请求登记, 不从模型或请求正文获取操作者身份"""
from collections import OrderedDict
from dataclasses import dataclass
from weakref import WeakKeyDictionary, ReferenceType, ref
from time import monotonic
import hashlib
import json

from satrap.core.pipeline.wake_window import PendingText
from satrap.core.platform.event import MessageEvent


PENDING_TTL = 600
"""未被消费的手动请求在此秒数后视为已取消, 释放幂等容量"""


@dataclass
class ManualWakeTicket:
    """手动请求与可选待处理窗口快照"""

    request_id: str
    snapshot: tuple[PendingText, ...] = ()
    cancelled: bool = False
    status: str = "pending"
    event_ref: ReferenceType[MessageEvent] | None = None


class ManualWakeRequests:
    """有限的进程内幂等记录, 活跃请求不会因容量不足被挤出"""

    def __init__(self):
        """初始化五分钟幂等记录和受信事件登记"""
        self.records: OrderedDict[str, tuple[str, float, ManualWakeTicket]] = OrderedDict()
        self.tickets: WeakKeyDictionary[MessageEvent, ManualWakeTicket] = WeakKeyDictionary()

    @staticmethod
    def fingerprint(payload: dict[str, object], operator: str) -> str:
        """
        计算含操作者的请求摘要

        参数:
        - payload: 已校验的请求
        - operator: 服务端认证主体

        返回:
        - str: 摘要, 不返回正文
        """
        return hashlib.sha256(json.dumps([operator, payload], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def check(self, request_id: str, fingerprint: str) -> dict[str, object] | None:
        """
        检查重复请求与容量

        参数:
        - request_id: 客户端幂等标识
        - fingerprint: 含操作者的请求摘要

        返回:
        - dict | None: 拒绝/重复结果, None 表示可提交
        """
        now = monotonic()
        for key, (_, created, ticket) in list(self.records.items()):
            if ticket.status == "pending" and ticket.event_ref is not None and ticket.event_ref() is None:
                ticket.status = "cancelled"
            if ticket.status == "pending" and now - created > PENDING_TTL:
                ticket.status = "cancelled"
                ticket.cancelled = True
            if now - created > 300 and ticket.status != "pending":
                del self.records[key]
        previous = self.records.get(request_id)
        if previous:
            same = previous[0] == fingerprint
            return {"status": "already_pending" if same else "rejected", "request_id": request_id,
                    "state": previous[2].status, "reason": "duplicate" if same else "request_id_conflict"}
        if len(self.records) >= 512:
            return {"status": "rejected", "request_id": request_id, "reason": "request_capacity"}
        return None

    def register(self, event: MessageEvent, fingerprint: str, ticket: ManualWakeTicket) -> None:
        """
        仅在入队成功后登记

        参数:
        - event: 受信应用层创建的事件
        - fingerprint: 请求摘要
        - ticket: 请求状态
        """
        self.records[ticket.request_id] = (fingerprint, monotonic(), ticket)
        ticket.event_ref = ref(event)
        self.tickets[event] = ticket

    def clear_adapter(self, adapter_id: str) -> None:
        """
        撤销平台残留请求

        参数:
        - adapter_id: 平台实例 ID
        """
        for event, ticket in list(self.tickets.items()):
            if event.call_origin.adapter_id == adapter_id:
                ticket.cancelled = True
                ticket.status = "cancelled"
