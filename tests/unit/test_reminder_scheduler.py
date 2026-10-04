"""调度期限, 停用和未知结果不重试"""
from dataclasses import replace
from unittest.mock import AsyncMock
import asyncio

import pytest

from satrap.core.group_chat.reminder_scheduler import ReminderScheduler, ReminderDelivery
from satrap.core.platform.event import MessageChain
from satrap.core.platform.scheduled import execute_scheduled_segments
from satrap.core.components import Plain
from satrap.core.platform.receipt import SendReceipt
from .test_reminder_store import setup_store, create, target, SCOPE


async def drain(scheduler):
    await asyncio.gather(*list(scheduler._workers.values()))
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_disable_pauses_future_tasks_and_enable_does_not_resume(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    enabled = [False]

    async def resolve(record, prepare):
        return ReminderDelivery("ready" if enabled[0] else "paused", "reminders_disabled")

    scheduler = ReminderScheduler(lambda: [store], resolve, clock=lambda: now[0], monotonic=lambda: now[0])
    await scheduler.tick()
    assert store.get(SCOPE, reminder["reminder_id"])["reminder"]["state"] == "paused"
    enabled[0] = True
    now[0] += 10
    await scheduler.tick()
    assert not scheduler._workers and store.get(SCOPE, reminder["reminder_id"])["reminder"]["state"] == "paused"


@pytest.mark.asyncio
async def test_offline_backoff_missed_and_unknown_are_not_blindly_resent(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    sender = AsyncMock(return_value=SendReceipt("unknown", reason="timeout"))
    online = [False]

    async def send(frozen, chain, recorder):
        return await execute_scheduled_segments(frozen, recorder, [{"index": 0}], [sender])

    async def resolve(record, prepare):
        if not online[0]:
            return ReminderDelivery("waiting", "offline", grace=600)
        return ReminderDelivery("ready", target=target(record), chain=MessageChain([Plain("提醒")]), send=send)

    scheduler = ReminderScheduler(lambda: [store], resolve, clock=lambda: now[0], monotonic=lambda: now[0])
    await scheduler.tick()
    now[0] += 10
    await scheduler.tick()
    await drain(scheduler)
    waiting = store.get(SCOPE, reminder["reminder_id"])["reminder"]
    assert waiting["state"] == "waiting_delivery" and waiting["retry_at"] == now[0] + 30
    sender.assert_not_awaited()
    online[0] = True
    now[0] += 30
    await scheduler.tick()
    await drain(scheduler)
    assert store.get(SCOPE, reminder["reminder_id"])["reminder"]["state"] == "unknown"
    now[0] += 300
    await scheduler.tick()
    sender.assert_awaited_once()
    late = create(store, "late")
    online[0] = False
    now[0] += 611
    await scheduler.tick()
    await drain(scheduler)
    assert store.get(SCOPE, late["reminder_id"])["reminder"]["state"] == "missed"


@pytest.mark.asyncio
async def test_clock_jump_skips_claim_until_stable_then_checks_original_deadline(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    monotonic = [0.0]

    async def resolve(record, prepare):
        return ReminderDelivery("waiting", "offline", grace=600)

    scheduler = ReminderScheduler(lambda: [store], resolve, clock=lambda: now[0], monotonic=lambda: monotonic[0])
    await scheduler.tick()
    now[0] += 1000
    monotonic[0] += 5
    await scheduler.tick()
    assert scheduler._clock_unstable and not scheduler._workers
    now[0] += 5
    monotonic[0] += 5
    await scheduler.tick()
    await drain(scheduler)
    assert store.get(SCOPE, reminder["reminder_id"])["reminder"]["state"] == "missed"
