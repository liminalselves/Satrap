"""宿主拥有的一次性提醒调度器, 平台接入通过发送与权限协议扩展"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal
import asyncio
import time
import traceback

from satrap.core.group_chat.reminders import ReminderStore, ReminderRecorder
from satrap.core.platform.event import MessageChain
from satrap.core.platform.receipt import SendReceipt
from satrap.core.platform.scheduled import ScheduledTarget, ScheduledRecorder
from satrap.core.log import logger


@dataclass(frozen=True)
class ReminderDelivery:
    """本次配置检查结论, ready 才提供重新构建的后台发送目标"""

    state: Literal["ready", "waiting", "paused"]
    reason: str = ""
    grace: int = 600
    target: ScheduledTarget | None = None
    chain: MessageChain | None = None
    send: Callable[[ScheduledTarget, MessageChain, ScheduledRecorder], Awaitable[SendReceipt]] | None = None


class ReminderScheduler:
    """独立隔离每个任务失败, 只重试未提交的离线任务"""

    def __init__(
        self, stores: Callable[[], Iterable[ReminderStore]],
        resolve: Callable[[dict[str, Any], bool], Awaitable[ReminderDelivery]], *,
        interval: float = 5, concurrency: int = 8,
        clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        绑定宿主存储发现与当前权限解析入口

        参数:
        - stores: 从真实平台数据库发现存储, 包括已移除平台的保留任务
        - resolve: 每轮配置检查, 第二个参数为是否需要成员核验与发送准备
        - interval: UTC 兜底扫描间隔, 默认 5 秒
        - concurrency: 同时处理的到期任务上限
        - clock: UTC 时钟
        - monotonic: 等待与时钟跳变比较使用的单调时钟
        """
        if not 0 < interval <= 60 or type(concurrency) is not int or not 1 <= concurrency <= 32:
            raise ValueError("提醒扫描间隔或并发数无效")
        self.stores = stores
        self.resolve = resolve
        self.interval = interval
        self.concurrency = concurrency
        self.clock = clock
        self.monotonic = monotonic
        self._task: asyncio.Task[None] | None = None
        self._workers: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._recovered: set[str] = set()
        self._last_wall: float | None = None
        self._last_monotonic: float | None = None
        self._clock_unstable = False
        self._stopping = False
        self._last_cleanup: dict[str, float] = {}
        self._next_delay = interval

    def start(self) -> None:
        """在宿主事件循环启动唯一维护任务"""
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="group-chat-reminders")

    async def stop(self) -> None:
        """停止新领取, 有限等待已发送的回执, 未确认发送不重启"""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        workers = list(self._workers.values())
        if workers:
            _, pending = await asyncio.wait(workers, timeout=5)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=3)
                for task in pending:
                    if not task.done():
                        logger.error("[提醒调度] 停止后仍有未确认发送, 下次启动按未知结果恢复")

    async def _run(self) -> None:
        """在单调时钟等待期间周期检查截止时间与配置"""
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[提醒调度] 维护轮次失败, 普通群聊继续运行" + "\n" + traceback.format_exc())
            await asyncio.sleep(self._next_delay)

    def _clock_stable(self) -> bool:
        """
        识别明显 UTC 跳变, 连续一个扫描周期稳定后重新评估期限

        返回:
        - 当前是否允许领取任务, 不改变任何任务原截止时间
        """
        wall, monotonic = self.clock(), self.monotonic()
        delta = 0.0 if self._last_wall is None or self._last_monotonic is None else abs(
            (wall - self._last_wall) - (monotonic - self._last_monotonic),
        )
        self._last_wall, self._last_monotonic = wall, monotonic
        if delta > 30:
            self._clock_unstable = True
            logger.warning(f"[提醒调度] clock_unstable, UTC 跳变={delta:.1f} 秒, 暂停领取")
            return False
        if self._clock_unstable:
            self._clock_unstable = False
            logger.info("[提醒调度] UTC 时钟重新稳定, 按原期限评估补发宽限")
        return True

    async def tick(self) -> None:
        """恢复证据, 暂停已撤销授权的任务, 有界领取到期任务"""
        if self._stopping:
            return
        stable = self._clock_stable()
        self._next_delay = self.interval
        stores = await asyncio.to_thread(lambda: list(self.stores()))
        for store in stores:
            key = str(store.database.resolve())
            try:
                if key not in self._recovered:
                    count = await asyncio.to_thread(store.recover)
                    self._recovered.add(key)
                    if count:
                        logger.warning(f"[提醒调度] 崩溃恢复标记未知任务={count}, 数据库={key}")
                if self.clock() - self._last_cleanup.get(key, 0) >= 3600:
                    await asyncio.to_thread(store.cleanup)
                    self._last_cleanup[key] = self.clock()
                active = await asyncio.to_thread(store.active)
                for reminder in active:
                    worker_key = (key, reminder["reminder_id"])
                    if worker_key in self._workers:
                        continue
                    try:
                        decision = await self.resolve(reminder, False)
                        if decision.state == "paused":
                            await asyncio.to_thread(store.transition, reminder["reminder_id"], ("scheduled", "waiting_delivery"), "paused", decision.reason)
                            continue
                        if not stable or self._stopping or len(self._workers) >= self.concurrency:
                            continue
                        ready_at = max(reminder["due_timestamp"], reminder["retry_at"] or reminder["due_timestamp"])
                        if ready_at > self.clock():
                            self._next_delay = min(self._next_delay, max(0.01, ready_at - self.clock()))
                            continue
                        task = asyncio.create_task(self._deliver(store, reminder), name="reminder:" + reminder["reminder_id"])
                        self._workers[worker_key] = task
                        task.add_done_callback(lambda finished, identity=worker_key: self._settled(identity, finished))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.error(f"[提醒调度] 任务权限检查失败, 任务={reminder['reminder_id']}" + "\n" + traceback.format_exc())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"[提醒调度] 存储不可用, 停止该库提醒发送, 数据库={key}" + "\n" + traceback.format_exc())

    def _settled(self, identity: tuple[str, str], task: asyncio.Task[None]) -> None:
        """
        仅在任务实际结束时释放并发名额并消费异常

        参数:
        - identity: 数据库和任务 ID
        - task: 真实发送 worker
        """
        self._workers.pop(identity, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error(f"[提醒调度] worker 异常, 任务={identity[1]}, 错误={task.exception()}")

    async def _wait_delivery(self, store: ReminderStore, reminder: dict[str, Any], decision: ReminderDelivery) -> None:
        """
        对尚未提交的暂时离线按期限退避

        参数:
        - store: 当前任务数据库
        - reminder: 当前任务
        - decision: 当前宽限与离线原因
        """
        deadline = reminder["due_timestamp"] + decision.grace
        if self.clock() > deadline:
            await asyncio.to_thread(store.transition, reminder["reminder_id"], ("scheduled", "waiting_delivery"), "missed", "catchup_expired")
            return
        delay = (30, 120, 300)[min(reminder["retry_count"], 2)]
        retry_at = min(self.clock() + delay, deadline)
        await asyncio.to_thread(store.transition, reminder["reminder_id"], ("scheduled", "waiting_delivery"), "waiting_delivery", decision.reason, retry_at)

    async def _deliver(self, store: ReminderStore, reminder: dict[str, Any]) -> None:
        """
        到期重新核验成员和当前配置后尝试一次发送

        参数:
        - store: 当前平台的任务数据库
        - reminder: 本轮读取的任务, 原事件已经结束
        """
        try:
            decision = await self.resolve(reminder, True)
            if decision.state == "paused":
                await asyncio.to_thread(store.transition, reminder["reminder_id"], ("scheduled", "waiting_delivery"), "paused", decision.reason)
                return
            if self.clock() > reminder["due_timestamp"] + decision.grace:
                await asyncio.to_thread(store.transition, reminder["reminder_id"], ("scheduled", "waiting_delivery"), "missed", "catchup_expired")
                return
            if decision.state == "waiting":
                await self._wait_delivery(store, reminder, decision)
                return
            if decision.target is None or decision.chain is None or decision.send is None:
                raise ValueError("发送准备缺少可信目标或平台能力")
            if self._stopping:
                return
            recorder = ReminderRecorder(store, reminder, decision.target.attempt_id)
            receipt = await decision.send(decision.target, decision.chain, recorder)
            current = await asyncio.to_thread(store.get, recorder.scope, recorder.identity)
            if current["reminder"]["state"] in {"scheduled", "waiting_delivery"}:
                if receipt.reason == "send_queue_unavailable":
                    await self._wait_delivery(store, reminder, ReminderDelivery("waiting", receipt.reason, decision.grace))
                elif receipt.reason == "scheduled_target_changed":
                    await asyncio.to_thread(store.transition, recorder.identity, ("scheduled", "waiting_delivery"), "paused", receipt.reason)
                elif receipt.reason != "scheduled_claim_unavailable":
                    await asyncio.to_thread(store.transition, recorder.identity, ("scheduled", "waiting_delivery"), "failed", receipt.reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(f"[提醒调度] 单任务失败, 任务={reminder['reminder_id']}" + "\n" + traceback.format_exc())
