"""业务链路场景与集中边界契约"""
from __future__ import annotations

from unittest.mock import AsyncMock
import asyncio
import pytest
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.pipeline.wake_window import WakeWindow
from satrap.core.platform import PlatformConfig
from satrap.core.platform.event import MessageEvent
from satrap.core.framework.providers import BindingState, BindingStatus
from typing import Any, cast
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.pipeline.rate_limiter import RateLimiter
from satrap.core.pipeline.request_diagnostics import REJECTION_STAGES, RequestDiagnostic, RequestDiagnosticLog
from satrap.core.platform import PlatformAdapterManager
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from satrap.core.components import Plain, Image, Video, Record, File, Reply, Forward, Node, At
from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.pipeline.wake_dry_run import dry_run_wake
from satrap.core.platform import PlatformAdapter
from satrap.core.platform.event import MessageChain, PlatformMetadata
from satrap.core.plugin_authorization import evaluate_plugin_permissions
from satrap.core.type import Group, MessageMember, PlatformMessage, PlatformMessageType
from satrap.edictum.plugin_config import parse_config_schema
from satrap.edictum.plugin_permissions import parse_plugin_permissions
from support.providers import RunnableRegistry

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
    manager.provider_registry = RunnableRegistry()
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
    manager.provider_registry = RunnableRegistry()
    scheduler = PipelineScheduler(manager)
    await scheduler.execute(queued)
    manager.handle_call_async.assert_not_awaited()
    assert [item.text for item in scheduler.wake_window.peek(queued)] == ["正文"]

@pytest.mark.asyncio
async def test_max_wait_enqueues_once_without_counting_or_repeating():
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_message_threshold": 3, "wake_max_wait": 0.02,
    }))
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
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
    manager.provider_registry = RunnableRegistry()
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
    manager.provider_registry = RunnableRegistry()
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
    manager.provider_registry = RunnableRegistry()
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
    manager.provider_registry = RunnableRegistry()
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
    anonymous_manager = AsyncMock()
    anonymous_manager.provider_registry = RunnableRegistry()
    scheduler = PipelineScheduler(anonymous_manager)
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
async def test_talk_value_resolves_frequency_trigger_with_explicit_and_group_override():
    """wake_talk_value 映射决定默认触发点, 显式阈值与群覆盖优先"""
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

    # 显式 wake_message_threshold 优先于 talk_value 映射
    explicit = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_cooldown": 0, "wake_talk_value": 1.0, "wake_message_threshold": 3}))
    window3 = WakeWindow()
    for i in range(2):
        item = await event(explicit, str(i + 1))
        decision = window3.decide(item, window3.observe(item, i), i + 1)
        assert decision.triggered is False
    assert "显式 wake_message_threshold" in decision.reason
    item = await event(explicit, "3")
    assert window3.decide(item, window3.observe(item, 3), 4).triggered is True

    # 群覆盖的 wake_talk_value 经有效配置合并后生效
    override = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_cooldown": 0,
        "wake_group_overrides": {"20": {"wake_talk_value": 1.0}},
    }))
    window4 = WakeWindow()
    hit = await event(override, "1", group="20")
    assert window4.decide(hit, window4.observe(hit, 0), 1).triggered is True
    plain = await event(override, "2", group="21")
    assert window4.decide(plain, window4.observe(plain, 2), 3).triggered is False

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
async def test_zero_talk_value_closes_automatic_but_keeps_mention_and_manual_wake():
    """talk_value=0 只关闭频率自动参与, 显式 @ 与手动唤醒不受影响, 到期也不旁路"""
    from satrap.core.pipeline.manual_wake import ManualWakeTicket

    settings: dict[str, object] = {
        "wake_mode": "frequency", "wake_cooldown": 0, "wake_talk_value": 0,
        "wake_words": ["唤醒"], "wake_max_wait": 0.02,
    }
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
    manager.handle_call_async.return_value = ""
    scheduler = PipelineScheduler(manager)
    window = WakeWindow()
    for i in range(6):
        item = await event(adapter, str(i + 1))
        decision = window.decide(item, window.observe(item, i), i + 1)
        assert decision.triggered is False
    assert "不触发" in decision.reason and "wake_talk_value=0" in decision.reason
    # 显式 @ 不经 decide 自动路径, 直接进入模型
    await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": "m1",
        "message_type": "group", "message": [{"type": "at", "data": {"qq": "10"}}, {"type": "text", "data": {"text": "在吗"}}]})
    await scheduler.execute(adapter._event_queue.get_nowait())
    manager.handle_call_async.assert_awaited_once()
    # 手动唤醒同样不受关闭影响
    manager.handle_call_async.reset_mock()
    manual = await event(adapter, "m2", text="手动内容")
    scheduler.manual_wakes.tickets[manual] = ManualWakeTicket("manual-1", ())
    await scheduler.execute(manual)
    manager.handle_call_async.assert_awaited_once()
    # 排了到期复查也不调用模型且不重排
    manager.handle_call_async.reset_mock()
    await scheduler.execute(await event(adapter, "w3", text="等很久了"))
    manager.handle_call_async.assert_not_awaited()
    timed = await asyncio.wait_for(adapter._event_queue.get(), 1)
    await scheduler.execute(timed)
    manager.handle_call_async.assert_not_awaited()
    stale = timed.get_extra("wake_decision")
    assert stale is not None and stale.triggered is False and "wake_talk_value=0" in stale.reason
    await asyncio.sleep(0.05)
    assert adapter._event_queue.empty() and not scheduler.wake_timers.tasks
    await scheduler.wake_timers.close()

@pytest.mark.asyncio
@pytest.mark.parametrize("extra, expected", [
    ({"wake_talk_value": 0}, False),
    ({"wake_talk_value": 0.2}, True),
    ({"wake_talk_value": 0, "wake_message_threshold": 3}, True),
], ids=["zero_closed", "positive", "explicit_over_zero"])
async def test_talk_value_max_wait_behaviour(extra: dict[str, object], expected: bool):
    """到期补偿: talk_value=0 不旁路关闭, 正值或被显式阈值覆盖时照常触发"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "frequency", "wake_cooldown": 0, "wake_max_wait": 5, **extra}))
    window = WakeWindow()
    item = await event(adapter, "1", text="等很久了")
    snapshot = window.observe(item, 0)
    assert window.decide(item, snapshot, 1).triggered is False
    deadline = window.decide(item, snapshot, 6, deadline=True)
    if expected:
        assert deadline.triggered is True and deadline.rule == "max_wait"
    else:
        assert deadline.triggered is False
        assert "wake_talk_value=0" in deadline.reason and "不触发" in deadline.reason
        # 到期复查重复判定同样不触发, 也不消费窗口
        assert window.claim(item, snapshot, True, 7, deadline=True) == ()

@pytest.mark.asyncio
async def test_zero_talk_value_does_not_block_necessity_mode():
    """talk_value 映射只接入 frequency 分支, 必要性评分与到期补偿不受影响"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "necessity", "wake_cooldown": 0, "wake_talk_value": 0, "wake_score_threshold": 0.1}))
    window = WakeWindow()
    item = await event(adapter, "1", text="请问这个怎么解决?")
    decision = window.decide(item, window.observe(item, 0), 1)
    assert decision.rule == "necessity" and decision.triggered is True

    strict = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={
        "wake_mode": "necessity", "wake_score_threshold": 0.99, "wake_talk_value": 0, "wake_max_wait": 5}))
    window2 = WakeWindow()
    idle = await event(strict, "1", text="随便聊聊")
    deadline = window2.decide(idle, window2.observe(idle, 0), 6, deadline=True)
    assert deadline.triggered is True and deadline.rule == "max_wait"


def _rejection(adapter: str, reason: str, at: float, request_id: str = "r") -> RequestDiagnostic:
    return RequestDiagnostic(
        adapter_id=adapter, session_id="group%20", actor_id="30", stage="wake_decision",
        decision="dropped", reason=reason, recorded_at=at, message_id="1", request_id=request_id,
    )

def runtime(settings: dict[str, object]) -> tuple[BackendManager, OneBotAdapter, Any, PipelineScheduler]:
    """构造带调度器但不连接外部平台的运行时"""
    backend = BackendManager()
    backend._running = True
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
    manager.handle_call_async.return_value = ""
    scheduler = PipelineScheduler(manager)
    backend._scheduler = scheduler
    backend._adapter_mgr = PlatformAdapterManager()
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"self_id": "10", **settings}))
    adapter.started = True
    backend._adapter_mgr._adapters["bot"] = adapter
    return backend, adapter, manager, scheduler

def _scheduler(backend: BackendManager) -> PipelineScheduler:
    scheduler = backend._scheduler
    if scheduler is None:
        raise AssertionError("运行时装配缺失调度器")
    return scheduler

async def _feed(adapter: OneBotAdapter, text: str, message_id: str = "1") -> None:
    """经真实 OneBot 入站构造事件并出队"""
    await adapter._handle_group_message({
        "self_id": 10, "group_id": 20, "user_id": 30, "message_id": message_id,
        "message_type": "group", "message": [{"type": "text", "data": {"text": text}}],
    })

class TestRejectionLog:
    def test_capacity_order_filter_and_clear(self):
        log = RequestDiagnosticLog(per_adapter=3)
        # 容量按请求计数: 每条记录都是独立请求, 同请求的同阶段记录会被合并
        for index in range(5):
            log.record(_rejection("bot", f"r{index}", 1_700_000_000.0 + index, request_id=f"req{index}"))
        log.record(_rejection("other", "x", 1_700_000_100.0))
        records = log.list("bot")
        # 环形容量 3: 只留最新三条, 最新在前
        assert [item["reason"] for item in records] == ["r4", "r3", "r2"]
        assert all(item["adapter_id"] == "bot" for item in records)
        merged = log.list()
        assert merged[0]["adapter_id"] == "other"
        limited = log.list("bot", limit=2)
        assert len(limited) == 2
        clamped = log.list("bot", limit=9999)
        assert len(clamped) == 3
        log.clear_adapter("bot")
        assert log.list("bot") == []
        assert len(log.list("other")) == 1

    def test_record_fields_and_iso_time(self):
        log = RequestDiagnosticLog()
        log.record(_rejection("bot", "no_match: 未命中", 1_700_000_000.0))
        record = log.list()[0]
        assert record["stage"] == "wake_decision" and record["decision"] == "dropped"
        assert record["session_id"] == "group%20" and record["actor_id"] == "30"
        assert record["message_id"] == "1" and record["request_id"] == "r"
        assert "2023" in str(record["recorded_at"])

class TestSchedulerCollection:
    @pytest.mark.asyncio
    async def test_dropped_at_wake_decision_recorded_without_projection(self):
        backend, adapter, manager, scheduler = runtime({})
        await _feed(adapter, "普通的群消息")
        event = adapter._event_queue.get_nowait()
        await scheduler.execute(event)
        # 验收关键案例: 消息未执行投影/未调用模型, 仍能查到拒绝原因
        manager.handle_call_async.assert_not_called()
        assert event.get_extra("input_projection") is None
        records = scheduler.request_diagnostics.list("bot")
        assert len(records) == 1
        record = records[0]
        assert record["stage"] == "wake_decision" and record["decision"] == "dropped"
        assert "no_match" in str(record["reason"])
        assert record["session_id"] == "group%20" and record["request_id"]

    @pytest.mark.asyncio
    async def test_woken_message_not_recorded(self):
        backend, adapter, manager, scheduler = runtime({"wake_words": ["助手"]})
        await _feed(adapter, "助手在吗")
        event = adapter._event_queue.get_nowait()
        await scheduler.execute(event)
        manager.handle_call_async.assert_called_once()
        # 已唤醒请求不产生拒绝记录, 只留执行阶段诊断
        assert scheduler.request_diagnostics.list("bot", stages=REJECTION_STAGES) == []
        stages = {item["stage"] for item in scheduler.request_diagnostics.list("bot")}
        assert {"model", "send"} <= stages

    @pytest.mark.asyncio
    async def test_rate_limit_rejection_records_wait_and_send_status(self):
        class _DenyAll:
            async def check(self, key: str) -> tuple[bool, float]:
                return False, 7.5

        backend, adapter, manager, scheduler = runtime({"wake_words": ["助手"]})
        scheduler.rate_limiter = cast(RateLimiter, _DenyAll())

        class _OkBot:
            async def send_group_msg(self, **kwargs: Any) -> dict[str, Any]:
                return {"message_id": 9}

        adapter._bot = _OkBot()
        await _feed(adapter, "助手在吗")
        event = adapter._event_queue.get_nowait()
        await scheduler.execute(event)
        manager.handle_call_async.assert_not_called()
        records = scheduler.request_diagnostics.list("bot", stages=REJECTION_STAGES)
        assert len(records) == 1
        record = records[0]
        assert record["stage"] == "rate_limit" and "7.5" in str(record["reason"])
        assert record["send_status"] == "success"

    @pytest.mark.asyncio
    async def test_clear_manual_wakes_also_clears_rejections(self):
        backend, adapter, manager, scheduler = runtime({})
        await _feed(adapter, "普通消息")
        await scheduler.execute(adapter._event_queue.get_nowait())
        assert len(scheduler.request_diagnostics.list("bot")) == 1
        await scheduler.clear_manual_wakes("bot")
        assert scheduler.request_diagnostics.list("bot") == []

class TestRejectionRoute:
    @pytest.mark.asyncio
    async def test_rejections_query_route(self):
        backend, adapter, manager, scheduler = runtime({})
        await _feed(adapter, "普通消息")
        await scheduler.execute(adapter._event_queue.get_nowait())
        server = BackendHTTPServer(backend, port=0)
        response = await server._route("GET", "/api/platforms/wake/rejections?adapter_id=bot&limit=10", b"")
        assert response[0] == 200
        records = cast(list[Any], response[1]["records"])
        assert len(records) == 1
        empty = await server._route("GET", "/api/platforms/wake/rejections?adapter_id=other", b"")
        assert empty[0] == 200 and empty[1]["records"] == []
        bad = await server._route("GET", "/api/platforms/wake/rejections?limit=abc", b"")
        assert bad[0] == 400
        # 不与手动唤醒状态查询路由混淆
        status = await server._route("GET", "/api/platforms/wake/some-request-id", b"")
        assert status[0] in (404, 503)

class _Adapter(PlatformAdapter):
    """不依赖具体平台的适配器, 不发送网络请求"""

    async def run(self) -> None:
        pass

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(name="test", id=self.config.id)

    async def send_message(self, session_id: str, message: MessageChain, **kwargs: Any) -> None:
        pass

def _event(adapter: PlatformAdapter, mid: str, actor: str = "30", components: list[Any] | None = None) -> MessageEvent:
    """构造身份与组件均明确的普通群事件, 不伪造平台命令或历史调用者"""
    message = PlatformMessage()
    message.type = PlatformMessageType.GROUP_MESSAGE
    message.self_id = "10"
    message.session_id = "group%20"
    message.message_id = mid
    message.sender = MessageMember(actor, nickname=f"成员{actor}")
    message.group = Group("20")
    message.message = components if components is not None else [Plain(text=f"正文{mid}")]
    message.message_str = "".join(item.text for item in message.message if isinstance(item, Plain))
    return MessageEvent(message.message_str, message, adapter.meta(), message.session_id, adapter, session_type="dummy")

def _runtime(settings: dict[str, Any] | None = None) -> tuple[Any, Any, PipelineScheduler]:
    """使用真实共享唤醒层和无网络适配器, 仅替换模型执行入口"""
    adapter = _Adapter(PlatformConfig(id="test", type="test", settings={"wake_mode": "all", **(settings or {})}))
    manager = AsyncMock()
    manager.provider_registry = SimpleNamespace(binding_status=lambda *_args: BindingStatus(BindingState.RUNNABLE))
    manager.handle_call_async.return_value = ""
    users: Any = SimpleNamespace(resolve_session=lambda **_kwargs: "shared")
    return adapter, manager, PipelineScheduler(manager, user_manager=users)

@pytest.mark.asyncio
async def test_all_ignores_thresholds_cooldown_and_deadline(monkeypatch: Any) -> None:
    """旧提交时间戳和极高阈值不延迟连续全触发, 不调用 decide 或排程"""
    adapter, manager, scheduler = _runtime({"wake_message_threshold": 32, "wake_talk_value": 0,
                                          "wake_score_threshold": 1, "wake_cooldown": 999, "wake_max_wait": 119})
    first = _event(adapter, "1")
    scheduler.wake_window._submitted[scheduler.wake_window.key(first)] = float("inf")
    monkeypatch.setattr(scheduler.wake_window, "decide", Mock(side_effect=AssertionError("all 不得进入自动阈值")))
    monkeypatch.setattr(scheduler.wake_timers, "schedule", Mock(side_effect=AssertionError("all 不得创建计时器")))
    await scheduler.execute(first)
    await scheduler.execute(_event(adapter, "2"))
    assert manager.handle_call_async.await_count == 2 and not scheduler.wake_timers.tasks
    assert first.get_extra("wake_decision").rule == "all"

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "video", "record", "file", "reply", "forward"])
async def test_all_accepts_supported_non_text_inputs(monkeypatch: Any, kind: str) -> None:
    """附件原事件继续走现有补全与投影, 不因轻量窗口为空而丢弃"""
    adapter, manager, scheduler = _runtime()
    monkeypatch.setattr("satrap.core.pipeline.scheduler.resolve_media", AsyncMock(return_value=()))
    monkeypatch.setattr("satrap.core.pipeline.attachments._download", AsyncMock(return_value="文件正文".encode("utf-8")))
    components = {
        "image": Image(file="https://example.com/image.png"),
        "video": Video(file="https://example.com/video.mp4"),
        "record": Record(file="record", text="语音正文"),
        "file": File(file="https://example.com/note.txt", name="note.txt"),
        "reply": Reply(id="old", message_str="引用原文", sender_id="31"),
        "forward": Forward(id="forward", nodes=[Node(user_id="31", nickname="成员31", content=[Plain(text="转发原文")])]),
    }
    event = _event(adapter, kind, components=[components[kind]])
    await scheduler.execute(event)
    manager.handle_call_async.assert_awaited_once()
    call = manager.handle_call_async.call_args.args[0]
    assert call.origin == event.call_origin
    if kind == "image":
        assert call.img_urls == ["https://example.com/image.png"]
    elif kind == "video":
        assert call.video_urls == ["https://example.com/video.mp4"]
    else:
        assert {"record": "语音正文", "file": "文件正文", "reply": "引用原文", "forward": "转发原文"}[kind] in call.message

@pytest.mark.asyncio
async def test_all_shared_batch_keeps_identity_and_cannot_gain_other_member_permission() -> None:
    """批次内已授权成员的正文仅为证据, 不能提升当前未授权请求者"""
    adapter, manager, scheduler = _runtime({"context_scope": "group"})
    previous = _event(adapter, "admin-message", actor="31")
    current = _event(adapter, "member-message", actor="30")
    scheduler.wake_window.observe(previous)
    meta = {"permission_schema_version": 1, "config_schema": {"writers": {"type": "textarea"}},
            "tools": {"write": "写入"}, "management_permissions": {
                "write": {"description": "写入", "caller_list": "writers", "empty_policy": "deny", "system_admin": False}},
            "tool_permissions": {"write": ["write"]}}
    permissions = parse_plugin_permissions(meta, parse_config_schema(meta))
    decisions = []

    async def handle(call):
        """以真实 UserCall.origin 交给既有授权器, 不读取合并正文作为身份"""
        decisions.append(evaluate_plugin_permissions("probe", permissions, "tools", "write", {"writers": ["31"]}, call.origin).status)
        return ""

    manager.handle_call_async.side_effect = handle
    await scheduler.execute(current)
    call = manager.handle_call_async.call_args.args[0]
    assert "ID 31" in call.message and "admin-message" in call.message
    assert "正文member-message" in call.message and call.origin.actor_id == "30"
    assert decisions == ["denied"]
    await scheduler.execute(_event(adapter, "authorized", actor="31"))
    assert decisions == ["denied", "allowed"]

@pytest.mark.asyncio
async def test_all_competing_events_claim_once_and_do_not_consume_rich_event() -> None:
    """后到者认领当前纯文本后原请求不重放, 含语音的旧事件保留原组件投影"""
    adapter, manager, scheduler = _runtime({"context_scope": "group"})
    first = _event(adapter, "first", components=[Plain(text="第一句")])
    rich = _event(adapter, "rich", components=[Plain(text="语音前言"), Record(file="record", text="语音唯一正文")])
    second = _event(adapter, "second", components=[Plain(text="第二句")])
    gate = asyncio.Event()
    lock_entered = asyncio.Event()
    original_turn = scheduler._session_turn

    @asynccontextmanager
    async def delayed_turn(manager, session_id):
        """确定性让纯文本与语音事件先入窗, 最后事件先持锁认领"""
        if not gate.is_set():
            lock_entered.set()
            await gate.wait()
        async with original_turn(manager, session_id):
            yield

    scheduler._session_turn = delayed_turn
    first_task = asyncio.create_task(scheduler.execute(first))
    await asyncio.wait_for(lock_entered.wait(), 1)
    lock_entered.clear()
    rich_task = asyncio.create_task(scheduler.execute(rich))
    await asyncio.wait_for(lock_entered.wait(), 1)
    scheduler._session_turn = original_turn
    await scheduler.execute(second)
    gate.set()
    await asyncio.gather(first_task, rich_task)
    calls = [item.args[0] for item in manager.handle_call_async.call_args_list]
    assert len(calls) == 2
    assert sum("第一句" in call.message for call in calls) == 1
    assert sum("第二句" in call.message for call in calls) == 1
    assert sum("语音唯一正文" in call.message for call in calls) == 1
    assert "语音前言" not in calls[0].message
    assert any(item["reason_code"] == "window_unavailable" for item in scheduler.request_diagnostics.list())

@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["mode", "scope", "disabled", "permission", "binding", "route", "unused_threshold"])
async def test_all_rechecks_queued_request_and_removes_revoked_content(change: str) -> None:
    """排队后切换有效规则或撤权不提交旧内容, 未使用的阈值修改仍允许正常执行"""
    adapter, manager, scheduler = _runtime({"context_scope": "group"})
    event = _event(adapter, "queued", components=[Plain(text="不应残留的旧正文")])
    entered = asyncio.Event()
    original_observe = scheduler.wake_window.observe

    def observed(*args, **kwargs):
        """通知入窗完成, 模型执行权仍被测试持有"""
        result = original_observe(*args, **kwargs)
        entered.set()
        return result

    scheduler.wake_window.observe = observed
    async with scheduler._session_turn(manager, "shared"):
        task = asyncio.create_task(scheduler.execute(event))
        await asyncio.wait_for(entered.wait(), 1)
        if change == "mode":
            adapter.config.settings = {**adapter.config.settings, "wake_mode": "explicit"}
        elif change == "scope":
            adapter.config.settings = {**adapter.config.settings, "context_scope": "group_member"}
        elif change == "disabled":
            adapter.config.enable = False
        elif change == "permission":
            scheduler._check_permission = AsyncMock(return_value=False)
        elif change == "binding":
            manager.provider_registry.binding_status = lambda *_args: BindingStatus(BindingState.DISABLED, "已禁用")
        elif change == "route":
            event.agent_route_is_current = lambda: False
        else:
            adapter.config.settings = {**adapter.config.settings, "wake_cooldown": 999}
    await asyncio.wait_for(task, 1)
    if change == "unused_threshold":
        manager.handle_call_async.assert_awaited_once()
    else:
        manager.handle_call_async.assert_not_awaited()
        assert not scheduler.wake_window.peek(event)

@pytest.mark.asyncio
async def test_all_switch_revokes_timer_and_queued_deadline() -> None:
    """切换全触发后旧最长等待请求不再执行, 普通请求直接消费保留窗口"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"wake_mode": "frequency", "wake_message_threshold": 32, "wake_max_wait": 0.01}))
    _generic, manager, scheduler = _runtime()
    scheduler.user_manager = None
    event = _event(adapter, "old")
    await scheduler.execute(event)
    timed = await asyncio.wait_for(adapter._event_queue.get(), 1)
    adapter.config.settings = {**adapter.config.settings, "wake_mode": "all"}
    await scheduler.execute(_event(adapter, "new"))
    assert scheduler.wake_timers.tickets[timed].cancelled
    await scheduler.execute(timed)
    manager.handle_call_async.assert_awaited_once()
    assert not scheduler.wake_timers.tasks
    await scheduler.wake_timers.close()

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["self", "notice", "no_identity", "rate", "empty"])
async def test_all_rejects_ineligible_input_and_keeps_rate_limit(kind: str) -> None:
    """自身消息/非消息/身份缺失不触发, 限流和空输入仍明确拒绝"""
    adapter, manager, scheduler = _runtime()
    event = _event(adapter, "test", actor="10" if kind == "self" else "" if kind == "no_identity" else "30",
                   components=[] if kind == "empty" else None)
    if kind == "notice":
        event.platform_message.type = PlatformMessageType.OTHER_MESSAGE
    if kind == "rate":
        scheduler.rate_limiter = RateLimiter(rate=0.01, burst=1)
        await scheduler.rate_limiter.check(f"{adapter.config.id}:{event.session_id}")
    await scheduler.execute(event)
    manager.handle_call_async.assert_not_awaited()

@pytest.mark.asyncio
async def test_all_dry_run_inheritance_and_no_automatic_threshold(monkeypatch: Any) -> None:
    """平台/时段/群覆盖一致, 试算提交不调用自动 decide 且阈值不适用"""
    settings = {"wake_mode": "explicit", "wake_cooldown": 999, "wake_talk_value": 0, "wake_max_wait": 119,
                "wake_time_rules": [{"start": "09:00", "end": "18:00", "settings": {"wake_mode": "all"}}],
                "wake_group_overrides": {"21": {"wake_mode": "frequency"}}}
    validate_wake_policy(settings)
    original = WakeWindow.decide
    monkeypatch.setattr(WakeWindow, "decide", Mock(side_effect=AssertionError("all 不进入 decide")))
    result = await dry_run_wake({"settings": settings, "group_id": "20", "local_time": "12:00", "probe": {"text": "你好"},
                                 "steps": [{"text": "第一条"}, {"submit": True}, {"text": "第二条"}]})
    assert result["explicit"]["rule"] == "all"
    automatic = result["automatic"]
    assert automatic["steps"][1]["claimed"] == 1 and automatic["decision"]["rule"] == "all"
    assert automatic["cooldown_remaining"] == 0 and automatic["threshold"]["applicable"] is False
    assert automatic["deadline_decision"]["rule"] == "no_timer"
    monkeypatch.setattr(WakeWindow, "decide", original)
    other = await dry_run_wake({"settings": settings, "group_id": "21", "local_time": "12:00", "steps": [{"text": "一条"}]})
    assert other["automatic"]["mode"] == "frequency"
    night = await dry_run_wake({"settings": settings, "group_id": "20", "local_time": "23:00", "steps": [{"text": "一条"}]})
    assert night["automatic"]["mode"] == "explicit"

@pytest.mark.asyncio
async def test_all_real_onebot_ingress_dedup_and_group_admission() -> None:
    """真实平台入站去重和群准入仍先于全触发, 不连接 QQ 或发送消息"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"wake_mode": "all", "group_whitelist": ["20"]}))
    _generic, manager, scheduler = _runtime()
    scheduler.user_manager = None
    payload = {"self_id": 10, "group_id": 20, "user_id": 30, "message_id": "same", "message_type": "group",
               "message": [{"type": "text", "data": {"text": "无提及正文"}}]}
    await adapter._handle_group_message(payload)
    await adapter._handle_group_message(payload)
    assert adapter._event_queue.qsize() == 1
    await scheduler.execute(adapter._event_queue.get_nowait())
    manager.handle_call_async.assert_awaited_once()
    await adapter._handle_group_message({**payload, "message_id": "other-group", "group_id": 21})
    await adapter._handle_group_message({**payload, "message_id": "self", "user_id": 10})
    assert adapter._event_queue.empty()

@pytest.mark.asyncio
async def test_all_command_keeps_window_and_freezes_command_body() -> None:
    """显式平台命令沿用原会话命令入口, 不认领或附带旧窗口正文"""
    adapter, manager, scheduler = _runtime({"context_scope": "group"})
    previous = _event(adapter, "old", components=[Plain(text="不属于命令的正文")])
    snapshot = scheduler.wake_window.observe(previous)
    command = _event(adapter, "command", components=[At(qq="10"), Plain(text=" /help")])
    await scheduler.execute(command)
    manager.handle_call_async.assert_awaited_once()
    assert manager.handle_call_async.call_args.args[0].message == "/help"
    assert scheduler.wake_window.peek(previous) == snapshot

@pytest.mark.asyncio
async def test_all_empty_current_event_does_not_submit_previous_window() -> None:
    """空消息不能借旧窗口触发模型, 拒绝理由仍可查询"""
    adapter, manager, scheduler = _runtime({"context_scope": "group"})
    scheduler.wake_window.observe(_event(adapter, "old"))
    await scheduler.execute(_event(adapter, "empty", components=[]))
    manager.handle_call_async.assert_not_awaited()
    assert any(item["reason_code"] == "empty_input" for item in scheduler.request_diagnostics.list())

@pytest.mark.asyncio
async def test_all_budget_overflow_survives_final_projection_diagnostics() -> None:
    """容量截断的诊断不被随后完成的附件投影覆盖, 仍保留来源标签"""
    adapter, manager, scheduler = _runtime({"context_scope": "group"})
    scheduler.wake_window = WakeWindow(max_messages=1)
    scheduler.wake_window.observe(_event(adapter, "evicted", actor="31"))
    current = _event(adapter, "kept")
    await scheduler.execute(current)
    manager.handle_call_async.assert_awaited_once()
    rows = scheduler.request_diagnostics.list()
    projection = next(row for row in rows if row["stage"] == "projection")
    assert projection["status"] == "partial" and "wake_window_budget" in str(projection["notes"])
    assert current.get_extra("wake_window_overflow")["messages"] == 1

@pytest.mark.asyncio
async def test_all_stale_deadline_is_rejected_before_any_new_all_event() -> None:
    """切换模式后尚无新消息, 旧补偿事件也不能通过上游唤醒或显式词继续执行"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"wake_mode": "frequency", "wake_message_threshold": 32, "wake_max_wait": 0.01}))
    _generic, manager, scheduler = _runtime()
    scheduler.user_manager = None
    await scheduler.execute(_event(adapter, "timer"))
    timed = await asyncio.wait_for(adapter._event_queue.get(), 1)
    adapter.config.settings = {**adapter.config.settings, "wake_mode": "all"}
    timed.is_wake = True
    await scheduler.execute(timed)
    manager.handle_call_async.assert_not_awaited()
    assert any(row["reason_code"] == "stale_wake_policy" for row in scheduler.request_diagnostics.list())
    await scheduler.wake_timers.close()
