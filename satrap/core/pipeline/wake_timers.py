"""空窗补偿计时, 到期仅向已有平台队列提交轻量事件"""
from dataclasses import dataclass
from weakref import WeakKeyDictionary
from time import monotonic
from copy import copy, deepcopy
import asyncio

from satrap.core.pipeline.wake_window import WakeWindow, PendingMessage
from satrap.core.config.platform_policy import policy_default
from satrap.core.platform.event import MessageEvent
from satrap.core.components import Plain
from satrap.core.platform import PlatformAdapter
from satrap.core.log import logger

MIN_RESCHEDULE_DELAY = 0.5
"""复查重排的最小延迟秒数, 防止到期时间已过导致零延迟忙循环"""


@dataclass
class DeadlineTicket:
    """排队后的补偿请求可在配置变更时撤销"""

    snapshot: tuple[PendingMessage, ...] = ()
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

    def schedule(self, event: MessageEvent, *, earliest: float | None = None) -> None:
        """
        按最早消息的到期时间安排一次复查, 不持有附件资源

        参数:
        - event: 已通过权限检查的原事件
        - earliest: 到期时间下限 (monotonic), 到期复查未触发时由调用方传入剩余冷却
        """
        wait = float(event.policy_settings.get("wake_max_wait", policy_default("wake_max_wait")))
        if wait <= 0 or event.policy_settings.get("wake_mode") not in {"frequency", "necessity"} or not isinstance(event.adapter, PlatformAdapter):
            return
        key = self.window.key(event)
        adapter = event.adapter
        snapshot = self.window.peek(event)
        if not snapshot or key in self.tasks or len(self.tasks) >= self.window.max_routes:
            return
        message = copy(event.platform_message)
        message.raw_message = {}
        message.message = [Plain(text=snapshot[-1].preview)]
        message.message_str = snapshot[-1].preview
        message.sender = deepcopy(message.sender)
        message.group = deepcopy(message.group)
        lightweight = MessageEvent(message.message_str, message, event.platform_meta, event.session_id,
                                   event.adapter, event.session_provider, event.session_type)
        lightweight.policy_settings = deepcopy(event.policy_settings)
        lightweight._call_origin = event.call_origin
        ticket = DeadlineTicket()
        self.tickets[lightweight] = ticket
        due = snapshot[0].received_at + wait
        if earliest is not None:
            due = max(due, earliest, monotonic() + MIN_RESCHEDULE_DELAY)

        async def enqueue() -> None:
            """到期后回到平台队列, 不直接调用模型"""
            try:
                await asyncio.sleep(max(0, due - monotonic()))
                ticket.snapshot = self.window.peek(lightweight)
                if ticket.snapshot and not ticket.cancelled and adapter.config.enable:
                    adapter.commit_event(lightweight)
                else:
                    logger.debug(f"[WakeTimers] 到期复查跳过 adapter={adapter.config.id} cancelled={ticket.cancelled} pending={len(ticket.snapshot)}")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(f"[WakeTimers] 到期复查提交失败 adapter={adapter.config.id}: {type(error).__name__}: {error}")
            finally:
                if self.tasks.get(key) is asyncio.current_task():
                    self.tasks.pop(key, None)

        self.tasks[key] = asyncio.create_task(enqueue())

    def cancel_route(self, event: MessageEvent) -> None:
        """
        已提交窗口后取消同路由计时

        参数:
        - event: 已消费消息的事件
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
