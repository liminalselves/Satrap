"""空窗补偿计时, 到期仅向已有平台队列提交轻量事件"""
from dataclasses import dataclass
from weakref import WeakKeyDictionary
from time import monotonic
from copy import copy, deepcopy
import asyncio

from satrap.core.pipeline.wake_window import WakeWindow, PendingText
from satrap.core.platform.event import MessageEvent
from satrap.core.components import Plain
from satrap.core.platform import PlatformAdapter


@dataclass
class DeadlineTicket:
    """排队后的补偿请求可在配置变更时撤销"""

    snapshot: tuple[PendingText, ...] = ()
    cancelled: bool = False


class WakeTimers:
    """每路由最多一个计时任务, 使用窗口容量限制全局数量"""

    def __init__(self, window: WakeWindow):
        """
        初始化窗口计时器

        参数:
        - window: 调度器拥有的待处理窗口
        """
        self.window = window
        self.tasks: dict[tuple[str, ...], asyncio.Task[None]] = {}
        self.tickets: WeakKeyDictionary[MessageEvent, DeadlineTicket] = WeakKeyDictionary()

    def schedule(self, event: MessageEvent) -> None:
        """
        按最早正文的到期时间安排一次复查, 不持有附件资源

        参数:
        - event: 已通过权限检查的原事件
        """
        wait = float(event.policy_settings.get("wake_max_wait", 0))
        if wait <= 0 or event.policy_settings.get("wake_mode") not in {"frequency", "necessity"} or not isinstance(event.adapter, PlatformAdapter):
            return
        key = self.window.key(event)
        adapter = event.adapter
        snapshot = self.window.peek(event)
        if not snapshot or key in self.tasks or len(self.tasks) >= self.window.max_routes:
            return
        message = copy(event.platform_message)
        message.raw_message = {}
        message.message = [Plain(text=snapshot[-1].text)]
        message.message_str = snapshot[-1].text
        message.sender = deepcopy(message.sender)
        message.group = deepcopy(message.group)
        lightweight = MessageEvent(message.message_str, message, event.platform_meta, event.session_id,
                                   event.adapter, event.session_provider, event.session_type)
        lightweight.policy_settings = deepcopy(event.policy_settings)
        lightweight._call_origin = event.call_origin
        ticket = DeadlineTicket()
        self.tickets[lightweight] = ticket
        due = snapshot[0].received_at + wait

        async def enqueue() -> None:
            """到期后回到平台队列, 不直接调用模型"""
            try:
                await asyncio.sleep(max(0, due - monotonic()))
                ticket.snapshot = self.window.peek(lightweight)
                if ticket.snapshot and not ticket.cancelled and adapter.config.enable:
                    adapter.commit_event(lightweight)
            finally:
                if self.tasks.get(key) is asyncio.current_task():
                    self.tasks.pop(key, None)

        self.tasks[key] = asyncio.create_task(enqueue())

    def cancel_route(self, event: MessageEvent) -> None:
        """
        已提交窗口后取消同路由计时

        参数:
        - event: 已消费正文的事件
        """
        task = self.tasks.pop(self.window.key(event), None)
        if task is not None:
            task.cancel()

    def clear_adapter(self, adapter_id: str) -> None:
        """
        撤销平台计时与已排队补偿请求

        参数:
        - adapter_id: 平台实例 ID
        """
        for key in list(self.tasks):
            if key[0] == adapter_id:
                self.tasks.pop(key).cancel()
        for event, ticket in list(self.tickets.items()):
            if event.call_origin.adapter_id == adapter_id:
                ticket.cancelled = True

    async def close(self) -> None:
        """取消并等待所有剩余计时, 使排队补偿失效"""
        tasks = list(self.tasks.values())
        self.tasks.clear()
        for task in tasks:
            task.cancel()
        for ticket in self.tickets.values():
            ticket.cancelled = True
        await asyncio.gather(*tasks, return_exceptions=True)
