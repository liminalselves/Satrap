"""待处理正文窗口的隔离, 容量和消费行为"""
from unittest.mock import AsyncMock
import asyncio

import pytest

from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.pipeline.wake_window import WakeWindow
from satrap.core.platform import PlatformConfig
from satrap.core.platform.event import MessageEvent


async def event(adapter: OneBotAdapter, message_id: str, actor: str = "30", group: str = "20", text: str = "正文") -> MessageEvent:
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
    assert next_decision.score is not None and decision.score is not None and next_decision.score < decision.score
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


@pytest.mark.asyncio
async def test_deadline_recheck_in_cooldown_reschedules_after_cooldown_not_immediately(monkeypatch: pytest.MonkeyPatch):
    """到期复查命中冷却时, 重排不早于冷却结束且有最小延迟, 不形成零延迟忙循环"""
    from satrap.core.pipeline import wake_timers as timers_module
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_message_threshold": 3, "wake_max_wait": 0.02, "wake_cooldown": 5,
    }))
    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    scheduler = PipelineScheduler(manager)
    first = await event(adapter, "1", text="第一批")
    await scheduler.execute(first)
    timed = await asyncio.wait_for(adapter._event_queue.get(), 1)
    await scheduler.execute(timed)
    manager.handle_call_async.assert_awaited_once()
    # 冷却期内新正文到达并再次到期
    second = await event(adapter, "2", text="第二批")
    await scheduler.execute(second)
    timed2 = await asyncio.wait_for(adapter._event_queue.get(), 1)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def spy_sleep(delay: float, *args: object, **kwargs: object) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(timers_module.asyncio, "sleep", spy_sleep)
    await scheduler.execute(timed2)
    assert timed2.get_extra("wake_decision").rule == "cooldown"
    await asyncio.sleep(0.01)
    assert sleeps and sleeps[-1] >= 4.5, sleeps
    assert manager.handle_call_async.await_count == 1
    await scheduler.wake_timers.close()


@pytest.mark.asyncio
async def test_deadline_recheck_not_rescheduled_when_policy_changed():
    """到期复查因策略失效未触发时不再重排"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_message_threshold": 3, "wake_max_wait": 0.02,
    }))
    manager = AsyncMock()
    scheduler = PipelineScheduler(manager)
    await scheduler.execute(await event(adapter, "1", text="正文"))
    timed = await asyncio.wait_for(adapter._event_queue.get(), 1)
    adapter.config.settings = {"wake_mode": "explicit"}
    await scheduler.execute(timed)
    manager.handle_call_async.assert_not_awaited()
    assert not scheduler.wake_timers.tasks


@pytest.mark.asyncio
async def test_timer_enqueue_failure_is_logged_not_lost(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch):
    import logging
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_message_threshold": 3, "wake_max_wait": 0.01,
    }))
    scheduler = PipelineScheduler(AsyncMock())
    original = await event(adapter, "1", text="正文")

    def broken_commit(_event: object) -> bool:
        raise RuntimeError("queue gone")

    monkeypatch.setattr(adapter, "commit_event", broken_commit)
    with caplog.at_level(logging.WARNING):
        await scheduler.execute(original)
        await asyncio.sleep(0.05)
    assert any("到期复查提交失败" in record.getMessage() and "RuntimeError" in record.getMessage() for record in caplog.records)
    assert not scheduler.wake_timers.tasks
