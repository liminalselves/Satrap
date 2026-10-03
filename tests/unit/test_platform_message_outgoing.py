"""真实发送确认采集, 分段边界与归档故障隔离"""
from __future__ import annotations

import asyncio
from pathlib import Path
from time import time
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from satrap.core.components import At, File, Node, Plain, Reply
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.platform import PlatformConfig
from satrap.core.platform.event import MessageChain
from satrap.core.platform.misskey.adapter import MisskeyAdapter
from satrap.core.platform.onebot.adapter import OneBotAdapter


def _onebot(tmp_path: Path, **settings: Any) -> OneBotAdapter:
    """
    装配实际归档和伪造发送端, 不连接外部平台

    参数:
    - tmp_path: 独立测试目录
    - settings: 额外平台设置

    返回:
    - 已确认账号身份的 OneBot 适配器
    """
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={"self_id": "10000", **settings}))
    adapter.message_archive = PlatformMessageStore(tmp_path / "platform.db", "ob")
    adapter._bot = cast(Any, Mock(send_group_msg=AsyncMock(return_value={"message_id": 1}),
                                send_private_msg=AsyncMock(return_value={"message_id": 2})))
    return adapter


def _store(adapter: OneBotAdapter | MisskeyAdapter) -> PlatformMessageStore:
    """
    取得测试明确装配的档案存储

    参数:
    - adapter: 被测适配器

    返回:
    - 已装配的真实平台档案
    """
    assert adapter.message_archive is not None
    return adapter.message_archive


def _item(adapter: OneBotAdapter | MisskeyAdapter, scope: MessageScope, message_id: str) -> dict[str, Any]:
    """
    验证实际采集记录存在后返回其字段

    参数:
    - adapter: 被测适配器
    - scope: 预期的消息归属
    - message_id: 已确认平台消息 ID

    返回:
    - 已持久化的消息记录
    """
    item = _store(adapter).get(scope, message_id)
    assert item is not None
    return item


@pytest.mark.asyncio
@pytest.mark.parametrize("session,kind,chat_id,message_id", [("group%456", "group", "456", "1"),
                                                            ("private%123", "private", "123", "2")])
async def test_actual_chain_and_private_peer_archive(tmp_path: Path, session: str, kind: str,
                                                     chat_id: str, message_id: str) -> None:
    adapter = _onebot(tmp_path)
    receipt = await adapter.send_message(session, MessageChain([Reply(id="99"), Plain("你好"), At(qq="789")]))
    await adapter.drain_message_archive()
    item = _item(adapter, MessageScope("ob", "10000", kind, chat_id), message_id)
    assert receipt.status == "success"
    assert item["text"] == "你好" and item["mentions"] == ["789"]
    assert item["reply_to_message_id"] == "99" and item["sender_id"] == "10000"
    assert item["direction"] == "outbound" and item["source"] == "confirmed_send"
    assert item["time_source"] == "local"


@pytest.mark.asyncio
async def test_split_partial_delivery_archives_only_confirmed_actual_chunks(tmp_path: Path) -> None:
    adapter = _onebot(tmp_path, message_text_limit=64)
    adapter._bot.send_group_msg = AsyncMock(side_effect=[{"message_id": 10}, {"message_id": 11}, None])
    receipt = await adapter.send_message("group%456", MessageChain.from_text("字" * 130))
    await adapter.drain_message_archive()
    result = _store(adapter).query(MessageScope("ob", "10000", "group", "456"))
    assert receipt.status != "success"
    assert {item["message_id"] for item in result["items"]} == {"10", "11"}
    assert [len(item["text"]) for item in result["items"]] == [64, 64]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {}, {"message_id": ""}, {"message_id": True}])
async def test_unknown_send_never_archives(tmp_path: Path, result: Any) -> None:
    adapter = _onebot(tmp_path)
    adapter._bot.send_group_msg = AsyncMock(return_value=result)
    receipt = await adapter._send_chunk("group%456", MessageChain.from_text("草稿"))
    await adapter.drain_message_archive()
    assert receipt.status != "success" and not _store(adapter).database.exists()


@pytest.mark.asyncio
async def test_file_upload_id_is_not_a_message_id(tmp_path: Path) -> None:
    adapter = _onebot(tmp_path)
    adapter._bot.upload_group_file = AsyncMock(return_value={"file_id": "file-1"})
    target = tmp_path / "asset.bin"
    target.write_bytes(b"content")
    receipt = await adapter.send_message("group%456", MessageChain([File(name="asset.bin", file=str(target))]))
    await adapter.drain_message_archive()
    assert receipt.status == "success" and receipt.message_ids == ("file-1",)
    assert not _store(adapter).database.exists()


@pytest.mark.asyncio
async def test_forward_archives_root_not_fabricated_node_messages(tmp_path: Path) -> None:
    adapter = _onebot(tmp_path)
    adapter._bot.send_group_forward_msg = AsyncMock(return_value={"message_id": 12, "forward_id": "forward-ref"})
    receipt = await adapter._send_forward("group%456", [Node(Plain("其它成员原文"), uin="123", name="成员")], 64)
    await adapter.drain_message_archive()
    item = _item(adapter, MessageScope("ob", "10000", "group", "456"), "12")
    assert receipt.status == "success"
    assert item["sender_id"] == "10000" and item["text"] == ""
    assert item["components"] == [{"type": "Forward", "id": "forward-ref"}]


@pytest.mark.asyncio
async def test_confirmed_message_deduplicates_own_echo(tmp_path: Path) -> None:
    adapter = _onebot(tmp_path)
    await adapter._send_chunk("group%456", MessageChain.from_text("同一条"))
    await adapter.drain_message_archive()
    await adapter._handle_group_message({"message_type": "group", "self_id": 10000, "user_id": 10000,
                                         "group_id": 456, "message_id": 1, "time": time(),
                                         "message": [{"type": "text", "data": {"text": "同一条"}}]})
    assert len(_store(adapter).query(MessageScope("ob", "10000", "group", "456"))["items"]) == 1
    assert adapter._event_queue.empty()


@pytest.mark.asyncio
async def test_archive_failure_does_not_change_success_receipt(tmp_path: Path, caplog) -> None:
    adapter = _onebot(tmp_path)
    _store(adapter).record = Mock(side_effect=OSError("故障"))
    receipt = await adapter._send_chunk("group%456", MessageChain.from_text("已送达"))
    await adapter.stop()
    assert receipt.status == "success" and adapter._archive_tasks == set()
    assert "出站写入失败" in caplog.text


@pytest.mark.asyncio
async def test_onebot_confirmation_preserves_account_at_submission(tmp_path: Path) -> None:
    adapter = _onebot(tmp_path)

    async def send(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["message"] == [{"type": "text", "data": {"text": "原账号"}}]
        adapter.bot_self_id = "20000"
        return {"message_id": 5}

    adapter._bot.send_group_msg = AsyncMock(side_effect=send)
    await adapter._send_chunk("group%456", MessageChain.from_text("原账号"))
    await adapter.drain_message_archive()
    assert _item(adapter, MessageScope("ob", "10000", "group", "456"), "5")["text"] == "原账号"
    assert _store(adapter).get(MessageScope("ob", "20000", "group", "456"), "5") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [True, False])
async def test_stream_archives_actual_emitted_chunks(tmp_path: Path, fallback: bool) -> None:
    adapter = _onebot(tmp_path)
    adapter._bot.send_group_msg = AsyncMock(side_effect=[{"message_id": 1}, {"message_id": 2}])

    async def chunks():
        yield MessageChain.from_text("第一段")
        yield MessageChain.from_text("第二段")

    receipt = await adapter.send_stream("group%456", chunks(), use_fallback=fallback)
    await adapter.drain_message_archive()
    items = _store(adapter).query(MessageScope("ob", "10000", "group", "456"))["items"]
    assert receipt.status == "success"
    assert [item["text"] for item in items] == (["第一段", "第二段"] if fallback else ["第一段第二段"])


@pytest.mark.asyncio
async def test_queue_bound_and_cancelled_sender_do_not_drop_owned_write(tmp_path: Path, caplog) -> None:
    adapter = _onebot(tmp_path, event_queue_capacity=1)
    started, release = asyncio.Event(), asyncio.Event()
    original = adapter._write_confirmed_message

    async def delayed_write(store, scope, snapshot) -> None:
        started.set()
        await release.wait()
        await original(store, scope, snapshot)

    adapter._write_confirmed_message = delayed_write
    scope = MessageScope("ob", "10000", "group", "456")

    async def sender() -> None:
        await adapter._send_chunk("group%456", MessageChain.from_text("确认后取消"))
        await asyncio.Event().wait()

    task = asyncio.create_task(sender())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not adapter.queue_confirmed_message(scope, ArchiveMessage("2", "10000", time(), "新消息", direction="outbound"))
    await adapter.drain_message_archive(timeout=0)
    assert "队列已满" in caplog.text and "停止等待超时" in caplog.text
    release.set()
    await adapter.drain_message_archive()
    assert _item(adapter, scope, "1")["text"] == "确认后取消"


def _misskey(tmp_path: Path) -> MisskeyAdapter:
    """
    装配 Misskey 原生回包和实际档案存储

    参数:
    - tmp_path: 独立测试目录

    返回:
    - 不连接外部平台的 Misskey 适配器
    """
    adapter = MisskeyAdapter(PlatformConfig(id="mk", type="misskey", settings={}))
    adapter.bot_self_id = "bot"
    adapter.message_archive = PlatformMessageStore(tmp_path / "platform.db", "mk")
    adapter._client = cast(Any, Mock(send_room_message=AsyncMock(return_value={"id": "room-message"}),
                                   send_message=AsyncMock(return_value={"id": "private-message"}),
                                   create_note=AsyncMock(return_value={"createdNote": {"id": "note-message"}})))
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("session,kind,chat_id,message_id", [("room%r", "group", "r", "room-message"),
                                                            ("chat%p", "private", "p", "private-message"),
                                                            ("note%p", "discussion", "note%p", "note-message")])
async def test_misskey_actual_clipped_text_and_sent_files(tmp_path: Path, session: str, kind: str,
                                                        chat_id: str, message_id: str) -> None:
    adapter = _misskey(tmp_path)
    adapter.max_message_length = 4
    adapter._collect_file_ids = AsyncMock(return_value=["file-1", "file-2"])
    result = await adapter.send_message(session, MessageChain.from_text("abcdefgh"))
    await adapter.drain_message_archive()
    item = _item(adapter, MessageScope("mk", "bot", kind, chat_id), message_id)
    assert isinstance(result, dict) and item["text"].endswith("abcd...")
    assert item["media"] == [{"type": "File", "native_id": "file-1"},
                              *([{"type": "File", "native_id": "file-2"}] if kind == "discussion" else [])]
    assert item["time_source"] == "local" and item["direction"] == "outbound"


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {"id": True}, {"id": ""}, {"id": "x", "toRoomId": "wrong"},
                                    {"id": "x", "fromUserId": "wrong"}])
async def test_misskey_unconfirmed_or_mismatched_receipt_not_archived(tmp_path: Path, result: Any) -> None:
    adapter = _misskey(tmp_path)
    cast(Any, adapter._client).send_room_message = AsyncMock(return_value=result)
    assert await adapter.send_message("room%r", MessageChain.from_text("消息")) == result
    await adapter.drain_message_archive()
    assert not _store(adapter).database.exists()


@pytest.mark.asyncio
async def test_misskey_frozen_account_and_native_timestamp(tmp_path: Path) -> None:
    adapter = _misskey(tmp_path)

    async def send(payload: Any) -> dict[str, Any]:
        adapter.bot_self_id = "replacement"
        return {"id": "confirmed", "fromUserId": "bot", "createdAt": "2026-10-03T12:00:00+08:00"}

    cast(Any, adapter._client).send_room_message = AsyncMock(side_effect=send)
    await adapter.send_message("room%r", MessageChain.from_text("原账号"))
    await adapter.drain_message_archive()
    item = _item(adapter, MessageScope("mk", "bot", "group", "r"), "confirmed")
    assert item["sender_id"] == "bot" and item["time_source"] == "platform"
    assert _store(adapter).get(MessageScope("mk", "replacement", "group", "r"), "confirmed") is None
