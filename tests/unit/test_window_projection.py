"""群聊文字, 图片及昵称快照进入同一次模型输入的回归测试"""
import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from satrap.core.components import Image
from satrap.core.framework.providers import BindingState, BindingStatus
from satrap.core.pipeline.input_projection import project_input, select_media, select_window_media
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.pipeline.wake_window import PendingImage, WakeWindow
from satrap.core.platform import PlatformConfig
from satrap.core.platform.event import MessageEvent
from satrap.core.platform.identity import BotIdentity
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.type import UserCall


class RunnableRegistry:
    """返回可运行会话定义的测试注册表"""

    @staticmethod
    def binding_status(*_args: object) -> BindingStatus:
        """
        返回可运行的绑定状态

        参数:
        - _args: 测试中的任意绑定参数

        返回:
        - BindingStatus: 可运行
        """
        return BindingStatus(BindingState.RUNNABLE)


def runtime(settings: dict[str, Any] | None = None) -> tuple[OneBotAdapter, PipelineScheduler, AsyncMock]:
    """
    构造使用真实 OneBot 解析和调度器的本地测试运行时

    参数:
    - settings: 覆盖默认平台设置, 缺省时使用 explicit

    返回:
    - tuple: 平台适配器, 调度器与记录会话调用的替身
    """
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={"self_id": "10", **(settings or {})}))
    adapter.started = True
    adapter._bot = AsyncMock()
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
    manager.handle_call_async.return_value = ""
    users = Mock()
    users.resolve_session.return_value = "test-session"
    return adapter, PipelineScheduler(manager, user_manager=users), manager


async def incoming(
    adapter: OneBotAdapter, number: int, segments: list[dict[str, Any]], actor: int = 30,
    sender: dict[str, Any] | None = None, group: int = 20,
) -> MessageEvent:
    """
    由平台入站入口创建事件, 避免绕过唤醒和路由设置

    参数:
    - adapter: 真实解析消息的平台适配器
    - number: 去重用消息 ID
    - segments: OneBot 消息组件
    - actor: 发送者 ID, 默认 30
    - sender: 昵称和群名片字段, 缺省时只有 ID
    - group: 群 ID, 默认 20

    返回:
    - MessageEvent: 已入队的真实平台事件
    """
    await adapter._handle_group_message({"self_id": 10, "group_id": group, "user_id": actor,
        "message_id": number, "message_type": "group", "message": segments,
        "sender": sender or {"user_id": actor}})
    return adapter._event_queue.get_nowait()


def text(value: str) -> dict[str, Any]:
    """
    构造 OneBot 文字段

    参数:
    - value: 正文

    返回:
    - dict: 平台文字组件
    """
    return {"type": "text", "data": {"text": value}}


def picture(name: str) -> dict[str, Any]:
    """
    构造保留原始图片标识的 OneBot 图片段

    参数:
    - name: 图片来源名

    返回:
    - dict: 同时含原始 file 和 URL 的平台图片组件
    """
    return {"type": "image", "data": {"file": f"{name}.image", "url": f"https://cdn/{name}.png"}}


AT = {"type": "at", "data": {"qq": "10"}}


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    替换真实图片下载, 保留文件创建和清理路径

    参数:
    - monkeypatch: pytest 替换工具

    返回:
    - list[str]: 按顺序记录的下载地址
    """
    urls: list[str] = []

    async def download(url: str, *_args: object, **_kwargs: object) -> bytes:
        """记录下载地址并返回本地测试图片字节"""
        urls.append(url)
        return b"\x89PNG\r\n\x1a\n" + bytes(32)

    monkeypatch.setattr("satrap.core.pipeline.media_resolve._download", download)
    return urls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["explicit", "frequency", "necessity"])
async def test_unwoken_text_and_image_then_question_share_one_input(mode: str, downloads: list[str]):
    adapter, scheduler, manager = runtime({"wake_mode": mode, "wake_message_threshold": 32,
        "wake_score_threshold": 1, "wake_cooldown": 0})
    first = await incoming(adapter, 1, [text("图前"), picture("old"), text("图后")],
                           sender={"user_id": 30, "nickname": "小明"})
    await scheduler.execute(first)
    assert not downloads
    manager.handle_call_async.assert_not_awaited()
    snapshot = scheduler.wake_window.peek(first)
    assert snapshot[0].parts == ("图前", PendingImage("old.image", "https://cdn/old.png"), "图后")
    cast(Image, first.get_messages()[1]).url = "https://cdn/mutated.png"
    first.platform_message.sender.nickname = "后来的名字"
    files: list[str] = []

    async def model(call: UserCall) -> str:
        """确认模型调用时文件可读, 同时捕获本轮输入"""
        files.extend(call.img_urls or [])
        assert all(Path(file).exists() for file in files)
        assert call.message is not None
        assert "[用户 小明 (ID 30), 消息 1]" in call.message
        assert "图前[图片 1]图后" in call.message
        assert call.message.count("这张图是什么") == 1
        assert "后来的名字" not in call.message
        assert "https://cdn/" not in call.message
        return ""

    manager.handle_call_async.side_effect = model
    second = await incoming(adapter, 2, [AT, text("这张图是什么")])
    await scheduler.execute(second)
    manager.handle_call_async.assert_awaited_once()
    assert downloads == ["https://cdn/old.png"]
    assert files and all(not Path(file).exists() for file in files)
    assert scheduler.wake_window.peek(second) == ()


@pytest.mark.asyncio
async def test_pure_at_consumes_prior_image_only_message(downloads: list[str]):
    adapter, scheduler, manager = runtime()
    await scheduler.execute(await incoming(adapter, 1, [picture("only")]))
    await scheduler.execute(await incoming(adapter, 2, [AT]))
    manager.handle_call_async.assert_awaited_once()
    call = manager.handle_call_async.await_args.args[0]
    assert len(call.img_urls) == 1 and "[图片 1]" in call.message
    assert downloads == ["https://cdn/only.png"]


@pytest.mark.asyncio
async def test_current_media_then_newest_history_use_shared_budget(downloads: list[str]):
    adapter, scheduler, manager = runtime({"input_media_limit": 2})
    await scheduler.execute(await incoming(adapter, 1, [picture("old")]))
    await scheduler.execute(await incoming(adapter, 2, [picture("recent")]))
    await scheduler.execute(await incoming(adapter, 3, [AT, picture("current"), text("比较一下")]))
    call = manager.handle_call_async.await_args.args[0]
    assert downloads == ["https://cdn/current.png", "https://cdn/recent.png"]
    assert len(call.img_urls) == 2
    assert "消息 1] [图片未纳入本次输入]" in call.message
    assert "消息 2] [图片 2]" in call.message


@pytest.mark.asyncio
async def test_same_image_in_current_and_window_is_downloaded_once(downloads: list[str]):
    adapter, scheduler, manager = runtime({"input_media_limit": 1})
    await scheduler.execute(await incoming(adapter, 1, [picture("same")]))
    await scheduler.execute(await incoming(adapter, 2, [AT, picture("same"), text("看图")]))
    call = manager.handle_call_async.await_args.args[0]
    assert downloads == ["https://cdn/same.png"]
    assert len(call.img_urls) == 1 and "消息 1] [图片 1]" in call.message


@pytest.mark.asyncio
async def test_small_text_budget_never_downloads_invisible_history(downloads: list[str]):
    adapter, scheduler, manager = runtime({"input_text_limit": 32})
    await scheduler.execute(await incoming(adapter, 1, [picture("old")]))
    current = await incoming(adapter, 2, [AT, text("x" * 32)])
    await scheduler.execute(current)
    call = manager.handle_call_async.await_args.args[0]
    assert len(call.message) <= 32 and not call.img_urls and not downloads
    assert "window_budget_dropped" in current.get_extra("input_projection").notes


@pytest.mark.asyncio
@pytest.mark.parametrize("scope,visible", [("legacy_user", False), ("group_member", False), ("group", True)])
async def test_window_respects_member_visibility(scope: str, visible: bool, downloads: list[str]):
    adapter, scheduler, manager = runtime({"context_scope": scope})
    await scheduler.execute(await incoming(adapter, 1, [text("成员私有正文"), picture("member")], actor=31,
                                           sender={"user_id": 31, "nickname": "成员甲"}))
    await scheduler.execute(await incoming(adapter, 2, [AT, text("现在回答")], actor=30))
    call = manager.handle_call_async.await_args.args[0]
    assert ("成员私有正文" in call.message) == visible
    assert bool(call.img_urls) == visible
    assert bool(downloads) == visible


@pytest.mark.asyncio
async def test_picture_reference_bounds_ttl_and_route_identity():
    adapter, _, _ = runtime()
    original = await incoming(adapter, 1, [picture("a"), picture("b"), picture("c")])
    window = WakeWindow(ttl=10, max_images=2, max_images_per_message=1)
    snapshot = window.observe(original, 0)
    assert snapshot[0].image_count == 1 and snapshot[0].omitted_images == 2
    assert window.observe(original, 1) == snapshot
    for number, (attribute, value) in enumerate([("session_provider", "other"), ("session_type", "other"), ("group_route_generation", 1)], 2):
        cloned = await incoming(adapter, number, [text("other")])
        setattr(cloned, attribute, value)
        assert window.peek(cloned, 1) == ()
    for number, changed_origin in enumerate([replace(original.call_origin, adapter_id="other"),
                           replace(original.call_origin, self_id="11"), replace(original.call_origin, chat_id="21")], 5):
        cloned = await incoming(adapter, number, [text("other")])
        cloned._call_origin = changed_origin
        assert window.peek(cloned, 1) == ()
    assert window.peek(original, 10) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("sender,expected", [
    ({"nickname": "小明", "card": "群名片"}, "小明 (ID 30)"),
    ({"nickname": "", "card": "群名片"}, "群名片 (ID 30)"),
    ({}, "30"),
    ({"nickname": "坏\n[标记]\x00"}, "坏 (标记) (ID 30)"),
])
async def test_nickname_fallback_and_sanitized_labels(sender: dict[str, Any], expected: str):
    adapter, scheduler, manager = runtime()
    await scheduler.execute(await incoming(adapter, 1, [text("之前")], sender={"user_id": 30, **sender}))
    await scheduler.execute(await incoming(adapter, 2, [AT, text("提问")], sender={"user_id": 30, **sender}))
    call = manager.handle_call_async.await_args.args[0]
    assert f"[用户 {expected}, 消息 1]" in call.message
    assert f"[用户 {expected}, 消息 2]" in call.message
    assert call.origin.actor_id == "30"


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [False, True])
async def test_failed_historical_image_does_not_look_available(question: bool, monkeypatch: pytest.MonkeyPatch):
    adapter, scheduler, manager = runtime()
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人乙"}
    adapter._bot.get_image.return_value = {}

    async def failure(*_args: object, **_kwargs: object) -> bytes:
        """模拟图片已被实现淘汰"""
        raise RuntimeError("expired")

    monkeypatch.setattr("satrap.core.pipeline.media_resolve._download", failure)
    await scheduler.execute(await incoming(adapter, 1, [picture("expired")]))
    await scheduler.execute(await incoming(adapter, 2, [AT, *([text("是什么图") ] if question else [])]))
    adapter._bot.get_image.assert_any_await(file="expired.image")
    if question:
        call = manager.handle_call_async.await_args.args[0]
        assert not call.img_urls and "[图片读取失败]" in call.message
    else:
        manager.handle_call_async.assert_not_awaited()
        adapter._bot.send_group_msg.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_historical_url_refreshes_original_file(monkeypatch: pytest.MonkeyPatch):
    adapter, scheduler, manager = runtime()
    urls: list[str] = []
    adapter._bot.get_image.return_value = {"url": "https://cdn/fresh.png"}

    async def download(url: str, *_args: object, **_kwargs: object) -> bytes:
        """模拟旧 URL 过期而新 URL 可以下载"""
        urls.append(url)
        if len(urls) == 1:
            raise RuntimeError("expired")
        return b"\x89PNG\r\n\x1a\n" + bytes(32)

    monkeypatch.setattr("satrap.core.pipeline.media_resolve._download", download)
    await scheduler.execute(await incoming(adapter, 1, [picture("expired")]))
    await scheduler.execute(await incoming(adapter, 2, [AT, text("看图")]))
    assert urls == ["https://cdn/expired.png", "https://cdn/fresh.png"]
    adapter._bot.get_image.assert_awaited_once_with(file="expired.image")
    assert len(manager.handle_call_async.await_args.args[0].img_urls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("with_identity", [False, True])
async def test_deadline_wake_preserves_picture_and_original_sender(with_identity: bool, downloads: list[str]):
    adapter, scheduler, manager = runtime({"wake_mode": "frequency", "wake_message_threshold": 3,
        "wake_max_wait": 0.01, "wake_cooldown": 0})
    if with_identity:
        adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人乙"}
        adapter._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 20, "card": "本群助手"}
    first = await incoming(adapter, 1, [picture("timer")], sender={"user_id": 30, "nickname": "小明"})
    await scheduler.execute(first)
    assert not downloads
    timed = await asyncio.wait_for(adapter._event_queue.get(), timeout=1)
    await scheduler.execute(timed)
    await scheduler.wake_timers.close()
    manager.handle_call_async.assert_awaited_once()
    call = manager.handle_call_async.await_args.args[0]
    prefix = "[你当前的平台机器人身份: 账号 ID 10, 账号昵称 机器人乙, 本群昵称 本群助手]\n" if with_identity else ""
    assert call.message == prefix + "[用户 小明 (ID 30), 消息 1] [图片 1]"
    assert len(call.img_urls) == 1 and downloads == ["https://cdn/timer.png"]


@pytest.mark.asyncio
async def test_retained_image_caps_are_visible_in_projection():
    adapter, _, _ = runtime()
    event = await incoming(adapter, 1, [picture("a"), picture("b")])
    snapshot = WakeWindow(max_images_per_message=1).observe(event)
    selection = select_window_media(event, select_media(event, "none"), snapshot, "none", "none", (), True)
    projected = project_input(event, "none", selection=selection)
    assert "另有 1 张图片超出窗口容量" in projected.message
    assert "window_media_storage_truncated" in projected.notes


@pytest.mark.asyncio
async def test_rate_limit_keeps_window_until_later_success(downloads: list[str]):
    adapter, scheduler, manager = runtime()
    limiter = AsyncMock()
    limiter.check.side_effect = [(False, 1.0), (True, 0.0)]
    scheduler.rate_limiter = limiter
    await scheduler.execute(await incoming(adapter, 1, [picture("kept")]))
    blocked = await incoming(adapter, 2, [AT, text("第一次提问")])
    await scheduler.execute(blocked)
    manager.handle_call_async.assert_not_awaited()
    assert len(scheduler.wake_window.peek(blocked)) == 2 and not downloads
    await scheduler.execute(await incoming(adapter, 3, [AT, text("第二次提问")]))
    call = manager.handle_call_async.await_args.args[0]
    assert "第一次提问" in call.message and "第二次提问" in call.message
    assert downloads == ["https://cdn/kept.png"]
    assert not scheduler.wake_window.peek(blocked)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["explicit", "frequency", "necessity"])
async def test_commands_leave_image_window_untouched(mode: str, downloads: list[str]):
    adapter, scheduler, manager = runtime({"wake_mode": mode, "wake_message_threshold": 32, "wake_score_threshold": 1})
    first = await incoming(adapter, 1, [picture("waiting")])
    await scheduler.execute(first)
    before = scheduler.wake_window.peek(first)
    command = await incoming(adapter, 2, [AT, text(" /help")])
    await scheduler.execute(command)
    assert manager.handle_call_async.await_args.args[0].message == "/help"
    assert scheduler.wake_window.peek(command) == before and not downloads
    await scheduler.execute(await incoming(adapter, 3, [AT, text("看看")]))
    assert downloads == ["https://cdn/waiting.png"]


@pytest.mark.asyncio
async def test_window_capacity_counts_preserved_whitespace():
    adapter, _, _ = runtime()
    window = WakeWindow(max_chars=8)
    first = await incoming(adapter, 1, [text("     a"), picture("first")])
    second = await incoming(adapter, 2, [text("     b"), picture("second")])
    window.observe(first)
    snapshot = window.observe(second)
    assert [item.message_id for item in snapshot] == ["2"]
    assert snapshot[0].text == "b" and snapshot[0].text_size == 6


@pytest.mark.asyncio
async def test_own_identity_is_distinct_from_sender_and_wake_alias():
    adapter, scheduler, manager = runtime({"wake_aliases": ["唤醒别名"]})
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人乙"}
    adapter._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 20, "card": "本群助手"}
    await scheduler.execute(await incoming(adapter, 1, [AT, text("你叫什么")], sender={"user_id": 30, "nickname": "用户甲"}))
    call = manager.handle_call_async.await_args.args[0]
    assert call.message.startswith("[你当前的平台机器人身份: 账号 ID 10, 账号昵称 机器人乙, 本群昵称 本群助手]\n")
    assert "[用户 用户甲 (ID 30), 消息 1]" in call.message
    assert "你叫什么" in call.message and "唤醒别名" not in call.message
    assert call.origin.actor_id == "30" and call.origin.self_id == "10"
    adapter._bot.get_group_member_info.assert_awaited_once_with(self_id="10", group_id=20, user_id=10, no_cache=True)


@pytest.mark.asyncio
async def test_private_message_gets_own_account_nickname():
    adapter, scheduler, manager = runtime()
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人乙"}
    await adapter._handle_private_message({"self_id": 10, "user_id": 30, "message_id": 1, "message_type": "private",
        "sender": {"user_id": 30, "nickname": "用户甲"}, "message": [text("你叫什么")]})
    await scheduler.execute(adapter._event_queue.get_nowait())
    call = manager.handle_call_async.await_args.args[0]
    assert call.message.startswith("[你当前的平台机器人身份: 账号 ID 10, 账号昵称 机器人乙]\n")
    assert "你叫什么" in call.message and "本群昵称" not in call.message
    adapter._bot.get_group_member_info.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["unwoken", "command", "limited", "disabled"])
async def test_identity_is_not_queried_before_normal_input_is_accepted(gate: str):
    adapter, scheduler, manager = runtime()
    segments = [AT, text("提问")]
    if gate == "unwoken":
        segments = [text("提问")]
    elif gate == "command":
        segments = [AT, text("/help")]
    elif gate == "limited":
        scheduler.rate_limiter = AsyncMock()
        scheduler.rate_limiter.check.return_value = (False, 1.0)
    else:
        manager.provider_registry = Mock()
        manager.provider_registry.binding_status.return_value = BindingStatus(BindingState.DISABLED)
    await scheduler.execute(await incoming(adapter, 1, segments))
    adapter._bot.get_login_info.assert_not_awaited()
    adapter._bot.get_group_member_info.assert_not_awaited()
    if gate != "command":
        manager.handle_call_async.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [32, 128, 256])
async def test_identity_reserves_history_budget_without_orphan_images(limit: int, downloads: list[str]):
    adapter, scheduler, manager = runtime({"input_text_limit": limit})
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人乙"}
    adapter._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 20, "card": "本群助手"}
    await scheduler.execute(await incoming(adapter, 1, [picture("old")]))
    event = await incoming(adapter, 2, [AT, text("现在看图")])
    await scheduler.execute(event)
    call = manager.handle_call_async.await_args.args[0]
    assert len(call.message) <= limit and "现在看图" in call.message
    for number in range(1, len(call.img_urls or []) + 1):
        assert f"[图片 {number}]" in call.message
    if limit == 32:
        assert "你当前的平台机器人身份" not in call.message
        assert "bot_identity_budget_dropped" in event.get_extra("input_projection").notes
    else:
        assert "账号昵称 机器人乙" in call.message


@pytest.mark.asyncio
@pytest.mark.parametrize("identity,expected", [
    (BotIdentity("10", "机器人\n[乙]\x00"), "账号昵称 机器人 (乙)"),
    (BotIdentity("11", "其他账号"), ""),
    (BotIdentity("10", "其他群", "21", "其他名片"), ""),
])
async def test_identity_projection_sanitizes_names_and_rejects_wrong_scope(identity: BotIdentity, expected: str):
    adapter, _, _ = runtime()
    event = await incoming(adapter, 1, [AT, text("提问")])
    selection = replace(select_media(event, "none"), self_identity=identity)
    projected = project_input(event, "none", selection=selection)
    assert "提问" in projected.message
    if expected:
        assert expected in projected.message and "\x00" not in projected.message
        assert projected.message.count("\n") == 1
    else:
        assert "你当前的平台机器人身份" not in projected.message
