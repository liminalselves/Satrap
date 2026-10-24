"""业务链路场景与集中边界契约"""
from __future__ import annotations

from dataclasses import replace
import inspect
from pathlib import Path
import pytest
from typing import Any, cast
from satrap.core.framework.command import AsyncCommandHandler, CommandHandler as _command_entry_CommandHandler
from satrap.core.framework.command.base import DEFAULT_COMMAND_PREFIX
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.pipeline.command_entry import OPERATOR_ONLY_COMMANDS, extract_command_candidate
from satrap.core.pipeline.command_entry import is_operator_only_command
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.components import At, AtAll, BaseMessageComponent, Image, Plain, Reply
from satrap.core.platform.event import MessageEvent, PlatformMetadata
from satrap.core.type import Group, MessageMember, PlatformMessage, PlatformMessageType
from satrap.expend.command.session_commands import cmd_history_async, cmd_new, cmd_new_async
from satrap.expend.command.session_commands import cmd_switch, cmd_switch_async
from satrap.core.framework import AsyncSession, CommandHandler as _session_commands_CommandHandler, Session
from satrap.edictum.plugin import collect_commands
from satrap.core.type import CommandAction
from satrap.core.utils.TCBuilder import AsyncTool, AsyncToolsManager, Tool, ToolsManager, _summarize_arguments
from support.providers import RunnableRegistry
from support.adapters import RecorderAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_command_history_switch_and_history_edit(tmp_path, asynchronous):
    """命令记录由会话开关控制, 清空和切换不回灌历史, 不调用模型"""
    from satrap.edictum import SimpleSession, AsyncSimpleSession
    from satrap.core.APICall.LLMCall import LLM, AsyncLLM

    model = AsyncLLM(api_key="test") if asynchronous else LLM(api_key="test")
    cls = AsyncSimpleSession if asynchronous else SimpleSession
    session = cls("commands", model, db_path=str(tmp_path / "platform.db"), enable_checkpoint=False)
    if asynchronous:
        await session.initialize()

    async def invoke(text):
        return await session.run(text) if asynchronous else session.run(text)

    async def answer_async():
        return "已执行"

    async def clear_async():
        await session.ctx.del_context()
        return "已清空"

    registry = session.command_handler
    registry.register_command("answer", answer_async if asynchronous else lambda: "已执行")
    assert await invoke("/answer") == "已执行"
    assert session.ctx.get_context() == []
    session.command_history_enabled = True
    assert await invoke("/answer") == "已执行"
    assert session.ctx.get_context() == [
        {"role": "user", "content": "/answer"}, {"role": "assistant", "content": "已执行"},
    ]
    registry.register_command("clear", clear_async if asynchronous else lambda: session.ctx.del_context())
    await invoke("/clear")
    assert session.ctx.get_context() == []
    await invoke("/clear")
    assert session.ctx.get_context() == []
    session.command_history_enabled = False
    await invoke("/answer")
    assert session.ctx.get_context() == []
    if asynchronous:
        await model.client.close()
    else:
        model.client.close()


def _event(
    components: list[BaseMessageComponent],
    *,
    private: bool = False,
    self_id: str = "bot",
    message_str: str = "",
    sender_id: str = "user-1",
) -> MessageEvent:
    """构造只带顶层组件的事件"""
    message = PlatformMessage()
    message.type = PlatformMessageType.FRIEND_MESSAGE if private else PlatformMessageType.GROUP_MESSAGE
    message.self_id = self_id
    message.session_id = "s1"
    message.message_id = "msg-1"
    message.sender = MessageMember(user_id=sender_id, nickname="User")
    message.group = None if private else Group(group_id="g1", group_name="g1")
    message.message = list(components)
    message.message_str = message_str
    return MessageEvent(
        message_str=message_str,
        platform_message=message,
        platform_meta=PlatformMetadata(name="rec1", id="rec1"),
        session_id="s1",
        adapter=None,
        session_type="dummy",
    )

@pytest.mark.parametrize(
    ("components", "expected", "kwargs"),
    [
        ([At(qq="bot"), Plain(text=" /help")], "/help", {}),
        ([At(qq="bot"), At(qq="bot"), Plain(text=" /help")], "/help", {}),
        ([At(qq="bot"), Plain(text="   /help")], "/help", {}),
        ([At(qq="bot"), Plain(text=" "), At(qq="bot"), Plain(text=" /help")], "/help", {}),
        ([At(qq="other"), Plain(text=" /help")], None, {}),
        ([AtAll(), Plain(text=" /help")], None, {}),
        ([Plain(text="/help")], None, {}),
        ([Plain(text="/help")], "/help", {"private": True}),
        ([Reply(id="m0"), At(qq="bot"), Plain(text=" /help")], "/help", {}),
        ([At(qq="bot"), Plain(text=" /help"), Image(file="https://cdn/a.png")], None, {}),
    ],
)
def test_extract_command_candidate_table(components, expected, kwargs):
    """平台入口命令候选抽取: 寻址段/非文本正文/群私聊差异的集中判定"""
    assert extract_command_candidate(_event(components, **kwargs)) == expected

def test_body_not_starting_with_prefix_is_not_command():
    """正文不以前缀开头时不是命令候选"""
    event = _event([At(qq="bot"), Plain(text=" 你好")], message_str="@bot 你好")
    assert extract_command_candidate(event) is None

def test_trailing_self_mention_is_kept_only_in_private_chat():
    """尾随 self 提及不剥离: 群内不构成命令, 私聊保留在正文里"""
    private = _event([Plain(text="/help "), At(qq="bot")], private=True)
    assert extract_command_candidate(private) == "/help @bot"
    assert extract_command_candidate(_event([Plain(text="/help "), At(qq="bot")])) is None

def test_multiple_commands_are_left_to_session_layer():
    """候选只看正文起点, 参数个数与命令名合法性交由会话层判定"""
    event = _event([At(qq="bot"), Plain(text=" /goal 修复 bug10")])
    assert extract_command_candidate(event) == "/goal 修复 bug10"

def test_self_mention_agrees_with_wake_predicate():
    """谓词等价性: self 提及判定与唤醒的提及分支一致"""
    from satrap.core.components import is_self_mention
    from satrap.core.pipeline.wake_policy import evaluate_wake

    for qq, expected in (("bot", True), ("other", False), ("all", False)):
        event = _event([At(qq=qq), Plain(text=" /help")], private=False)
        assert is_self_mention(event.get_messages()[0], "bot") is expected
        assert (evaluate_wake(event).rule == "mention") is expected

def test_empty_self_id_never_matches():
    """self_id 缺失时任何提及都不算 self 提及"""
    from satrap.core.components import is_self_mention

    assert is_self_mention(At(qq=""), "") is False
    assert is_self_mention(At(qq="bot"), "") is False
    assert extract_command_candidate(_event([At(qq="bot"), Plain(text=" /help")], self_id="")) is None

def test_command_prefix_has_single_source(monkeypatch: pytest.MonkeyPatch):
    """前缀单一来源: 处理器默认值与平台入口判定取自同一常量, 改写常量后判定同步变化"""
    assert inspect.signature(AsyncCommandHandler.__init__).parameters["cmd_prefix"].default is DEFAULT_COMMAND_PREFIX
    assert inspect.signature(_command_entry_CommandHandler.__init__).parameters["cmd_prefix"].default is DEFAULT_COMMAND_PREFIX
    assert AsyncCommandHandler().prefix == DEFAULT_COMMAND_PREFIX
    assert _command_entry_CommandHandler().prefix == DEFAULT_COMMAND_PREFIX

    monkeypatch.setattr("satrap.core.pipeline.command_entry.DEFAULT_COMMAND_PREFIX", "#")
    assert extract_command_candidate(_event([At(qq="bot"), Plain(text=" #help")])) == "#help"
    assert extract_command_candidate(_event([At(qq="bot"), Plain(text=" /help")])) is None

def test_extract_does_not_touch_message_str():
    """判定不改写平台侧渲染文本与组件列表"""
    message_str = "@bot /help"
    event = _event([At(qq="bot"), Plain(text=" /help")], message_str=message_str)
    components = event.get_messages()
    assert extract_command_candidate(event) == "/help"
    assert event.get_message_str() == message_str
    assert event.get_messages() == components


class _FakeSessionManager:
    """记录 handle_call_async 调用的假 SessionManager"""

    class_cfg_mgr = None
    provider_registry = RunnableRegistry()

    def __init__(self, response: str = "回复"):
        self.response = response
        self.calls: list[Any] = []

    async def handle_call_async(self, user_call: Any) -> str:
        self.calls.append(user_call)
        return self.response

class _FakeUserManager:
    """按平台与成员解析会话的假 UserManager"""

    def __init__(self):
        self.resolved: list[tuple[str, str, str, str]] = []

    def resolve_session(self, user_id: str, platform: str, session_type: str,
                        class_cfg_mgr: Any, extra_params: Any,
                        session_provider: str, **kwargs: Any) -> str:
        self.resolved.append((user_id, platform, session_provider, session_type))
        return f"{platform}:{user_id}:sid"

def _as_session_manager(fake: Any) -> SessionManager:
    """SessionManager 替身类型边界: 替身只实现 handle_call_async 调用面"""
    return cast(SessionManager, fake)

def _scheduler_event(
    adapter: RecorderAdapter,
    components: list[BaseMessageComponent],
    *,
    private: bool = False,
    message_str: str = "",
    sender_id: str = "user-1",
    request_id_note: str = "",
) -> MessageEvent:
    """构造接入真实适配器的群/私聊事件"""
    message = PlatformMessage()
    message.type = PlatformMessageType.FRIEND_MESSAGE if private else PlatformMessageType.GROUP_MESSAGE
    message.self_id = "bot"
    message.session_id = "s1"
    message.message_id = f"msg-{sender_id}{request_id_note}"
    message.sender = MessageMember(user_id=sender_id, nickname="User")
    message.group = None if private else Group(group_id="g1", group_name="g1")
    message.message = list(components)
    message.message_str = message_str
    return MessageEvent(
        message_str=message_str,
        platform_message=message,
        platform_meta=PlatformMetadata(name=adapter.config.id, id=adapter.config.id),
        session_id="s1",
        adapter=adapter,
        session_type="dummy",
    )

@pytest.mark.asyncio
async def test_command_freezes_text_and_keeps_window():
    """群命令送入会话的是冻结正文, 窗口里既有正文不被消费"""
    adapter = RecorderAdapter(settings={"context_scope": "group"})
    manager = _FakeSessionManager(response="/help 回执")
    scheduler = PipelineScheduler(_as_session_manager(manager), user_manager=cast(Any, _FakeUserManager()))

    # Step.1 预置三条待处理正文: 群共享范围内同属一条路由
    for index in range(3):
        pending = _scheduler_event(adapter, [Plain(text=f"闲聊 {index}")], sender_id="u1", request_id_note=str(index))
        scheduler.wake_window.observe(pending)
    before = [item.text for item in scheduler.wake_window.peek(pending)]
    assert before == ["闲聊 0", "闲聊 1", "闲聊 2"]

    # Step.2 群内 @bot /help: 提及本就唤醒, 冻结正文不带走窗口
    command = _scheduler_event(adapter, [At(qq="bot"), Plain(text=" /help")], message_str="@bot /help", sender_id="u1")
    await scheduler.execute(command)

    assert len(manager.calls) == 1
    assert manager.calls[0].message == "/help"
    assert manager.calls[0].img_urls == []
    assert manager.calls[0].video_urls == []
    assert "[先前窗口消息" not in manager.calls[0].message
    assert "[用户" not in manager.calls[0].message
    assert "@bot" not in manager.calls[0].message

    # Step.3 命令未 claim 也未入窗: 三条既有正文仍在, 命令文本不在
    assert [item.text for item in scheduler.wake_window.peek(command)] == before

@pytest.mark.asyncio
async def test_command_skips_window_observe_in_frequency_mode():
    """频率模式下命令不发生 observe: 窗口行数不变, 命令文本不入窗"""
    adapter = RecorderAdapter(settings={"wake_mode": "frequency", "context_scope": "group"})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager), user_manager=cast(Any, _FakeUserManager()))

    for index in range(3):
        scheduler.wake_window.observe(
            _scheduler_event(adapter, [Plain(text=f"闲聊 {index}")], sender_id="u1", request_id_note=str(index))
        )
    observed = [item.text for item in scheduler.wake_window.peek(_scheduler_event(adapter, [Plain(text="")]))]
    assert observed == ["闲聊 0", "闲聊 1", "闲聊 2"]

    command = _scheduler_event(adapter, [At(qq="bot"), Plain(text=" /help")], message_str="@bot /help", sender_id="u1")
    await scheduler.execute(command)

    assert len(manager.calls) == 1
    # Step.1 命令未 observe 也未 claim: 三条既有正文原样留在窗口里
    after = [item.text for item in scheduler.wake_window.peek(command)]
    assert after == observed

    # Step.2 随后一条普通消息仍能看到它们, 并按频率阈值连同自身一起提交
    follow_up = _scheduler_event(
        adapter, [Plain(text="那条命令怎么说")], sender_id="u1", message_str="那条命令怎么说", request_id_note="f",
    )
    await scheduler.execute(follow_up)

    assert len(manager.calls) == 2
    merged = manager.calls[1].message
    assert merged.count("[先前窗口消息:") == 3
    for index in range(3):
        assert f"闲聊 {index}" in merged


@pytest.mark.parametrize("scope", ["group", "group_member"])
async def test_new_success_boundary_preserves_later_window_and_cancels_old_tickets(scope: str, monkeypatch: pytest.MonkeyPatch):
    """宿主确认重置后只清旧窗口, 旧快照撤销, 后到正文仍能继续提交"""
    from satrap.core.pipeline.wake_timers import DeadlineTicket
    from satrap.core.pipeline.manual_wake import ManualWakeTicket
    from satrap.core.platform import event as event_module
    stamp = event_module.monotonic()
    monkeypatch.setattr(event_module, "monotonic", lambda: stamp)
    adapter = RecorderAdapter(settings={"context_scope": scope, "wake_mode": "frequency", "wake_message_threshold": 100, "wake_max_wait": 30})
    scheduler = PipelineScheduler(_as_session_manager(_FakeSessionManager()), user_manager=cast(Any, _FakeUserManager()))
    old = _scheduler_event(adapter, [Plain(text="重置前")], sender_id="u1", request_id_note="old")
    another = _scheduler_event(adapter, [Plain(text="另一位成员")], sender_id="u2", request_id_note="other")
    command = _scheduler_event(adapter, [At(qq="bot"), Plain(text=" /new")], sender_id="u1", request_id_note="command")
    scheduler.wake_window.observe(old)
    scheduler.wake_window.observe(another)
    scheduler.wake_timers.schedule(old)
    old_snapshot = scheduler.wake_window.peek(command)
    deadline_event = _scheduler_event(adapter, [Plain(text="old deadline")], sender_id="u1", request_id_note="deadline")
    manual_event = _scheduler_event(adapter, [Plain(text="old manual")], sender_id="u1", request_id_note="manual")
    scheduler.wake_timers.tickets[deadline_event] = DeadlineTicket(snapshot=old_snapshot)
    scheduler.manual_wakes.tickets[manual_event] = ManualWakeTicket("manual", snapshot=old_snapshot)

    class Manager(_FakeSessionManager):
        async def handle_call_async(self, call):
            self.calls.append(call)
            if call.message == "/new":
                call.context_reset_at = call.received_at
                after = _scheduler_event(adapter, [Plain(text="重置后")], sender_id="u3" if scope == "group" else "u1", request_id_note="after")
                assert after.queued_at == command.queued_at and after.queued_sequence > command.queued_sequence
                scheduler.wake_window.observe(after)
                scheduler.wake_timers.schedule(after)
                return "已开始新对话"
            return "已回复"

    manager = Manager()
    scheduler.session_manager = _as_session_manager(manager)
    await scheduler.execute(command)
    assert [row.text for row in scheduler.wake_window.peek(command)] == ["重置后"]
    assert scheduler.wake_timers.tickets[deadline_event].cancelled
    assert scheduler.manual_wakes.tickets[manual_event].cancelled
    key = scheduler.wake_window.key(command)
    assert key in scheduler.wake_timers.tasks
    assert scheduler.wake_timers._sources[key].call_origin.actor_id == ("u3" if scope == "group" else "u1")
    if scope == "group_member":
        assert [row.text for row in scheduler.wake_window.peek(another)] == ["另一位成员"]
    await scheduler.execute(deadline_event)
    await scheduler.execute(manual_event)
    assert len(manager.calls) == 1
    await scheduler.wake_timers.close()


@pytest.mark.parametrize("mode", ["mention", "all"])
async def test_new_failure_keeps_window_and_waiting_message_re_resolves_route(mode: str, monkeypatch: pytest.MonkeyPatch):
    """失败没有重置信号, 并发等待的普通消息必须使用切换后的实际路由"""
    import asyncio
    from satrap.core.platform import event as event_module
    stamp = event_module.monotonic()
    monkeypatch.setattr(event_module, "monotonic", lambda: stamp)
    adapter = RecorderAdapter(settings={"context_scope": "group", "wake_mode": mode})
    entered, release = asyncio.Event(), asyncio.Event()

    class Users(_FakeUserManager):
        current = "old"
        def resolve_session(self, **kwargs):
            return self.current

    users = Users()

    class Manager(_FakeSessionManager):
        async def handle_call_async(self, call):
            self.calls.append(call)
            if call.message == "/new":
                entered.set()
                await release.wait()
                users.current = "new"
                call.session_id = "new"
                call.context_reset_at = call.received_at
            return "回执"

    manager = Manager()
    scheduler = PipelineScheduler(_as_session_manager(manager), user_manager=cast(Any, users))
    pending = _scheduler_event(adapter, [Plain(text="旧窗口")], request_id_note="old")
    command = _scheduler_event(adapter, [At(qq="bot"), Plain(text=" /new")], request_id_note="new")
    scheduler.wake_window.observe(pending)
    # Step.1 无成功信号的失败回复不能撤销已有窗口
    scheduler.session_manager = _as_session_manager(_FakeSessionManager(response="切换失败"))
    await scheduler.execute(command)
    assert [row.text for row in scheduler.wake_window.peek(command)] == ["旧窗口"]
    scheduler.session_manager = _as_session_manager(manager)
    task = asyncio.create_task(scheduler.execute(command))
    await entered.wait()
    next_event = _scheduler_event(adapter, [At(qq="bot"), Plain(text=" 后续消息")], message_str="@bot 后续消息", request_id_note="next")
    following = asyncio.create_task(scheduler.execute(next_event))
    await asyncio.sleep(0)
    assert len(manager.calls) == 1
    release.set()
    await asyncio.gather(task, following)
    assert len(manager.calls) == 2, scheduler.request_diagnostics.list()[-8:]
    assert manager.calls[1].session_id == "new"
    assert "旧窗口" not in manager.calls[1].message
    assert not scheduler._session_turns


@pytest.mark.asyncio
async def test_queue_order_uses_actual_enqueue_when_clock_is_equal(monkeypatch: pytest.MonkeyPatch):
    """事件构造顺序可以不同于入队顺序, 同刻窗口排序仍按实际入队顺序"""
    from satrap.core.platform import event as event_module
    from satrap.core.pipeline.wake_window import WakeWindow
    monkeypatch.setattr(event_module, "monotonic", lambda: 100.0)
    adapter = RecorderAdapter(settings={"context_scope": "group"})
    first = _scheduler_event(adapter, [Plain(text="后入队")], request_id_note="first")
    second = _scheduler_event(adapter, [Plain(text="先入队")], request_id_note="second")
    assert first.queued_sequence < second.queued_sequence
    assert adapter.commit_event(second) and adapter.commit_event(first)
    assert second.queued_sequence < first.queued_sequence
    window = WakeWindow()
    window.observe(first, now=100.0)
    snapshot = window.observe(second, now=100.0)
    assert [row.text for row in snapshot] == ["先入队", "后入队"]


@pytest.mark.parametrize("scope", ["legacy_user", "group_member", "group"])
@pytest.mark.parametrize(("asynchronous", "async_entry"), [(False, False), (False, True), (True, True)], ids=["sync-entry", "sync-session-async-entry", "async-simple"])
async def test_real_edictum_new_history_switch_and_route_isolation(tmp_path, monkeypatch, scope, asynchronous, async_entry):
    """真实会话及命令插件完成新建、上下文重置、历史持久化与范围隔离"""
    from satrap.core.conversation import ConversationRoute
    from satrap.core.framework.UserManager import UserManager, UserInfoStore
    from satrap.core.framework.providers import EdictumProvider
    from satrap.edictum.registry import create_default_edictum_type_registry
    from satrap.edictum.config import EdictumConfigManager
    from satrap.edictum.plugin_compatibility import PluginEnvironment
    from satrap.core.type import LLMCallResponse, UserCall
    from support.group_models import Script, Model, AsyncModel

    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / "edictum.json")
    configs.create("chat", {"edictum_type": "async_simple" if asynchronous else "simple",
                            "plugins": [{"name": "session_commands"}], "params": {"enable_checkpoint": False}})
    database = tmp_path / "platform.db"
    provider = EdictumProvider(configs, registry, default_checkpoint_db=str(database))
    script = Script([LLMCallResponse(type="text", content=text) for text in ("旧回复", "新回复", "恢复回复")])
    model = AsyncModel(script) if asynchronous else Model(script)
    create = provider.create_session
    monkeypatch.setattr(provider, "create_session", lambda config, llm=None, **kwargs: create(config, llm=cast(Any, model), **kwargs))
    manager = SessionManager(db_path=database, default_checkpoint_db=str(database))
    manager.plugin_environment = PluginEnvironment("platform", "onebot")
    manager.register_provider(provider)
    users = UserManager(manager, db_path=database)
    manager._user_mgr = users
    route = ConversationRoute("user", "qq", "chat", provider="edictum", scope=scope, self_id="bot", group_id="group")

    async def invoke(text, current_route=route):
        sid = users.resolve_session(current_route.user_id, "qq", "chat", session_provider="edictum", route=current_route)
        call = UserCall(session_id=sid, session_provider="edictum", session_type="chat", message=text, route=current_route)
        result = await manager.handle_call_async(call) if async_entry else manager.handle_call(call)
        return call, result

    try:
        old, response = await invoke("旧轮唯一内容")
        assert response == "旧回复"
        assert old.history_window is not None
        old_window = old.history_window
        new, response = await invoke("/new")
        assert "已开始新对话" in response and new.session_id != old.session_id
        assert new.context_reset_at is not None
        new_config = manager.store.get(new.session_id)
        assert new_config is not None and new_config.history_started_at == new.received_wall_at
        fresh, response = await invoke("新轮唯一内容")
        assert fresh.history_window is not None and fresh.history_window.started_at == new.received_wall_at
        assert response == "新回复"
        assert "旧轮唯一内容" not in str(script.requests[-1][0])
        assert "旧回复" not in str(script.requests[-1][0])
        _, history = await invoke("/history")
        assert old.session_id in history and new.session_id in history
        assert set(UserInfoStore(database).list_context_history(route)) == {old.session_id, new.session_id}
        if scope != "legacy_user":
            other = replace(route, group_id="another-group")
            outsider = users.resolve_session("user", "qq", "chat", session_provider="edictum", route=other)
            _, denied = await invoke(f"/switch {outsider}")
            assert "对话不存在" in denied
            assert users.resolve_session("user", "qq", "chat", session_provider="edictum", route=route) == new.session_id
        if scope == "group":
            peer = replace(route, user_id="other-member")
            assert UserInfoStore(database).list_context_history(peer) == users.store.list_context_history(route)
        switched, response = await invoke(f"/switch {old.session_id}")
        assert "已切换至" in response and switched.session_id == old.session_id
        assert switched.context_reset_at is None
        restored, _ = await invoke("回到旧轮")
        assert restored.history_window == old_window
        from satrap.core.framework.SessionManager import SessionConfigStore
        persisted = SessionConfigStore(database).get(old.session_id)
        assert persisted is not None and persisted.history_started_at == old_window.started_at
        assert "旧轮唯一内容" in str(script.requests[-1][0])
        assert "新轮唯一内容" not in str(script.requests[-1][0])
        assert len(script.requests) == 3
    finally:
        for entry in list(manager.pool.list_entries().values()):
            await manager._release_session_memory_async(entry.session)

@pytest.mark.asyncio
async def test_body_with_media_is_not_a_command_and_still_resolves(monkeypatch: pytest.MonkeyPatch):
    """正文含媒体组件时按普通消息处理: 保守拒绝不改变媒体链路"""
    adapter = RecorderAdapter()
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))
    calls: list[str] = []

    async def fake_resolve_attachments(event: MessageEvent, resolver: Any) -> list[Any]:
        calls.append("resolve_attachments")
        return []

    async def fake_resolve_media(event: MessageEvent, selection: Any) -> list[Any]:
        calls.append("resolve_media")
        return []

    monkeypatch.setattr("satrap.core.pipeline.scheduler.resolve_attachments", fake_resolve_attachments)
    monkeypatch.setattr("satrap.core.pipeline.scheduler.resolve_media", fake_resolve_media)

    with_media = _scheduler_event(
        adapter,
        [At(qq="bot"), Plain(text=" /help"), Image(file="https://cdn/a.png")],
        message_str="@bot /help[图片]",
    )
    assert extract_command_candidate(with_media) is None
    await scheduler.execute(with_media)
    assert calls == ["resolve_attachments", "resolve_media"]

@pytest.mark.asyncio
async def test_command_with_leading_reply_skips_resolution(monkeypatch: pytest.MonkeyPatch):
    """带首部引用(且引用链带图)的命令仍被识别, 且不触发引用, 附件, 媒体与下载"""
    adapter = RecorderAdapter(settings={"quote_lookup": True})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))
    calls: list[str] = []

    async def fake_resolve_quotes(event: MessageEvent) -> str:
        calls.append("resolve_quotes")
        return "unavailable"

    async def fake_resolve_attachments(event: MessageEvent, resolver: Any) -> list[Any]:
        calls.append("resolve_attachments")
        return []

    async def fake_resolve_media(event: MessageEvent, selection: Any) -> list[Any]:
        calls.append("resolve_media")
        return []

    async def fake_download_into(*args: Any, **kwargs: Any) -> Any:
        calls.append("download")
        raise AssertionError("命令路径不得发生媒体下载")

    monkeypatch.setattr("satrap.core.pipeline.scheduler.resolve_quotes", fake_resolve_quotes)
    monkeypatch.setattr("satrap.core.pipeline.scheduler.resolve_attachments", fake_resolve_attachments)
    monkeypatch.setattr("satrap.core.pipeline.scheduler.resolve_media", fake_resolve_media)
    monkeypatch.setattr("satrap.core.pipeline.media_resolve._download_into", fake_download_into)

    # Step.1 同为"前置引用 + @bot"且引用链带图, 但正文不是命令: 三个阶段照常执行
    plain_reply = _scheduler_event(
        adapter, [Reply(id="m0", chain=[Image(file="https://cdn/q.png")]), At(qq="bot"), Plain(text=" 你好")],
        message_str="@bot 你好",
    )
    await scheduler.execute(plain_reply)
    assert calls == ["resolve_quotes", "resolve_attachments", "resolve_media"]

    # Step.2 正文是命令: 引用链上的图文与附件都不再解析, 也不发生下载
    calls.clear()
    command_reply = _scheduler_event(
        adapter, [Reply(id="m0", chain=[Image(file="https://cdn/q.png")]), At(qq="bot"), Plain(text=" /help")],
        message_str="@bot /help",
    )
    await scheduler.execute(command_reply)

    assert len(manager.calls) == 2
    assert manager.calls[1].message == "/help"
    assert manager.calls[1].img_urls == []
    assert calls == []

@pytest.mark.asyncio
async def test_manual_and_deadline_events_are_not_commands():
    """面板手动唤醒与定时补偿的合成事件不做命令判定"""
    from satrap.core.pipeline.manual_wake import ManualWakeTicket
    from satrap.core.pipeline.wake_timers import DeadlineTicket

    adapter = RecorderAdapter(settings={"wake_mode": "frequency"})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    manual_event = _scheduler_event(adapter, [At(qq="bot"), Plain(text=" /help")], message_str="@bot /help")
    scheduler.manual_wakes.tickets[manual_event] = ManualWakeTicket(request_id="r1", status="pending")
    await scheduler.execute(manual_event)
    assert manager.calls[-1].message != "/help"

    deadline_event = _scheduler_event(adapter, [At(qq="bot"), Plain(text=" /help")], message_str="@bot /help")
    scheduler.wake_timers.tickets[deadline_event] = DeadlineTicket()
    await scheduler.execute(deadline_event)
    assert manager.calls[-1].message != "/help"

@pytest.mark.asyncio
async def test_unknown_command_still_reaches_model_without_window():
    """未知 /foo 不被平台层拒绝, 但该请求不含窗口块"""
    adapter = RecorderAdapter(settings={"wake_mode": "frequency"})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    pending = _scheduler_event(adapter, [Plain(text="闲聊")], sender_id="u1")
    scheduler.wake_window.observe(pending)

    unknown = _scheduler_event(adapter, [At(qq="bot"), Plain(text=" /foo")], message_str="@bot /foo")
    await scheduler.execute(unknown)

    assert len(manager.calls) == 1
    assert manager.calls[0].message == "/foo"

@pytest.mark.asyncio
async def test_group_command_without_mention_is_not_executed_or_rejected():
    """群内裸 /help 不唤醒也不外发, 与既有唤醒门行为一致"""
    adapter = RecorderAdapter()
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    bare = _scheduler_event(adapter, [Plain(text="/help")], message_str="/help")
    await scheduler.execute(bare)

    assert manager.calls == []
    assert adapter.sent == []

@pytest.mark.asyncio
async def test_wake_word_woken_slash_message_is_not_command():
    """由唤醒词唤醒的 /help 按普通消息处理"""
    adapter = RecorderAdapter(settings={"wake_words": ["小撒"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    event = _scheduler_event(adapter, [Plain(text="小撒 /help")], message_str="小撒 /help")
    await scheduler.execute(event)

    assert len(manager.calls) == 1
    assert manager.calls[0].message == "[用户 User (ID user-1), 消息 msg-user-1] 小撒 /help"

@pytest.mark.asyncio
async def test_private_command_has_no_mention_in_body():
    """私聊 /help 与 @bot /help 都执行, 且命令名与参数不含 self 提及"""
    adapter = RecorderAdapter()
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    await scheduler.execute(_scheduler_event(adapter, [Plain(text="/help")], private=True, message_str="/help"))
    await scheduler.execute(
        _scheduler_event(adapter, [At(qq="bot"), Plain(text=" /help")], private=True, message_str="@bot /help")
    )

    assert [call.message for call in manager.calls] == ["/help", "/help"]

@pytest.mark.asyncio
async def test_frozen_text_is_parsed_by_session_command_handler():
    """交接: 冻结正文进入会话后由 CommandHandler 解析出命令名与参数, 提及不进入参数"""
    adapter = RecorderAdapter()
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))
    handler = AsyncCommandHandler()
    seen: list[list[str]] = []

    async def goal_cmd(*args: str) -> str:
        seen.append(list(args))
        return "已设置目标"

    handler.register_command("goal", goal_cmd, intro="设置目标")
    await scheduler.execute(
        _scheduler_event(adapter, [At(qq="bot"), Plain(text=" /goal 修复 bug10")], message_str="@bot /goal 修复 bug10")
    )

    message = manager.calls[0].message
    assert message == "/goal 修复 bug10"
    name, args = handler._parse(message)
    assert (name, args) == ("goal", ["修复", "bug10"])
    assert handler._is_cmd(message) is True
    result, is_command = await handler.process_message(message)
    assert (result, is_command, seen) == ("已设置目标", True, [["修复", "bug10"]])
    assert not any("@" in arg for arg in seen[0])

@pytest.mark.asyncio
async def test_command_skips_model_stage_record():
    """命令未调用模型: 不产生 model 阶段记录, 命中与拒绝都由 projection 的原因码区分"""
    adapter = RecorderAdapter()
    manager = _FakeSessionManager(response="/help 回执")
    scheduler = PipelineScheduler(_as_session_manager(manager))

    command = _scheduler_event(adapter, [At(qq="bot"), Plain(text=" /help")], message_str="@bot /help")
    await scheduler.execute(command)

    records = scheduler.request_diagnostics.list(adapter.config.id)
    codes = [(str(record["stage"]), str(record["reason_code"])) for record in records]
    assert ("projection", "command_candidate") in codes
    assert all(str(record["stage"]) != "model" for record in records)
    assert all("模型输出" not in str(record["reason"]) for record in records)

def _feedback_texts(adapter: RecorderAdapter) -> list[str]:
    """错误反馈通道外发的文本"""
    return [
        "".join(getattr(component, "text", "") for component in message.components)
        for _, message, purpose in adapter.sent if purpose == "error_feedback"
    ]

def _protected_event(
    adapter: RecorderAdapter,
    body: str = " /approve mode full",
    *,
    sender_id: str = "user-1",
    private: bool = False,
) -> MessageEvent:
    """受保护命令事件"""
    return _scheduler_event(
        adapter, [At(qq="bot"), Plain(text=body)], message_str=f"@bot{body}", sender_id=sender_id, private=private,
    )

@pytest.mark.asyncio
async def test_operator_only_command_name_set_is_frozen():
    """受保护名集只有 approve 与 plan 两个名字"""
    assert OPERATOR_ONLY_COMMANDS == {"approve", "plan"}
    assert is_operator_only_command("approve") is True
    assert is_operator_only_command("plan") is True
    assert is_operator_only_command("goal") is False
    assert is_operator_only_command("memory") is False
    assert is_operator_only_command("approveX") is False

def test_operator_name_set_has_single_seam():
    """名集只在 command_entry.py 出现, 判定统一走 is_operator_only_command 接缝"""
    root = Path(__file__).resolve().parents[2] / "satrap"
    references = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "OPERATOR_ONLY_COMMANDS" in path.read_text(encoding="utf-8")
    )
    assert references == ["core/pipeline/command_entry.py"]

@pytest.mark.asyncio
async def test_non_operator_is_refused_for_protected_commands():
    """非名单成员执行 /approve 时被拒绝: 不进会话, 不调模型, 收到固定文案"""
    adapter = RecorderAdapter(settings={"command_operators": ["owner-9"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    await scheduler.execute(_protected_event(adapter))

    assert manager.calls == []
    assert _feedback_texts(adapter) == ["该命令仅允许已授权操作员执行。"]
    records = scheduler.request_diagnostics.list(adapter.config.id)
    assert ("projection", "operator_required") in [(str(record["stage"]), str(record["reason_code"])) for record in records]
    assert all(str(record["stage"]) != "model" for record in records)

@pytest.mark.asyncio
async def test_plan_off_is_refused_for_non_operator():
    """放宽写操作限制的 /plan off 与 /approve 同级保护"""
    adapter = RecorderAdapter(settings={"command_operators": ["owner-9"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    await scheduler.execute(_protected_event(adapter, " /plan off"))

    assert manager.calls == []
    assert _feedback_texts(adapter) == ["该命令仅允许已授权操作员执行。"]

@pytest.mark.asyncio
async def test_operator_passes_both_protected_commands():
    """名单内成员可执行 /approve 与 /plan"""
    adapter = RecorderAdapter(settings={"command_operators": ["user-1"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    await scheduler.execute(_protected_event(adapter, " /approve mode full"))
    await scheduler.execute(_protected_event(adapter, " /plan on"))

    assert [call.message for call in manager.calls] == ["/approve mode full", "/plan on"]
    assert _feedback_texts(adapter) == []

@pytest.mark.asyncio
@pytest.mark.parametrize("operators", [None, [], ["   "], "user-1", ["owner-9", "USER-1"]])
async def test_operator_list_fails_closed(operators: Any):
    """名单缺失/为空/取值非法或大小写不匹配时一律拒绝, 不存在空名单表示不限制的语义"""
    settings: dict[str, Any] = {} if operators is None else {"command_operators": operators}
    adapter = RecorderAdapter(settings=settings)
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    await scheduler.execute(_protected_event(adapter))

    assert manager.calls == []
    assert _feedback_texts(adapter) == ["该命令仅允许已授权操作员执行。"]

@pytest.mark.asyncio
async def test_blank_entry_next_to_match_does_not_block():
    """名单项级校验由配置校验器负责: 与匹配项并存的空白项不影响该成员通过"""
    adapter = RecorderAdapter(settings={"command_operators": ["user-1", "  "]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    await scheduler.execute(_protected_event(adapter))

    assert [call.message for call in manager.calls] == ["/approve mode full"]

@pytest.mark.asyncio
async def test_admin_role_does_not_grant_operator_rights():
    """平台管理员身份不等于 Satrap 操作员"""
    adapter = RecorderAdapter(settings={"command_operators": ["owner-9"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    event = _protected_event(adapter)
    event.role = "admin"
    assert event.is_admin() is True
    await scheduler.execute(event)

    assert manager.calls == []
    assert _feedback_texts(adapter) == ["该命令仅允许已授权操作员执行。"]

@pytest.mark.asyncio
async def test_management_origin_is_not_subject_to_operator_gate():
    """管理面来源不进入平台授权判定: 已过管理面认证, 不继承为平台操作员"""
    adapter = RecorderAdapter(settings={"command_operators": ["owner-9"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    event = _protected_event(adapter)
    event._call_origin = replace(event.call_origin, actor_id="panel-user", actor_kind="management")
    await scheduler.execute(event)

    assert [call.message for call in manager.calls] == ["/approve mode full"]

@pytest.mark.asyncio
async def test_other_commands_are_not_gated():
    """边界钉住: /goal 与 /memory 本轮不加门, 以免保护范围被悄悄扩大"""
    adapter = RecorderAdapter(settings={"command_operators": ["owner-9"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    await scheduler.execute(_protected_event(adapter, " /goal 修复 bug10"))
    await scheduler.execute(_protected_event(adapter, " /memory clear"))

    assert [call.message for call in manager.calls] == ["/goal 修复 bug10", "/memory clear"]
    assert _feedback_texts(adapter) == []

@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["approve", "plan"])
async def test_disabled_command_is_still_refused_at_platform_entry(name: str):
    """P2: 会话停用状态属会话实例, 平台层仍按名字拒绝, 不因停用变成可执行"""
    adapter = RecorderAdapter(settings={"command_operators": ["owner-9"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))
    handler = AsyncCommandHandler()
    handler.register_command(name, lambda: None, intro="受保护命令")
    handler.disable_command(name)
    assert handler.is_command_enabled(name) is False

    await scheduler.execute(_protected_event(adapter, f" /{name}"))

    assert manager.calls == []
    assert _feedback_texts(adapter) == ["该命令仅允许已授权操作员执行。"]

@pytest.mark.asyncio
async def test_private_chat_protected_command_is_gated():
    """私聊中的受保护命令同样受判"""
    adapter = RecorderAdapter(settings={"command_operators": ["owner-9"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    await scheduler.execute(_protected_event(adapter, " /approve", private=True))

    assert manager.calls == []
    assert _feedback_texts(adapter) == ["该命令仅允许已授权操作员执行。"]

@pytest.mark.asyncio
async def test_unconfigured_operator_name_without_prefix_is_not_gated():
    """判定依据是命令名而不是正文文本: 无前缀的同一文本仍走普通管线"""
    adapter = RecorderAdapter(settings={"command_operators": ["owner-9"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    await scheduler.execute(
        _scheduler_event(adapter, [At(qq="bot"), Plain(text=" 帮我 approve 一下")], message_str="@bot 帮我 approve 一下")
    )

    assert len(manager.calls) == 1
    assert "approve" in manager.calls[0].message
    assert _feedback_texts(adapter) == []

@pytest.mark.asyncio
async def test_operator_refusal_is_rate_limited():
    """连续拒绝受同一限流键约束, 第二次先被限流"""
    from satrap.core.pipeline.rate_limiter import RateLimiter

    adapter = RecorderAdapter(settings={"command_operators": ["owner-9"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager), rate_limiter=RateLimiter(rate=1.0, burst=1))

    await scheduler.execute(_protected_event(adapter))
    await scheduler.execute(_protected_event(adapter, " /plan off", sender_id="user-2"))

    assert manager.calls == []
    assert _feedback_texts(adapter)[0] == "该命令仅允许已授权操作员执行。"
    assert "频率过高" in _feedback_texts(adapter)[1]
    codes = [str(record["reason_code"]) for record in scheduler.request_diagnostics.list(adapter.config.id)]
    assert "operator_required" in codes
    assert "rate_limited" in codes

@pytest.mark.asyncio
async def test_command_name_extraction_matches_session_parse():
    """平台入口取命令名与会话层 _parse 同一口径"""
    from satrap.core.pipeline.command_entry import command_name_from_text

    handler = AsyncCommandHandler()
    assert command_name_from_text(" /approve mode full ") == "approve"
    assert command_name_from_text("/plan") == "plan"
    assert command_name_from_text("/") == ""
    assert command_name_from_text("approve") == ""
    assert handler._parse(" /approve   mode  full ") [0] == command_name_from_text(" /approve   mode  full ")

class FakeUserManager:
    def __init__(self, sessions: list[str] | None = None):
        self.sessions = list(sessions or [])
        self.bound: list[tuple[str, str]] = []

    def get_user_session_ids(self, user_id: str):
        return list(self.sessions)

    def bind_session(self, user_id: str, session_id: str):
        self.bound.append((user_id, session_id))
        if session_id not in self.sessions:
            self.sessions.append(session_id)
        return True

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_session_command_contract(asynchronous: bool, monkeypatch: pytest.MonkeyPatch):
    """同步与异步会话命令注册/执行契约一致, 异步侧延续 history/new/switch"""
    if asynchronous:
        handler = AsyncCommandHandler()

        async def about() -> str:
            return "custom"

        handler.register_command("about", about, "预置")
        session = AsyncSession("chat:misskey:user1:old", command_handler=handler)
        assert await session.cmd_process("/about") == ("custom", True)

        user_manager = FakeUserManager(
            ["chat:misskey:user1:old", "chat:misskey:user1:next"],
        )
        monkeypatch.setattr(session, "_user_manager", user_manager)
        history = await cmd_history_async(session)
        new_action = await cmd_new_async(session)
        switch_action = await cmd_switch_async(session, "chat:misskey:user1:next")
        assert isinstance(history, str)
        assert "chat:misskey:user1:old" in history
        assert isinstance(new_action, CommandAction)
        assert new_action.target_session_id is not None
        assert new_action.target_session_id.startswith("chat:misskey:user1:")
        assert isinstance(switch_action, CommandAction)
        assert switch_action.target_session_id == "chat:misskey:user1:next"
    else:
        handler = _session_commands_CommandHandler()
        handler.register_command("about", lambda: "custom", "自定义")
        session = Session("chat:misskey:user1", command_handler=handler)
        result, is_command = session.cmd_process("/about")
        assert is_command is True
        assert result == "custom"

def test_sync_new_command_defers_binding_until_host_success(monkeypatch: pytest.MonkeyPatch):
    session = Session("chat:misskey:user1:old")
    user_manager = FakeUserManager(["chat:misskey:user1:old"])
    monkeypatch.setattr(session, "_user_manager", user_manager)

    result = cmd_new(session)

    assert isinstance(result, CommandAction)
    assert result.action == "switch"
    assert result.target_session_id is not None
    assert result.target_session_id.startswith("chat:misskey:user1:")
    assert result.target_session_id != session.session_id
    assert result.message == "已开始新对话（旧对话可通过 /history 查看和切换）"
    assert user_manager.bound == []

def test_sync_switch_validates_bound_contexts(monkeypatch: pytest.MonkeyPatch):
    session = Session("chat:misskey:user1")
    monkeypatch.setattr(
        session,
        "_user_manager",
        FakeUserManager(["chat:misskey:user1", "chat:misskey:user1:next"]),
    )

    assert cmd_switch(session, "chat:misskey:user1:missing") == (
        "对话不存在: chat:misskey:user1:missing\n请通过 /history 查看可用对话"
    )
    result = cmd_switch(session, "chat:misskey:user1:next")

    assert isinstance(result, CommandAction)
    assert result.action == "switch_session"
    assert result.target_session_id == "chat:misskey:user1:next"

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_platform_session_commands_plugin_builds_bound_commands(
    asynchronous: bool,
    monkeypatch: pytest.MonkeyPatch,
):
    """平台命令插件以当前会话为作用域构建同步/异步命令 (镜像)"""
    plugin_dir = (
        Path(__file__).resolve().parents[2]
        / "satrap"
        / "expend"
        / "plugins"
        / "session_commands"
    )
    if asynchronous:
        session = AsyncSession("onebot-edictum:onebot-platform:user1:old")
        sync_commands, async_commands = collect_commands(
            plugin_dir,
            "session_commands",
            cast(Any, session),
            {"about_text": "异步自定义说明"},
        )
        assert sync_commands == {}
        assert set(async_commands) == {"new", "history", "switch", "about"}
        assert await async_commands["about"]() == "异步自定义说明"
    else:
        session = Session("onebot-edictum:onebot-platform:user1:old")
        monkeypatch.setattr(session, "_user_manager", FakeUserManager([session.session_id]))
        sync_commands, async_commands = collect_commands(
            plugin_dir,
            "session_commands",
            cast(Any, session),
            {"about_text": "自定义平台说明"},
        )
        result = sync_commands["new"]()
        about = sync_commands["about"]()
        assert set(sync_commands) == {"new", "history", "switch", "about"}
        assert async_commands == {}
        assert about == "自定义平台说明"
        assert isinstance(result, CommandAction)
        assert result.target_session_id is not None
        assert result.target_session_id.startswith("onebot-edictum:onebot-platform:user1:")

class CountingTool(Tool):
    def __init__(self):
        super().__init__(
            tool_name="counting",
            description="计数工具",
            params_dict={"value": ("number", "输入值")},
        )
        self.calls = 0

    def execute(self, value: int):
        self.calls += 1
        return {"value": value}

class AsyncCountingTool(AsyncTool):
    def __init__(self):
        super().__init__(
            tool_name="async_counting",
            description="异步计数工具",
            params_dict={"value": ("number", "输入值")},
        )
        self.calls = 0

    async def execute(self, value: int):
        self.calls += 1
        return {"value": value}

class RaisingTool(Tool):
    def __init__(self):
        super().__init__(
            tool_name="raising",
            description="异常工具",
            params_dict={"value": ("number", "输入值")},
        )
        self.calls = 0

    def execute(self, value: int):
        self.calls += 1
        raise RuntimeError(f"boom {value}")

class AsyncRaisingTool(AsyncTool):
    def __init__(self):
        super().__init__(
            tool_name="async_raising",
            description="异步异常工具",
            params_dict={"value": ("number", "输入值")},
        )
        self.calls = 0

    async def execute(self, value: int):
        self.calls += 1
        raise RuntimeError(f"async boom {value}")

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_tools_manager_can_disable_and_enable_tool(asynchronous: bool):
    """启停工具: 定义隐藏/执行拒绝/恢复 (同步与异步镜像)"""
    if asynchronous:
        manager: Any = AsyncToolsManager()
        tool = AsyncCountingTool()
        name = "async_counting"
    else:
        manager = ToolsManager()
        tool = CountingTool()
        name = "counting"
    manager.register_tool(tool)

    assert manager.is_tool_enabled(name) is True
    assert len(manager.get_tools_definitions()) == 1
    assert manager.disable_tool(name) is True
    assert name in manager.tools
    assert manager.is_tool_enabled(name) is False
    assert manager.get_tools_definitions() == []

    result = await manager.execute_tool(name, {"value": 1}) if asynchronous else manager.execute_tool(name, {"value": 1})

    assert result["error"] == f"工具 {name} 已禁用"
    assert result["ok"] is False
    assert result["error_type"] == "disabled"
    assert result["tool_name"] == name
    assert tool.calls == 0

    assert manager.enable_tool(name) is True
    assert len(manager.get_tools_definitions()) == 1
    restored = await manager.execute_tool(name, {"value": 2}) if asynchronous else manager.execute_tool(name, {"value": 2})
    assert restored == {"value": 2}
    assert tool.calls == 1

def test_tools_manager_bulk_enable_disable_and_unregister_regression():
    manager = ToolsManager()
    tool = CountingTool()
    manager.register_tool(tool)

    assert manager.disable_tool("missing") is False
    assert manager.enable_tool("missing") is False
    assert manager.disable_all_tools() is True
    assert manager.get_tools_definitions() == []
    assert manager.enable_all_tools() is True
    assert len(manager.get_tools_definitions()) == 1

    assert manager.unregister_tool("counting") is True
    assert "counting" not in manager.tools
    assert manager.unregister_all_tools() is False

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_tools_manager_structured_errors_and_exception_isolated(asynchronous: bool):
    """非法调用返回结构化错误且不执行, 工具异常被隔离为 execution_error (同步与异步镜像)"""
    if asynchronous:
        manager: Any = AsyncToolsManager()
        counting = AsyncCountingTool()
        raising = AsyncRaisingTool()
        name_c, name_r = "async_counting", "async_raising"
    else:
        manager = ToolsManager()
        counting = CountingTool()
        raising = RaisingTool()
        name_c, name_r = "counting", "raising"
    manager.register_tool(counting)
    manager.register_tool(raising)

    async def call(tool_name: str, arguments: Any) -> dict[str, Any]:
        if asynchronous:
            return await manager.execute_tool(tool_name, arguments)
        return manager.execute_tool(tool_name, arguments)

    missing = await call("missing", {})
    assert missing["error_type"] == "not_found"
    assert missing["tool_name"] == "missing"

    invalid_name = await call("", {})
    assert invalid_name["error_type"] == "invalid_tool_call"

    invalid_args = await call(name_c, cast(Any, "not a dict"))
    assert invalid_args["error_type"] == "invalid_arguments"
    assert counting.calls == 0

    none_args = await call(name_c, None)
    assert none_args["error_type"] == "invalid_arguments"
    assert counting.calls == 0

    boom = await call(name_r, {"value": 3})
    assert boom["ok"] is False
    assert boom["error_type"] == "execution_error"
    assert boom["tool_name"] == name_r
    assert boom["error"] == "工具执行失败: RuntimeError"
    assert "boom" not in boom["error"]
    assert raising.calls == 1

def test_tool_argument_log_summary_redacts_nested_credentials() -> None:
    """工具异常日志参数摘要不得包含凭据原值"""
    summary = _summarize_arguments({
        "api_key": "secret-key",
        "nested": {"access-token": "secret-token", "path": "demo.txt"},
    })

    assert "secret-key" not in summary
    assert "secret-token" not in summary
    assert summary.count("********") == 2
    assert "demo.txt" in summary

def test_execute_tool_call_handles_malformed_call_info_without_raising():
    manager = ToolsManager()
    manager.register_tool(CountingTool())

    tool_message, tool_result = manager.execute_tool_call(cast(Any, "bad call"))
    assert tool_message["id"] == ""
    assert tool_message["function"]["name"] == ""
    assert tool_result["error_type"] == "invalid_tool_call"

    tool_message, tool_result = manager.execute_tool_call(
        {"id": "call_bad", "arguments": {"value": 1}}
    )
    assert tool_message["id"] == "call_bad"
    assert tool_result["error_type"] == "invalid_tool_call"

    tool_message, tool_result = manager.execute_tool_call(
        {"id": "call_bad_args", "name": "counting", "arguments": ["value", 1]}
    )
    assert tool_message["id"] == "call_bad_args"
    assert tool_result["error_type"] == "invalid_arguments"



@pytest.mark.asyncio
@pytest.mark.parametrize("command, owner", [("/approve mode auto", "permissions"), ("/plan off", "satrap_coding")])
@pytest.mark.parametrize("granted_plugin", ["permissions", "satrap_coding"])
async def test_command_owner_administrator_passes_operator_precheck(monkeypatch, command, owner, granted_plugin):
    """前置门按命令所属插件核验管理范围, 不借用其他插件的管理员身份"""
    from types import SimpleNamespace
    from satrap.core import platform
    adapter = RecorderAdapter(settings={"command_operators": []})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))
    reviewed = []
    def resolve(origin, plugin):
        reviewed.append((origin.actor_id, plugin))
        return SimpleNamespace(allowed=origin.actor_id == "user-1" and plugin == granted_plugin)
    monkeypatch.setattr(platform, "current_adapter_manager", lambda: SimpleNamespace(administrator_service=SimpleNamespace(resolve=resolve)))
    await scheduler.execute(_protected_event(adapter, command))
    assert [call.message for call in manager.calls] == ([command] if owner == granted_plugin else [])
    assert reviewed == [("user-1", owner)]
    assert bool(_feedback_texts(adapter)) == (owner != granted_plugin)
