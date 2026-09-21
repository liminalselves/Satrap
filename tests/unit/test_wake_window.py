"""待处理正文窗口的隔离, 容量和消费行为"""
from unittest.mock import AsyncMock
import asyncio

import pytest

from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.pipeline.wake_window import WakeWindow
from satrap.core.platform import PlatformConfig


async def event(adapter, message_id, actor="30", group="20", text="正文"):
    """从实际 OneBot 解析构造轻量入站事件"""
    await adapter._handle_group_message({"self_id": 10, "group_id": group, "user_id": actor,
        "message_id": message_id, "message_type": "group", "message": [{"type": "text", "data": {"text": text}}]})
    return adapter._event_queue.get_nowait()


@pytest.mark.asyncio
async def test_window_isolation_ttl_capacity_and_single_consumption():
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={}))
    window = WakeWindow(max_routes=2, max_messages=2, max_chars=8, ttl=10)
    first = await event(adapter, "1")
    window.observe(first, 0)
    other = await event(adapter, "2", actor="31")
    assert len(window.observe(other, 1)) == 1
    second = await event(adapter, "3")
    snapshot = window.observe(second, 2)
    assert [item.message_id for item in snapshot] == ["1", "3"]
    assert window.claim(second, snapshot, False, 3) == snapshot
    assert window.claim(second, snapshot, False, 3) == ()
    third = await event(adapter, "4", text="123456789")
    assert window.observe(third, 4)[0].text == "12345678"
    assert window.observe(await event(adapter, "5"), 15)[0].message_id == "5"
    for group in ["21", "22", "23"]:
        window.observe(await event(adapter, group, group=group), 16)
    assert len(window._pending) == 2
    window.clear_adapter("bot")
    assert not window._pending and not window._submitted


@pytest.mark.asyncio
async def test_only_explicit_shared_scope_merges_members():
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"context_scope": "group"}))
    window = WakeWindow()
    window.observe(await event(adapter, "1", actor="30"))
    rows = window.observe(await event(adapter, "2", actor="31"))
    assert [item.actor_id for item in rows] == ["30", "31"]
    assert len(window.observe(await event(adapter, "3", group="21"))) == 1


@pytest.mark.asyncio
async def test_frequency_scheduler_batches_and_cooldown_keeps_new_text():
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_message_threshold": 2, "wake_cooldown": 60, "wake_words": ["唤醒"],
    }))
    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    scheduler = PipelineScheduler(manager)
    await scheduler.execute(await event(adapter, "1", text="第一句"))
    assert manager.handle_call_async.await_count == 0
    await scheduler.execute(await event(adapter, "2", text="第二句"))
    assert manager.handle_call_async.await_count == 1
    message = manager.handle_call_async.call_args.args[0].message
    assert "第一句" in message and "第二句" in message
    await scheduler.execute(await event(adapter, "3", text="第三句"))
    await scheduler.execute(await event(adapter, "4", text="第四句"))
    assert manager.handle_call_async.await_count == 1
    await scheduler.execute(await event(adapter, "5", text="唤醒"))
    assert manager.handle_call_async.await_count == 2
    assert "第三句" in manager.handle_call_async.call_args.args[0].message
    assert "第一句" not in manager.handle_call_async.call_args.args[0].message


@pytest.mark.asyncio
async def test_necessity_score_explains_question_and_recent_submission_penalty():
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "necessity", "wake_cooldown": 0, "wake_score_threshold": 0.6,
    }))
    window = WakeWindow()
    first = await event(adapter, "1", text="怎么处理这个问题？")
    snapshot = window.observe(first, 0)
    decision = window.decide(first, snapshot, 0)
    assert decision.triggered and decision.score is not None
    assert "问题=1" in decision.reason
    assert window.claim(first, snapshot, True, 0)
    second = await event(adapter, "2", text="怎么处理另一个问题？")
    snapshot = window.observe(second, 1)
    next_decision = window.decide(second, snapshot, 1)
    assert not next_decision.triggered
    assert next_decision.score < decision.score
    assert not window.claim(second, snapshot, True, 1)
    assert window.claim(second, snapshot, False, 1)


@pytest.mark.asyncio
async def test_queued_event_cannot_restore_disabled_automatic_policy():
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_message_threshold": 1,
    }))
    queued = await event(adapter, "1")
    adapter.config.settings = {"wake_mode": "explicit"}
    manager = AsyncMock()
    scheduler = PipelineScheduler(manager)
    await scheduler.execute(queued)
    manager.handle_call_async.assert_not_awaited()
    assert not scheduler.wake_window._pending


@pytest.mark.asyncio
async def test_max_wait_enqueues_once_without_counting_or_repeating():
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_message_threshold": 3, "wake_max_wait": 0.02,
    }))
    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    scheduler = PipelineScheduler(manager)
    original = await event(adapter, "1", text="待处理正文")
    await scheduler.execute(original)
    manager.handle_call_async.assert_not_awaited()
    timed = await asyncio.wait_for(adapter._event_queue.get(), 1)
    assert timed is not original
    assert timed.platform_message.raw_message == {}
    assert timed.call_origin == original.call_origin
    assert len(scheduler.wake_window.peek(timed)) == 1
    await scheduler.execute(timed)
    manager.handle_call_async.assert_awaited_once()
    assert "待处理正文" in manager.handle_call_async.call_args.args[0].message
    await asyncio.sleep(0.05)
    assert adapter._event_queue.empty()
    assert not scheduler.wake_timers.tasks
    assert not scheduler.wake_window.peek(timed)
    await scheduler.wake_timers.close()


@pytest.mark.asyncio
async def test_max_wait_applies_to_necessity_mode():
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "necessity", "wake_score_threshold": 0.99, "wake_max_wait": 0.02,
    }))
    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    scheduler = PipelineScheduler(manager)
    original = await event(adapter, "1", text="随便聊聊")
    await scheduler.execute(original)
    manager.handle_call_async.assert_not_awaited()
    timed = await asyncio.wait_for(adapter._event_queue.get(), 1)
    await scheduler.execute(timed)
    manager.handle_call_async.assert_awaited_once()
    assert timed.get_extra("wake_decision").rule == "max_wait"
    await scheduler.wake_timers.close()


@pytest.mark.asyncio
async def test_max_wait_cancelled_on_close_and_queued_ticket_revoked():
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_max_wait": 0.02,
    }))
    manager = AsyncMock()
    scheduler = PipelineScheduler(manager)
    await scheduler.execute(await event(adapter, "1"))
    timed = await asyncio.wait_for(adapter._event_queue.get(), 1)
    scheduler.wake_timers.clear_adapter("bot")
    await scheduler.execute(timed)
    manager.handle_call_async.assert_not_awaited()
    await scheduler.execute(await event(adapter, "2"))
    await scheduler.wake_timers.close()
    await asyncio.sleep(0.05)
    assert adapter._event_queue.empty()
    assert not scheduler.wake_timers.tasks
