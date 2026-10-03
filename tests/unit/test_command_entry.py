"""
平台命令入口单元测试

覆盖:
- 候选提取: 寻址段剥离, 群与私聊差异, 正文渲染的保守拒绝, 前缀单一来源
- 平台命令入口: 冻结文本进入会话, 窗口与投影被跳过, 合成事件不参与判定
- 群与私聊命令语义: 唤醒门不被绕过, 未知命令失去窗口上下文
- operator 授权: 名集单点, fail-closed, 与平台角色无关
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import inspect
from pathlib import Path
import pytest
from typing import Any, cast

from satrap.core.framework.command import AsyncCommandHandler, CommandHandler
from satrap.core.framework.command.base import DEFAULT_COMMAND_PREFIX
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.pipeline.command_entry import (
    OPERATOR_ONLY_COMMANDS,
    extract_command_candidate,
    is_operator_only_command,
)
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.components import At, AtAll, BaseMessageComponent, Image, Plain, Reply
from satrap.core.platform.event import MessageChain, MessageEvent, PlatformMetadata
from satrap.core.platform import PlatformAdapter, PlatformConfig
from satrap.core.type import Group, MessageMember, PlatformMessage, PlatformMessageType
from satrap.core.framework.providers import BindingState, BindingStatus


class _RunnableRegistry:
    """绑定判定恒为可运行的会话定义注册表替身"""

    @staticmethod
    def binding_status(*_args: object) -> BindingStatus:
        """
        恒定答复可运行

        返回:
        - BindingStatus: 可运行
        """
        return BindingStatus(BindingState.RUNNABLE)


def _event(
    components: list[BaseMessageComponent],
    *,
    private: bool = False,
    self_id: str = "bot",
    message_str: str = "",
    sender_id: str = "user-1",
) -> MessageEvent:
    """
    构造只带顶层组件的事件

    参数:
    - components: 顶层消息组件
    - private: 是否为私聊
    - self_id: 机器人自身平台 ID
    - message_str: 平台侧渲染文本
    - sender_id: 发送者 ID

    返回:
    - MessageEvent: 可交给平台命令入口判定的事件
    """
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


# ================= 候选提取 =================


def test_leading_self_mention_is_stripped():
    """群内前置 @bot 的命令剥离提及后得到正文"""
    event = _event([At(qq="bot"), Plain(text=" /help")], message_str="@bot /help")
    assert extract_command_candidate(event) == "/help"


def test_body_not_starting_with_prefix_is_not_command():
    """正文不以前缀开头时不是命令候选"""
    event = _event([At(qq="bot"), Plain(text=" 你好")], message_str="@bot 你好")
    assert extract_command_candidate(event) is None


def test_addressing_run_allows_repeated_mention_and_blank_text():
    """相邻提及与全空白文本都属于寻址段"""
    cases: tuple[list[BaseMessageComponent], ...] = (
        [At(qq="bot"), At(qq="bot"), Plain(text=" /help")],
        [At(qq="bot"), Plain(text="   /help")],
        [At(qq="bot"), Plain(text=" "), At(qq="bot"), Plain(text=" /help")],
    )
    for components in cases:
        assert extract_command_candidate(_event(components)) == "/help"


def test_trailing_self_mention_is_kept_only_in_private_chat():
    """尾随 self 提及不剥离: 群内不构成命令, 私聊保留在正文里"""
    private = _event([Plain(text="/help "), At(qq="bot")], private=True)
    assert extract_command_candidate(private) == "/help @bot"
    assert extract_command_candidate(_event([Plain(text="/help "), At(qq="bot")])) is None


def test_mention_of_others_is_not_addressing():
    """提及他人不构成寻址"""
    assert extract_command_candidate(_event([At(qq="other"), Plain(text=" /help")])) is None


def test_at_all_is_not_self_mention():
    """全体成员提及不构成寻址"""
    assert extract_command_candidate(_event([AtAll(), Plain(text=" /help")])) is None


def test_group_requires_explicit_mention():
    """群内没有前置 @bot 时不是命令, 私聊不要求"""
    assert extract_command_candidate(_event([Plain(text="/help")])) is None
    assert extract_command_candidate(_event([Plain(text="/help")], private=True)) == "/help"


def test_leading_reply_marker_is_addressing():
    """首部引用标记不属于命令正文"""
    event = _event([Reply(id="m0"), At(qq="bot"), Plain(text=" /help")])
    assert extract_command_candidate(event) == "/help"


def test_non_text_component_in_body_is_rejected():
    """正文含不可渲染组件时保守拒绝"""
    event = _event([At(qq="bot"), Plain(text=" /help"), Image(file="https://cdn/a.png")])
    assert extract_command_candidate(event) is None


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


def test_prefix_has_single_source():
    """前缀单一来源: 处理器默认值与平台入口判定取自同一常量"""
    assert inspect.signature(AsyncCommandHandler.__init__).parameters["cmd_prefix"].default is DEFAULT_COMMAND_PREFIX
    assert inspect.signature(CommandHandler.__init__).parameters["cmd_prefix"].default is DEFAULT_COMMAND_PREFIX
    assert AsyncCommandHandler().prefix == DEFAULT_COMMAND_PREFIX
    assert CommandHandler().prefix == DEFAULT_COMMAND_PREFIX


def test_prefix_constant_drives_candidate(monkeypatch: pytest.MonkeyPatch):
    """改写前缀常量后平台入口判定同步变化"""
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


# ================= 平台入口集成 =================


class _RecorderAdapter(PlatformAdapter):
    """记录 send_message 调用的测试适配器"""

    def __init__(self, adapter_id: str = "rec1", settings: dict[str, Any] | None = None):
        self.config = PlatformConfig(id=adapter_id, type="rec", settings=settings or {})
        self.sent: list[tuple[str, MessageChain, str]] = []

    async def run(self) -> None:
        return None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(name=self.config.id, id=self.config.id)

    async def send_message(
        self, session_id: str, message: MessageChain, *, request_id: str = "",
        purpose: str = "business", require_tracking: bool = False,
    ) -> Any:
        self.sent.append((session_id, message, purpose))
        return None


class _FakeSessionManager:
    """记录 handle_call_async 调用的假 SessionManager"""

    class_cfg_mgr = None
    provider_registry = _RunnableRegistry()

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
    adapter: _RecorderAdapter,
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
    adapter = _RecorderAdapter(settings={"context_scope": "group"})
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
    adapter = _RecorderAdapter(settings={"wake_mode": "frequency", "context_scope": "group"})
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


@pytest.mark.asyncio
async def test_body_with_media_is_not_a_command_and_still_resolves(monkeypatch: pytest.MonkeyPatch):
    """正文含媒体组件时按普通消息处理: 保守拒绝不改变媒体链路"""
    adapter = _RecorderAdapter()
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
    adapter = _RecorderAdapter(settings={"quote_lookup": True})
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

    adapter = _RecorderAdapter(settings={"wake_mode": "frequency"})
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
    adapter = _RecorderAdapter(settings={"wake_mode": "frequency"})
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
    adapter = _RecorderAdapter()
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    bare = _scheduler_event(adapter, [Plain(text="/help")], message_str="/help")
    await scheduler.execute(bare)

    assert manager.calls == []
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_wake_word_woken_slash_message_is_not_command():
    """由唤醒词唤醒的 /help 按普通消息处理"""
    adapter = _RecorderAdapter(settings={"wake_words": ["小撒"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    event = _scheduler_event(adapter, [Plain(text="小撒 /help")], message_str="小撒 /help")
    await scheduler.execute(event)

    assert len(manager.calls) == 1
    assert manager.calls[0].message == "[用户 User (ID user-1), 消息 msg-user-1] 小撒 /help"


@pytest.mark.asyncio
async def test_private_command_has_no_mention_in_body():
    """私聊 /help 与 @bot /help 都执行, 且命令名与参数不含 self 提及"""
    adapter = _RecorderAdapter()
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
    adapter = _RecorderAdapter()
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
    adapter = _RecorderAdapter()
    manager = _FakeSessionManager(response="/help 回执")
    scheduler = PipelineScheduler(_as_session_manager(manager))

    command = _scheduler_event(adapter, [At(qq="bot"), Plain(text=" /help")], message_str="@bot /help")
    await scheduler.execute(command)

    records = scheduler.request_diagnostics.list(adapter.config.id)
    codes = [(str(record["stage"]), str(record["reason_code"])) for record in records]
    assert ("projection", "command_candidate") in codes
    assert all(str(record["stage"]) != "model" for record in records)
    assert all("模型输出" not in str(record["reason"]) for record in records)


# ================= operator 授权 =================


def _feedback_texts(adapter: _RecorderAdapter) -> list[str]:
    """错误反馈通道外发的文本"""
    return [
        "".join(getattr(component, "text", "") for component in message.components)
        for _, message, purpose in adapter.sent if purpose == "error_feedback"
    ]


def _protected_event(
    adapter: _RecorderAdapter,
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
    adapter = _RecorderAdapter(settings={"command_operators": ["owner-9"]})
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
    adapter = _RecorderAdapter(settings={"command_operators": ["owner-9"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    await scheduler.execute(_protected_event(adapter, " /plan off"))

    assert manager.calls == []
    assert _feedback_texts(adapter) == ["该命令仅允许已授权操作员执行。"]


@pytest.mark.asyncio
async def test_operator_passes_both_protected_commands():
    """名单内成员可执行 /approve 与 /plan"""
    adapter = _RecorderAdapter(settings={"command_operators": ["user-1"]})
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
    adapter = _RecorderAdapter(settings=settings)
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    await scheduler.execute(_protected_event(adapter))

    assert manager.calls == []
    assert _feedback_texts(adapter) == ["该命令仅允许已授权操作员执行。"]


@pytest.mark.asyncio
async def test_blank_entry_next_to_match_does_not_block():
    """名单项级校验由配置校验器负责: 与匹配项并存的空白项不影响该成员通过"""
    adapter = _RecorderAdapter(settings={"command_operators": ["user-1", "  "]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    await scheduler.execute(_protected_event(adapter))

    assert [call.message for call in manager.calls] == ["/approve mode full"]


@pytest.mark.asyncio
async def test_admin_role_does_not_grant_operator_rights():
    """平台管理员身份不等于 Satrap 操作员"""
    adapter = _RecorderAdapter(settings={"command_operators": ["owner-9"]})
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
    adapter = _RecorderAdapter(settings={"command_operators": ["owner-9"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    event = _protected_event(adapter)
    event._call_origin = replace(event.call_origin, actor_id="panel-user", actor_kind="management")
    await scheduler.execute(event)

    assert [call.message for call in manager.calls] == ["/approve mode full"]


@pytest.mark.asyncio
async def test_other_commands_are_not_gated():
    """边界钉住: /goal 与 /memory 本轮不加门, 以免保护范围被悄悄扩大"""
    adapter = _RecorderAdapter(settings={"command_operators": ["owner-9"]})
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
    adapter = _RecorderAdapter(settings={"command_operators": ["owner-9"]})
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
    adapter = _RecorderAdapter(settings={"command_operators": ["owner-9"]})
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))

    await scheduler.execute(_protected_event(adapter, " /approve", private=True))

    assert manager.calls == []
    assert _feedback_texts(adapter) == ["该命令仅允许已授权操作员执行。"]


@pytest.mark.asyncio
async def test_unconfigured_operator_name_without_prefix_is_not_gated():
    """判定依据是命令名而不是正文文本: 无前缀的同一文本仍走普通管线"""
    adapter = _RecorderAdapter(settings={"command_operators": ["owner-9"]})
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

    adapter = _RecorderAdapter(settings={"command_operators": ["owner-9"]})
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

