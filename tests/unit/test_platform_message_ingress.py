"""真实适配器入口中的消息采集, 准入边界与失败隔离"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock
import logging

import pytest

from satrap.core.components import At, Image, Plain, Reply
from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore
from satrap.core.platform import PlatformConfig
from satrap.core.platform.message_archive import archive_snapshot
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.misskey.adapter import MisskeyAdapter


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
    await adapter._handle_group_message(_payload(group_id=789))
    await adapter._handle_group_message(_payload(self_id=20000))
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
