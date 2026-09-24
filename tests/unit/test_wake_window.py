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


@pytest.mark.asyncio
async def test_necessity_mode_tolerates_zero_message_threshold():
    """运行时阈值被改成 0 时必要性评分不除零"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={}))
    current = await event(adapter, "1")
    current.policy_settings = {"wake_mode": "necessity", "wake_message_threshold": 0}
    window = WakeWindow()
    snapshot = window.observe(current, 0)
    decision = window.decide(current, snapshot, 1)
    assert decision.rule == "necessity"


# ================= wake_talk_value 映射测试 =================


def test_talk_value_mapping_is_monotone_with_boundaries():
    from satrap.core.pipeline.wake_policy import NEVER_TRIGGER_THRESHOLD, map_talk_value_threshold

    assert map_talk_value_threshold(0) == NEVER_TRIGGER_THRESHOLD
    assert map_talk_value_threshold(0.05) == 21
    assert map_talk_value_threshold(0.1) == 13
    assert map_talk_value_threshold(0.2) == 8
    assert map_talk_value_threshold(0.35) == 5
    assert map_talk_value_threshold(0.5) == 3
    assert map_talk_value_threshold(0.75) == 2
    assert map_talk_value_threshold(1.0) == 1
    # 单调不增: 频率偏好越高, 阈值越低
    grid = [map_talk_value_threshold(x / 100) for x in range(101)]
    assert all(first >= second for first, second in zip(grid, grid[1:]))
    # base 只替换 0.5 档, 其余阶梯固定
    assert map_talk_value_threshold(0.5, base=5) == 5
    assert map_talk_value_threshold(0.2, base=5) == 8


@pytest.mark.asyncio
async def test_talk_value_changes_frequency_trigger_without_explicit_threshold():
    """未显式设置条数阈值时, wake_talk_value 映射决定触发点"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_cooldown": 0, "wake_talk_value": 1.0}))
    window = WakeWindow()
    first = await event(adapter, "1", text="一句")
    decision = window.decide(first, window.observe(first, 0), 1)
    assert decision.triggered is True
    assert "wake_talk_value=1" in decision.reason and "映射" in decision.reason

    low = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_cooldown": 0, "wake_talk_value": 0.2}))
    window2 = WakeWindow()
    # 0.2 映射为 8 条: 前 7 条不触发, 第 8 条触发
    for i in range(7):
        item = await event(low, str(i + 1), text=f"第{i}句")
        assert window2.decide(item, window2.observe(item, i), i + 1).triggered is False
    item = await event(low, "8", text="第八句")
    decision = window2.decide(item, window2.observe(item, 8), 9)
    assert decision.triggered is True and "wake_talk_value=0.2" in decision.reason


@pytest.mark.asyncio
async def test_explicit_threshold_beats_talk_value():
    """显式 wake_message_threshold 优先于 wake_talk_value 映射"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_cooldown": 0, "wake_talk_value": 1.0, "wake_message_threshold": 3}))
    window = WakeWindow()
    for i in range(2):
        item = await event(adapter, str(i + 1))
        decision = window.decide(item, window.observe(item, i), i + 1)
        assert decision.triggered is False
    assert "显式 wake_message_threshold" in decision.reason
    item = await event(adapter, "3")
    assert window.decide(item, window.observe(item, 3), 4).triggered is True


@pytest.mark.asyncio
async def test_zero_talk_value_never_triggers_frequency_but_mention_still_wakes():
    """talk_value=0 关闭频率触发, 显式 @ 不经 decide 自动路径, 不受连带关闭"""
    from satrap.core.pipeline.wake_policy import evaluate_wake

    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_cooldown": 0, "wake_talk_value": 0}))
    window = WakeWindow()
    for i in range(6):
        item = await event(adapter, str(i + 1))
        decision = window.decide(item, window.observe(item, i), i + 1)
        assert decision.triggered is False
    assert "不触发" in decision.reason and "wake_talk_value=0" in decision.reason
    await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 99,
        "message_type": "group", "message": [{"type": "at", "data": {"qq": "10"}}]})
    assert evaluate_wake(adapter._event_queue.get_nowait()).triggered is True


@pytest.mark.asyncio
async def test_zero_talk_value_does_not_block_necessity_mode():
    """talk_value 映射只接入 frequency 分支, 必要性评分不受影响"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "necessity", "wake_cooldown": 0, "wake_talk_value": 0, "wake_score_threshold": 0.1}))
    window = WakeWindow()
    item = await event(adapter, "1", text="请问这个怎么解决?")
    decision = window.decide(item, window.observe(item, 0), 1)
    assert decision.rule == "necessity" and decision.triggered is True


@pytest.mark.asyncio
async def test_group_override_talk_value_applies_through_resolve():
    """群覆盖的 wake_talk_value 经有效配置合并后生效"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_cooldown": 0,
        "wake_group_overrides": {"20": {"wake_talk_value": 1.0}},
    }))
    window = WakeWindow()
    overridden = await event(adapter, "1", group="20")
    assert window.decide(overridden, window.observe(overridden, 0), 1).triggered is True
    plain = await event(adapter, "2", group="21")
    assert window.decide(plain, window.observe(plain, 2), 3).triggered is False


@pytest.mark.parametrize("value", [-0.1, 1.1, True, "0.5", float("nan")])
def test_talk_value_validation_rejects_out_of_range(value: object):
    from satrap.core.config.platform_policy import validate_wake_policy

    with pytest.raises(ValueError, match="wake_talk_value"):
        validate_wake_policy({"wake_talk_value": value})


def test_talk_value_allowed_in_group_and_time_overrides():
    from satrap.core.config.platform_policy import validate_wake_policy

    overrides: dict[str, object] = {
        "wake_group_overrides": {"456": {"wake_talk_value": 0.5}},
        "wake_time_rules": [{"start": "08:00", "end": "09:00", "settings": {"wake_talk_value": 0.8}}],
    }
    validate_wake_policy(overrides)


# ================= B6: 有效零值先于到期补偿 =================


def test_message_threshold_resolution_reports_source_and_override():
    """有效阈值与来源: 显式优先于映射, talk_value 被显式阈值覆盖时不作为关闭依据"""
    from satrap.core.pipeline.wake_policy import (
        NEVER_TRIGGER_THRESHOLD,
        THRESHOLD_SOURCE_DEFAULT,
        THRESHOLD_SOURCE_EXPLICIT,
        THRESHOLD_SOURCE_TALK_VALUE,
        resolve_message_threshold,
    )

    default = resolve_message_threshold({})
    assert (default.threshold, default.source, default.talk_value_effective, default.closed) == (
        3, THRESHOLD_SOURCE_DEFAULT, False, False,
    )
    explicit = resolve_message_threshold({"wake_message_threshold": 5})
    assert (explicit.threshold, explicit.source, explicit.talk_value_effective, explicit.closed) == (
        5, THRESHOLD_SOURCE_EXPLICIT, False, False,
    )
    mapped = resolve_message_threshold({"wake_talk_value": 0.2})
    assert (mapped.threshold, mapped.source, mapped.talk_value_effective, mapped.closed) == (
        8, THRESHOLD_SOURCE_TALK_VALUE, True, False,
    )
    zero = resolve_message_threshold({"wake_talk_value": 0})
    assert zero.closed and zero.talk_value_effective and zero.threshold == NEVER_TRIGGER_THRESHOLD
    # 反例: 显式阈值存在时 talk_value=0 不是有效关闭, 必须能显示出"被显式阈值覆盖"
    overridden = resolve_message_threshold({"wake_talk_value": 0, "wake_message_threshold": 3})
    assert (overridden.threshold, overridden.source, overridden.closed) == (3, THRESHOLD_SOURCE_EXPLICIT, False)
    assert overridden.talk_value == 0 and overridden.talk_value_effective is False


@pytest.mark.asyncio
async def test_zero_talk_value_is_not_bypassed_by_max_wait():
    """反例: talk_value=0 已关闭自动参与, 到期补偿不得绕过关闭"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_cooldown": 0, "wake_talk_value": 0, "wake_max_wait": 5,
    }))
    window = WakeWindow()
    item = await event(adapter, "1", text="等很久了")
    snapshot = window.observe(item, 0)
    normal = window.decide(item, snapshot, 1)
    stale = window.decide(item, snapshot, 6, deadline=True)
    assert normal.triggered is False and stale.triggered is False
    assert "wake_talk_value=0" in stale.reason and "不触发" in stale.reason
    # 到期复查重复判定同样不触发, 也不消费窗口
    assert window.claim(item, snapshot, True, 7, deadline=True) == ()


@pytest.mark.asyncio
async def test_positive_talk_value_still_triggers_on_deadline():
    """正值映射不受零值关闭影响: 到达最长等待仍按 max_wait 触发"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_cooldown": 0, "wake_talk_value": 0.2, "wake_max_wait": 5,
    }))
    window = WakeWindow()
    item = await event(adapter, "1", text="等很久了")
    snapshot = window.observe(item, 0)
    assert window.decide(item, snapshot, 1).triggered is False
    deadline = window.decide(item, snapshot, 6, deadline=True)
    assert deadline.triggered is True and deadline.rule == "max_wait"


@pytest.mark.asyncio
async def test_explicit_threshold_with_zero_talk_value_keeps_deadline():
    """显式阈值与 talk_value=0 同时存在时按显式阈值判断, 到期补偿照常生效"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_cooldown": 0, "wake_talk_value": 0,
        "wake_message_threshold": 3, "wake_max_wait": 5,
    }))
    window = WakeWindow()
    item = await event(adapter, "1", text="等很久了")
    snapshot = window.observe(item, 0)
    deadline = window.decide(item, snapshot, 6, deadline=True)
    assert deadline.triggered is True and deadline.rule == "max_wait"


@pytest.mark.asyncio
async def test_zero_talk_value_max_wait_does_not_wake_real_pipeline():
    """反例: 真实管线在 talk_value=0 下即使排了到期复查也不调用模型"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_cooldown": 0, "wake_talk_value": 0, "wake_max_wait": 0.02,
    }))
    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    scheduler = PipelineScheduler(manager)
    await scheduler.execute(await event(adapter, "1", text="等很久了"))
    manager.handle_call_async.assert_not_awaited()
    timed = await asyncio.wait_for(adapter._event_queue.get(), 1)
    assert timed.get_extra("wake_decision") is None
    await scheduler.execute(timed)
    manager.handle_call_async.assert_not_awaited()
    decision = timed.get_extra("wake_decision")
    assert decision is not None and decision.triggered is False and "wake_talk_value=0" in decision.reason
    # 未触发不消费正文也不重排: 关闭状态下不会累积定时复查事件
    await asyncio.sleep(0.05)
    assert adapter._event_queue.empty()
    assert len(scheduler.wake_window.peek(timed)) == 1
    manager.handle_call_async.assert_not_awaited()
    await scheduler.wake_timers.close()


@pytest.mark.asyncio
async def test_zero_talk_value_keeps_mention_and_manual_wake():
    """talk_value=0 只关闭自动频率参与: 显式 @ 与手动唤醒不受影响"""
    settings: dict[str, object] = {
        "wake_mode": "frequency", "wake_cooldown": 0, "wake_talk_value": 0,
        "wake_words": ["唤醒"], "wake_max_wait": 0.02,
    }
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    scheduler = PipelineScheduler(manager)
    # 显式 @ 事件不经 decide 自动路径, 直接进入模型
    await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": "m1",
        "message_type": "group", "message": [{"type": "at", "data": {"qq": "10"}}, {"type": "text", "data": {"text": "在吗"}}]})
    await scheduler.execute(adapter._event_queue.get_nowait())
    manager.handle_call_async.assert_awaited_once()
    # 手动唤醒同样不受关闭影响
    manager.handle_call_async.reset_mock()
    manual = await event(adapter, "m2", text="手动内容")
    from satrap.core.pipeline.manual_wake import ManualWakeTicket

    scheduler.manual_wakes.tickets[manual] = ManualWakeTicket("manual-1", ())
    await scheduler.execute(manual)
    manager.handle_call_async.assert_awaited_once()
    await scheduler.wake_timers.close()


@pytest.mark.asyncio
async def test_zero_talk_value_does_not_block_necessity_deadline():
    """necessity 保持既定语义: 到期补偿在必要性模式下照常触发"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "necessity", "wake_score_threshold": 0.99, "wake_talk_value": 0, "wake_max_wait": 5,
    }))
    window = WakeWindow()
    item = await event(adapter, "1", text="随便聊聊")
    decision = window.decide(item, window.observe(item, 0), 6, deadline=True)
    assert decision.triggered is True and decision.rule == "max_wait"
