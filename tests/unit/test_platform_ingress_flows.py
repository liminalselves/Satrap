"""业务链路场景与集中边界契约"""
from __future__ import annotations

from typing import Any
import asyncio
from copy import deepcopy
import pytest
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform import PlatformConfig
from pathlib import Path
from unittest.mock import Mock
import logging
from satrap.core.components import At, Image, Plain, Reply
from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore
from satrap.core.platform.message_archive import archive_snapshot
from satrap.core.platform.misskey.adapter import MisskeyAdapter
from unittest.mock import AsyncMock
from satrap.core.platform.identity import BotIdentity

def payload(message_id: int | None = 10, self_id: int | None = 10000, user_id: int = 123, group_id: int = 456) -> dict[str, Any]:
    return {"self_id": self_id, "user_id": user_id, "group_id": group_id,
            "message_id": message_id, "message_type": "group",
            "message": [{"type": "text", "data": {"text": "测试正文"}}]}

@pytest.mark.asyncio
@pytest.mark.parametrize("role,sender_id,expected", [("admin", 123, "admin"), ("owner", 123, "owner"),
                                                   ("admin", 999, ""), ("root", 123, "")])
async def test_group_role_is_frozen_from_matching_platform_sender(role, sender_id, expected):
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot"))
    frame = deepcopy(payload())
    frame["sender"] = {"user_id": sender_id, "role": role}
    await adapter._handle_group_message(frame)
    event = adapter._event_queue.get_nowait()
    frame["sender"]["role"] = "member"
    assert event.call_origin.group_role == expected

@pytest.mark.asyncio
async def test_account_binding_and_echo_filter_do_not_poison_dedup():
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    await adapter._handle_group_message(payload(self_id=None))
    assert adapter.bot_self_id == ""
    await adapter._handle_group_message(payload())
    await adapter._handle_group_message(payload(11, self_id=20000))
    await adapter._handle_group_message(payload(11, user_id=10000))
    await adapter._handle_group_message(payload(11))
    assert adapter.bot_self_id == adapter.client_self_id == "10000"
    assert adapter._event_queue.qsize() == 2
    assert adapter.get_stats()["ingress"] == {
        "account": 2, "self_echo": 1, "duplicate": 0, "handler_error": 0, "dedup_entries": 2,
        "dedup_capacity": 4096, "dedup_ttl": 120,
    }

@pytest.mark.asyncio
async def test_replay_scope_expiry_and_instance_isolation(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("satrap.core.platform.onebot.adapter.monotonic", lambda: clock[0])
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    other = OneBotAdapter(PlatformConfig(id="other", type="onebot", settings={}))
    await adapter._handle_group_message(payload())
    await adapter._handle_group_message(payload(user_id=124))
    await adapter._handle_group_message(payload(group_id=789))
    await other._handle_group_message(payload())
    assert adapter._event_queue.qsize() == 2
    assert other._event_queue.qsize() == 1
    clock[0] = 220.0
    await adapter._handle_group_message(payload())
    assert adapter._event_queue.qsize() == 3
    assert len(adapter._seen_messages) == 1

@pytest.mark.asyncio
async def test_concurrent_duplicate_and_cancelled_conversion(monkeypatch):
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    entered = asyncio.Event()
    release = asyncio.Event()
    original = adapter.convert_message

    async def convert(raw):
        entered.set()
        await release.wait()
        return await original(raw)

    monkeypatch.setattr(adapter, "convert_message", convert)
    first = asyncio.create_task(adapter._handle_group_message(payload()))
    await entered.wait()
    await adapter._handle_group_message(payload())
    assert adapter._event_queue.empty()
    assert adapter.get_stats()["ingress"]["duplicate"] == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not adapter._seen_messages
    release.set()
    await adapter._handle_group_message(payload())
    assert adapter._event_queue.qsize() == 1

@pytest.mark.asyncio
async def test_full_queue_does_not_mark_message_delivered():
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={"event_queue_capacity": 1}))
    await adapter._handle_group_message(payload())
    await adapter._handle_group_message(payload(11))
    assert len(adapter._seen_messages) == 1
    adapter._event_queue.get_nowait()
    adapter._event_queue.task_done()
    await adapter._handle_group_message(payload(11))
    assert adapter._event_queue.get_nowait().call_origin.source_message_id == "11"

@pytest.mark.asyncio
async def test_dedup_capacity_is_bounded_and_termination_clears_state():
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    # 预置远超上界的未过期键, 新消息入队后按容量裁剪
    adapter._seen_messages.update({("g", "u", "m", f"k{i}"): 1e18 for i in range(4100)})
    await adapter._handle_group_message(payload(message_id=99999))
    assert len(adapter._seen_messages) == 4096
    await adapter.terminate()
    assert not adapter._seen_messages

@pytest.mark.asyncio
async def test_missing_message_id_is_not_a_shared_dedup_key():
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    for _ in range(2):
        await adapter._handle_group_message(payload(message_id=None))
    assert adapter._event_queue.qsize() == 2
    assert not adapter._seen_messages

@pytest.mark.asyncio
async def test_guarded_handler_isolates_exception_and_counts(caplog: pytest.LogCaptureFixture):
    """aiocqhttp 回调入口异常被兜底记录, 不改变适配器状态"""
    import logging
    from unittest.mock import AsyncMock, Mock
    from satrap.core.platform import PlatformStatus
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    seen: list[str] = []
    adapter._bot = Mock(spec=["on_message"])
    adapter._bot.on_message = lambda kind: (lambda handler: seen.append(kind) or handler)
    adapter._register_handlers()
    assert seen == ["private", "group"]
    guarded = adapter._guard_handler("group_message", AsyncMock(side_effect=ValueError("bad whitelist")))
    with caplog.at_level(logging.ERROR):
        await guarded(payload())
    assert adapter._ingress_rejections["handler_error"] == 1
    assert adapter.status is not PlatformStatus.ERROR
    assert any("group_message 处理异常" in record.getMessage() and "ValueError" in record.getMessage() for record in caplog.records)
    with pytest.raises(asyncio.CancelledError):
        await adapter._guard_handler("x", AsyncMock(side_effect=asyncio.CancelledError()))(payload())

NOW = 1_800_000_000.0

def _onebot(tmp_path: Path, **settings) -> OneBotAdapter:
    """装配实际平台数据库, 不连接外部适配器"""
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=settings))
    adapter.message_archive = PlatformMessageStore(tmp_path / "platform.db", "ob", clock=lambda: NOW)
    return adapter

def _payload(message_id: int = 10, *, user_id: int = 123, group_id: int = 456, self_id: int = 10000) -> dict:
    """生成带真实时间和身份信息的 OneBot 原始事件"""
    return {"message_type": "group", "self_id": self_id, "user_id": user_id, "group_id": group_id,
            "message_id": message_id, "time": NOW - 10, "sender": {"nickname": "昵称", "card": "群名片"},
            "message": [{"type": "text", "data": {"text": "未提及机器人也要采集"}},
                        {"type": "at", "data": {"qq": "789"}}, {"type": "reply", "data": {"id": "old"}}]}

@pytest.mark.asyncio
async def test_admitted_messages_archive_before_model_queue_even_when_queue_full(tmp_path: Path) -> None:
    adapter = _onebot(tmp_path, event_queue_capacity=1, wake_only_when_mentioned=True)
    await adapter._handle_group_message(_payload())
    await adapter._handle_group_message(_payload(11))
    scope = MessageScope("ob", "10000", "group", "456")
    result = adapter.message_archive.query(scope)
    assert adapter._event_queue.qsize() == 1
    assert adapter.dropped_events == 1
    assert [item["message_id"] for item in result["items"]] == ["10", "11"]
    for item in result["items"]:
        assert item["sender_id"] == "123" and item["mentions"] == ["789"]
        assert item["nickname"] == "昵称" and item["card"] == "群名片"
        assert item["reply_to_message_id"] == "old" and item["time_source"] == "platform"
        assert item["message_time"] == NOW - 10

@pytest.mark.asyncio
async def test_rejected_group_and_foreign_account_are_not_archived(tmp_path: Path) -> None:
    adapter = _onebot(tmp_path, self_id="10000", group_whitelist=["456"])
    assert adapter.client_self_id == "10000"
    await adapter._handle_group_message(_payload(group_id=789))
    await adapter._handle_group_message(_payload(self_id=20000))
    assert adapter._event_queue.empty()
    assert not adapter.message_archive.database.exists()
    await adapter._handle_group_message(_payload())
    assert len(adapter.message_archive.query(MessageScope("ob", "10000", "group", "456"))["items"]) == 1
    assert adapter.message_archive.query(MessageScope("ob", "20000", "group", "456"))["items"] == []

@pytest.mark.asyncio
async def test_own_group_echo_archives_but_does_not_wake_and_restart_deduplicates(tmp_path: Path) -> None:
    adapter = _onebot(tmp_path)
    await adapter._handle_group_message(_payload(user_id=10000))
    assert adapter._event_queue.empty()
    scope = MessageScope("ob", "10000", "group", "456")
    assert adapter.message_archive.get(scope, "10")["direction"] == "outbound"
    restarted = _onebot(tmp_path)
    await restarted._handle_group_message(_payload(user_id=10000))
    assert restarted._event_queue.empty()
    assert len(restarted.message_archive.query(scope)["items"]) == 1

@pytest.mark.asyncio
async def test_archive_write_failure_logs_and_next_messages_still_enter_queue(tmp_path: Path, monkeypatch,
                                                                            caplog: pytest.LogCaptureFixture) -> None:
    adapter = _onebot(tmp_path)
    original = adapter.message_archive.record
    failing = Mock(side_effect=OSError("数据库不可写"))
    monkeypatch.setattr(adapter.message_archive, "record", failing)
    with caplog.at_level(logging.ERROR):
        await adapter._handle_group_message(_payload())
    assert adapter._event_queue.qsize() == 1
    assert any("采集失败" in record.getMessage() and "OSError" in record.getMessage() for record in caplog.records)
    monkeypatch.setattr(adapter.message_archive, "record", original)
    await adapter._handle_group_message(_payload(11))
    assert adapter._event_queue.qsize() == 2
    assert adapter.message_archive.get(MessageScope("ob", "10000", "group", "456"), "11") is not None

@pytest.mark.asyncio
async def test_verified_recall_marks_body_but_foreign_and_denied_notices_cannot(tmp_path: Path) -> None:
    adapter = _onebot(tmp_path, group_whitelist=["456"])
    await adapter._handle_group_message(_payload())
    scope = MessageScope("ob", "10000", "group", "456")
    await adapter._handle_notice({"self_id": 20000, "notice_type": "group_recall", "group_id": 456, "message_id": 10})
    assert adapter.message_archive.get(scope, "10")["status"] == "active"
    await adapter._handle_notice({"self_id": 10000, "notice_type": "group_recall", "group_id": 789, "message_id": 10})
    assert adapter.message_archive.get(scope, "10")["status"] == "active"
    await adapter._handle_notice({"self_id": 10000, "notice_type": "group_recall", "group_id": 456, "message_id": 10})
    assert adapter.message_archive.get(scope, "10")["status"] == "recalled"
    assert adapter.message_archive.query(scope)["items"] == []

@pytest.mark.asyncio
async def test_media_snapshot_never_reads_local_files_or_embedded_data(tmp_path: Path) -> None:
    adapter = _onebot(tmp_path)
    message = await adapter.convert_message(_payload())
    message.message = [Plain("原文"), Reply(id="quoted", message_str="被引用正文不混入当前正文"), At(qq="target"),
                       Image(file="base64://SECRET"), Image(file="F:/private/secret.png"),
                       Image(file="https://example.org/image.png")]
    snapshot = archive_snapshot(message)
    assert snapshot.text == "原文" and snapshot.mentions == ["target"]
    assert snapshot.reply_to_message_id == "quoted"
    assert snapshot.media == [{"type": "Image"}, {"type": "Image"},
                              {"type": "Image", "url": "https://example.org/image.png"}]
    assert "SECRET" not in str(snapshot) and "secret.png" not in str(snapshot)

def _misskey(tmp_path: Path, **settings) -> MisskeyAdapter:
    """装配 Misskey 测试实例, 不连接网络"""
    adapter = MisskeyAdapter(PlatformConfig(id="mk", type="misskey", settings=settings))
    adapter.bot_self_id = adapter.client_self_id = "bot"
    adapter.message_archive = PlatformMessageStore(tmp_path / "platform.db", "mk", clock=lambda: NOW)
    return adapter

def _chat(message_id="chat-1", sender="member", room="room-a") -> dict:
    """生成 Misskey 房间或私聊事件"""
    return {"id": message_id, "fromUserId": sender, "fromUser": {"id": sender, "username": sender, "name": "成员"},
            "toRoomId": room, "toRoom": {"name": "房间名"}, "text": "房间讨论",
            "createdAt": "2027-01-15T08:00:00Z"}

@pytest.mark.asyncio
async def test_misskey_room_and_private_are_distinct_and_platform_time_is_preserved(tmp_path: Path) -> None:
    adapter = _misskey(tmp_path)
    await adapter._handle_chat_message(_chat())
    await adapter._handle_chat_message(_chat(room=None))
    room = adapter.message_archive.get(MessageScope("mk", "bot", "group", "room-a"), "chat-1")
    private = adapter.message_archive.get(MessageScope("mk", "bot", "private", "member"), "chat-1")
    assert room["text"] == private["text"] == "房间讨论"
    assert room["message_time"] == private["message_time"] == NOW
    assert room["time_source"] == private["time_source"] == "platform"
    assert adapter._event_queue.qsize() == 2

@pytest.mark.asyncio
async def test_misskey_own_room_echo_is_archived_without_agent_event(tmp_path: Path) -> None:
    adapter = _misskey(tmp_path)
    await adapter._handle_chat_message(_chat(sender="bot"))
    assert adapter._event_queue.empty()
    assert adapter.message_archive.get(MessageScope("mk", "bot", "group", "room-a"), "chat-1")["direction"] == "outbound"

@pytest.mark.asyncio
async def test_misskey_disabled_conversation_kinds_are_not_archived(tmp_path: Path) -> None:
    adapter = _misskey(tmp_path, chat_enabled=False, room_enabled=False)
    await adapter._handle_chat_message(_chat())
    await adapter._handle_chat_message(_chat(room=None))
    assert adapter._event_queue.empty() and not adapter.message_archive.database.exists()

@pytest.mark.asyncio
async def test_missing_and_invalid_platform_time_is_explicit_local_fallback(tmp_path: Path) -> None:
    adapter = _misskey(tmp_path)
    raw = _chat()
    raw["createdAt"] = "invalid"
    message = await adapter.convert_room_message(raw)
    assert message.timestamp_source == "local"
    onebot = _onebot(tmp_path / "other")
    raw = _payload()
    raw.pop("time")
    assert (await onebot.convert_message(raw)).timestamp_source == "local"

@pytest.fixture
def adapter() -> OneBotAdapter:
    """装配固定账号及只读动作替身"""
    result = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"self_id": "10"}))
    result._bot = AsyncMock()
    result._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人昵称"}
    result._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 20, "nickname": "机器人昵称", "card": "群中名字"}
    return result

@pytest.mark.asyncio
async def test_self_identity_verifies_caches_and_scopes_group_cards(adapter: OneBotAdapter):
    first, second = await asyncio.gather(adapter.resolve_self_identity("10", "20"), adapter.resolve_self_identity("10", "20"))
    assert first == second == BotIdentity("10", "机器人昵称", "20", "群中名字")
    adapter._bot.get_login_info.assert_awaited_once_with(self_id="10")
    adapter._bot.get_group_member_info.assert_awaited_once_with(self_id="10", group_id=20, user_id=10, no_cache=True)
    assert await adapter.resolve_self_identity("10") == BotIdentity("10", "机器人昵称")
    # 账号昵称共享缓存, 群名片按群隔离
    adapter._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 21, "card": "另一个群名片"}
    assert (await adapter.resolve_self_identity("10", "21")) == BotIdentity("10", "机器人昵称", "21", "另一个群名片")
    assert (await adapter.resolve_self_identity("10", "20")) == BotIdentity("10", "机器人昵称", "20", "群中名字")
    adapter._bot.get_login_info.assert_awaited_once()
    assert adapter._bot.get_group_member_info.await_count == 2

@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"user_id": 11, "nickname": "别人的名字"},
    {"nickname": "无法确认所属账号"},
    {"user_id": 10, "nickname": 123},
    {"user_id": 10, "nickname": " "},
    [],
])
async def test_invalid_account_info_cannot_be_used_as_bot_name(adapter: OneBotAdapter, payload: object):
    adapter._bot.get_login_info.return_value = payload
    assert await adapter.resolve_self_identity("10") is None

@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"user_id": 11, "group_id": 20, "card": "他人的名片"},
    {"user_id": 10, "group_id": 21, "card": "其他群的名片"},
    {"card": "缺少身份"},
    [],
])
async def test_invalid_group_info_falls_back_to_verified_account(adapter: OneBotAdapter, payload: object):
    adapter._bot.get_group_member_info.return_value = payload
    assert await adapter.resolve_self_identity("10", "20") == BotIdentity("10", "机器人昵称", "20")

@pytest.mark.asyncio
async def test_failed_queries_fall_back_then_are_cached_and_refresh_after_ttl(
    adapter: OneBotAdapter, monkeypatch: pytest.MonkeyPatch,
):
    from satrap.core.platform.onebot import self_identity as module

    moment = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: moment[0])
    # 账号资料查询失败时回退到已核验的本群昵称
    adapter._bot.get_login_info.side_effect = RuntimeError("unavailable")
    assert await adapter.resolve_self_identity("10", "20") == BotIdentity("10", "机器人昵称", "20", "群中名字")
    # 两级查询都失败时结果同样缓存, 不重复回源, TTL 到期后才刷新
    adapter._self_identity.clear()
    adapter._bot.get_login_info.reset_mock()
    adapter._bot.get_group_member_info.reset_mock()
    adapter._bot.get_login_info.side_effect = None
    adapter._bot.get_login_info.return_value = {}
    adapter._bot.get_group_member_info.return_value = {}
    assert await adapter.resolve_self_identity("10", "20") is None
    assert await adapter.resolve_self_identity("10", "20") is None
    adapter._bot.get_login_info.assert_awaited_once()
    adapter._bot.get_group_member_info.assert_awaited_once()
    moment[0] = module.IDENTITY_TTL + 1
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "改名后"}
    adapter._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 20, "card": "新名片"}
    assert await adapter.resolve_self_identity("10", "20") == BotIdentity("10", "改名后", "20", "新名片")

@pytest.mark.asyncio
async def test_reconnect_invalidates_identity_cache(adapter: OneBotAdapter):
    assert await adapter.resolve_self_identity("10") == BotIdentity("10", "机器人昵称")
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "重连后名字"}
    await adapter._handle_meta({"self_id": 10, "meta_event_type": "lifecycle", "sub_type": "connect"})
    assert await adapter.resolve_self_identity("10") == BotIdentity("10", "重连后名字")
    assert adapter._bot.get_login_info.await_count == 2

@pytest.mark.asyncio
async def test_changed_client_during_lookup_discards_old_result(adapter: OneBotAdapter):
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(**_kwargs: object) -> dict[str, object]:
        """等待新连接接替旧连接后返回旧资料"""
        entered.set()
        await release.wait()
        return {"user_id": 10, "nickname": "旧连接名字"}

    adapter._bot.get_login_info.side_effect = delayed
    task = asyncio.create_task(adapter.resolve_self_identity("10"))
    await entered.wait()
    adapter._bot = AsyncMock()
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "新连接名字"}
    release.set()
    assert await task is None
    assert await adapter.resolve_self_identity("10") == BotIdentity("10", "新连接名字")

@pytest.mark.asyncio
async def test_timeout_keeps_available_nickname_and_does_not_repeat_query(adapter: OneBotAdapter, monkeypatch: pytest.MonkeyPatch):
    from satrap.core.platform.onebot import self_identity as module

    monkeypatch.setattr(module, "IDENTITY_ACTION_TIMEOUT", 0.01)

    async def blocked(**_kwargs: object) -> dict[str, object]:
        """模拟成员资料接口无响应"""
        await asyncio.Event().wait()
        return {}

    adapter._bot.get_group_member_info.side_effect = blocked
    assert await asyncio.wait_for(adapter.resolve_self_identity("10", "20"), 0.5) == BotIdentity("10", "机器人昵称", "20")
    assert await adapter.resolve_self_identity("10", "20") == BotIdentity("10", "机器人昵称", "20")
    adapter._bot.get_group_member_info.assert_awaited_once()

@pytest.mark.asyncio
async def test_other_account_or_disallowed_group_never_reads_identity(adapter: OneBotAdapter):
    adapter.config.settings["group_whitelist"] = ["20"]
    assert await adapter.resolve_self_identity("11", "20") is None
    assert await adapter.resolve_self_identity("10", "21") is None
    adapter._bot.get_login_info.assert_not_awaited()
    adapter._bot.get_group_member_info.assert_not_awaited()

@pytest.mark.asyncio
async def test_identity_caches_do_not_cross_adapter_instances(adapter: OneBotAdapter):
    other = OneBotAdapter(PlatformConfig(id="second", type="onebot", settings={"self_id": "10"}))
    other._bot = AsyncMock()
    other._bot.get_login_info.return_value = {"user_id": 10, "nickname": "另一个实例"}
    assert await adapter.resolve_self_identity("10") == BotIdentity("10", "机器人昵称")
    assert await other.resolve_self_identity("10") == BotIdentity("10", "另一个实例")
