"""全触发的普通输入, 来源隔离, 排队复核和窗口原子消费契约"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from satrap.core.components import Plain, Image, Video, Record, File, Reply, Forward, Node, At
from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.framework.providers import BindingState, BindingStatus
from satrap.core.pipeline.rate_limiter import RateLimiter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.pipeline.wake_dry_run import dry_run_wake
from satrap.core.pipeline.wake_policy import evaluate_wake
from satrap.core.pipeline.wake_window import WakeWindow
from satrap.core.platform import PlatformAdapter, PlatformConfig
from satrap.core.platform.event import MessageChain, MessageEvent, PlatformMetadata
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.plugin_authorization import evaluate_plugin_permissions
from satrap.core.type import Group, MessageMember, PlatformMessage, PlatformMessageType
from satrap.edictum.plugin_config import parse_config_schema
from satrap.edictum.plugin_permissions import parse_plugin_permissions


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
