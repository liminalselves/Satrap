"""群聊只读宿主的实际适配器调用, 身份隔离和异步失效反例"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from unittest.mock import AsyncMock
import asyncio
import logging
import sqlite3
from aiocqhttp.exceptions import ActionFailed

import pytest

from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.core.config.agent_routing import AgentRouteStore
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.group_chat.service import GroupChatService
from satrap.core.group_chat.types import GroupChatLimits, MemberRecord, MemberSnapshot, VerifiedMember, VerifiedMessage
from satrap.core.platform import (PlatformAdapter, PlatformAdapterManager, PlatformAdapterRegistry, PlatformConfig,
                                  current_adapter_manager, set_current_adapter_manager)
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.misskey.adapter import MisskeyAdapter
from satrap.core.platform.event import PlatformMetadata


NOW = 1_800_000_000.0
SCOPE = MessageScope("ob", "10000", "group", "456")


@pytest.fixture(autouse=True)
def _manager_scope():
    """恢复进程级管理器, 各测试不依赖其它测试留下的适配器"""
    previous = current_adapter_manager()
    yield
    set_current_adapter_manager(previous)


def _setup(tmp_path: Path) -> tuple[GroupChatService, OneBotAdapter, CallOrigin]:
    """
    装配真实适配器管理器和平台数据库

    参数:
    - tmp_path: 当前测试的独立数据目录

    返回:
    - 群聊宿主, OneBot 实例和可信当前群来源
    """
    registry = PlatformAdapterRegistry()
    registry.register("onebot", OneBotAdapter)
    manager = PlatformAdapterManager(registry)
    adapter = manager.add_adapter(PlatformConfig(id="ob", type="onebot", settings={}))
    assert isinstance(adapter, OneBotAdapter)
    adapter._bot = AsyncMock()
    adapter._running = True
    adapter.bot_self_id = adapter.client_self_id = "10000"
    adapter.message_archive = PlatformMessageStore(tmp_path / "platform.db", "ob", clock=lambda: NOW)
    set_current_adapter_manager(manager)
    origin = CallOrigin("ob", "10000", "GroupMessage", "456", "123", "77", "request-1")
    return GroupChatService(), adapter, origin


def _raw(message_id: int = 77, text: str = "原始平台消息") -> dict:
    """
    构造 OneBot 可核验群消息

    参数:
    - message_id: 平台消息 ID
    - text: 当前消息正文

    返回:
    - 带群, 账号, 发送者和时间的协议回包
    """
    return {"message_type": "group", "self_id": 10000, "group_id": 456, "message_id": message_id,
            "user_id": 123, "sender": {"user_id": 123, "nickname": "甲", "card": "群名片"}, "time": NOW - 10,
            "message": [{"type": "text", "data": {"text": text}}, {"type": "at", "data": {"qq": 789}}]}


@pytest.mark.asyncio
async def test_local_queries_preserve_sender_ids_and_never_use_model_history(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    store = adapter.message_archive
    store.record(SCOPE, ArchiveMessage("a", "123", NOW - 20, "讨论 A", mentions=["789"]))
    store.record(SCOPE, ArchiveMessage("b", "789", NOW - 10, "讨论 B"))
    store.record(MessageScope("ob", "10000", "group", "999"), ArchiveMessage("a", "123", NOW - 20, "其他群"))
    with sqlite3.connect(store.database) as connection:
        connection.execute("CREATE TABLE chat_history(content TEXT)")
        connection.execute("INSERT INTO chat_history VALUES('模型历史不能伪造平台出处')")
    with bind_call_origin(origin):
        result = await service.execute("group_chat_recent_messages", {})
        search = await service.execute("group_chat_search_messages", {"keyword": "讨论", "sender_id": "789"})
        message = await service.execute("group_chat_get_message", {"message_id": "a"})
    assert [item["message_id"] for item in result["items"]] == ["a", "b"]
    assert result["coverage"]["complete"] is False
    assert [item["message_id"] for item in search["items"]] == ["b"]
    assert message["item"]["sender_id"] == "123" and message["item"]["mentions"] == ["789"]
    adapter._bot.get_msg.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("offset_hours", [8, -5, 5.5])
async def test_search_automatically_uses_backend_timezone_and_preserves_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offset_hours: float,
) -> None:
    backend_zone = timezone(timedelta(hours=offset_hours))

    class LocalDateTime(datetime):
        """模拟不同时区的后端, 不修改进程或操作系统时区"""

        def astimezone(self, tz: tzinfo | None = None) -> datetime:
            if self.tzinfo is None and tz is None:
                return self.replace(tzinfo=backend_zone)
            return super().astimezone(tz)

    monkeypatch.setattr("satrap.core.group_chat.service.datetime", LocalDateTime)
    service, adapter, origin = _setup(tmp_path)
    store = adapter.message_archive
    assert store is not None
    for name, when in (("older", NOW - 30), ("a", NOW - 25), ("b", NOW - 20), ("c", NOW - 15), ("newer", NOW - 10)):
        store.record(SCOPE, ArchiveMessage(name, "123", when, "讨论"))
    start = datetime.fromtimestamp(NOW - 25, backend_zone)
    end = datetime.fromtimestamp(NOW - 15, backend_zone)
    plain = {"start_time": start.replace(tzinfo=None).isoformat(), "end_time": end.replace(tzinfo=None).isoformat()}
    explicit = {"start_time": start.astimezone(timezone.utc).isoformat(), "end_time": end.isoformat()}
    with bind_call_origin(origin):
        first = await service.execute("group_chat_search_messages", {**plain, "limit": 1})
        second = await service.execute("group_chat_search_messages", {**explicit, "cursor": first["next_cursor"]})
        all_messages = await service.execute("group_chat_search_messages", {})
        null_bounds = await service.execute("group_chat_search_messages", {"start_time": None, "end_time": None})
    assert first["ok"] and second["ok"]
    assert [item["message_id"] for item in first["items"]] == ["c"]
    assert [item["message_id"] for item in second["items"]] == ["a", "b"]
    assert len(all_messages["items"]) == 5
    assert null_bounds["items"] == all_messages["items"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_bounds", [
    {"start_time": "bad"}, {"end_time": "2026-99-04T18:00:00"},
    {"start_time": 123}, {"end_time": ""}, {"start_time": "x" * 65},
    {"start_time": "2027-01-16T00:00:00", "end_time": "2027-01-15T00:00:00"},
])
async def test_invalid_search_times_are_rejected_without_breaking_next_query(tmp_path: Path, bad_bounds: dict) -> None:
    service, adapter, origin = _setup(tmp_path)
    store = adapter.message_archive
    assert store is not None
    store.record(SCOPE, ArchiveMessage("a", "123", NOW - 20, "讨论"))
    with bind_call_origin(origin):
        invalid = await service.execute("group_chat_search_messages", bad_bounds)
        next_query = await service.execute("group_chat_search_messages", {})
    assert invalid["ok"] is False and invalid["error"]["code"] == "invalid_argument"
    assert next_query["ok"] and [item["message_id"] for item in next_query["items"]] == ["a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"chat_type": "FriendMessage"}, {"self_id": "20000"}, {"adapter_id": "missing"},
                                   {"conversation_kind": "group", "conversation_id": "other"}])
async def test_private_foreign_or_inconsistent_origins_cannot_read_archive(tmp_path: Path, change: dict) -> None:
    service, _, origin = _setup(tmp_path)
    with bind_call_origin(replace(origin, **change)):
        result = await service.execute("group_chat_recent_messages", {})
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_missing_and_ended_call_scope_returns_stale_call(tmp_path: Path) -> None:
    service, _, origin = _setup(tmp_path)
    assert (await service.execute("group_chat_recent_messages", {}))["error"]["code"] == "stale_call"
    with bind_call_origin(origin):
        pass
    assert (await service.execute("group_chat_recent_messages", {}))["error"]["code"] == "stale_call"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,args", [
    ("group_chat_recent_messages", {"group_id": "999"}),
    ("group_chat_search_messages", {"adapter_id": "other"}),
    ("group_chat_get_message", {"message_id": 77}),
    ("group_chat_get_member", {"user_id": True}),
    ("group_chat_recent_messages", {"limit": True}),
    ("group_chat_recent_messages", {"limit": 101}),
    ("group_chat_search_messages", {"start_time": "2027-99-15T08:00:00"}),
    ("group_chat_find_members", {"query": ""}),
])
async def test_bad_or_cross_group_arguments_are_explicit_errors(tmp_path: Path, operation: str, args: dict) -> None:
    service, adapter, origin = _setup(tmp_path)
    with bind_call_origin(origin):
        result = await service.execute(operation, args)
    assert result["error"]["code"] == "invalid_argument"
    adapter._bot.get_msg.assert_not_awaited()
    adapter._bot.get_group_member_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_failure_is_logged_archive_unavailable_not_empty_success(tmp_path: Path, monkeypatch,
                                                                         caplog: pytest.LogCaptureFixture) -> None:
    service, adapter, origin = _setup(tmp_path)
    def fail(*args, **kwargs):
        raise OSError("模拟文件不可读")
    monkeypatch.setattr(adapter.message_archive, "query", fail)
    with bind_call_origin(origin), caplog.at_level(logging.ERROR):
        result = await service.execute("group_chat_recent_messages", {})
    assert result["error"]["code"] == "archive_unavailable"
    assert "items" not in result
    assert any("存储操作失败" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_corrupt_message_json_is_archive_failure_not_invalid_model_argument(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter.message_archive.record(SCOPE, ArchiveMessage("a", "123", NOW, "正文"))
    with sqlite3.connect(adapter.message_archive.database) as connection:
        connection.execute("UPDATE platform_messages SET mentions_json='{' WHERE message_id='a'")
    with bind_call_origin(origin):
        result = await service.execute("group_chat_get_message", {"message_id": "a"})
    assert result["error"]["code"] == "archive_unavailable"


@pytest.mark.asyncio
async def test_repeated_nicknames_return_candidates_and_card_matches(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_group_member_list.return_value = [
        {"group_id": 456, "user_id": 200, "nickname": "小明", "card": ""},
        {"group_id": 456, "user_id": 100, "nickname": "其他", "card": "小明"},
        {"group_id": 456, "user_id": 300, "nickname": "小明同学", "card": ""},
    ]
    with bind_call_origin(origin):
        result = await service.execute("group_chat_find_members", {"query": "小明"})
    assert [item["user_id"] for item in result["items"]] == ["100", "200", "300"]
    assert result["ambiguous"] is True and result["unique"] is False
    assert result["items"][0]["matched_by"] == ["card"]
    adapter._bot.get_group_member_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_after_2048_is_found_and_invalid_entries_mark_incomplete(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_group_member_list.return_value = [
        {"user_id": index, "nickname": "普通成员", "card": ""} for index in range(1, 2100)
    ] + [{"group_id": 456, "user_id": 5000, "nickname": "唯一目标", "card": ""},
         {"group_id": 999, "user_id": 6000, "nickname": "唯一目标", "card": ""}]
    with bind_call_origin(origin):
        result = await service.execute("group_chat_find_members", {"query": "唯一目标"})
    assert [item["user_id"] for item in result["items"]] == ["5000"]
    assert result["coverage"]["complete"] is False and result["truncated"] is True
    assert result["unique"] is False


@pytest.mark.asyncio
async def test_member_pagination_uses_snapshot_and_expired_cursor_requires_restart(tmp_path: Path) -> None:
    _, adapter, origin = _setup(tmp_path)
    clock = [10.0]
    service = GroupChatService(clock=lambda: clock[0])
    adapter._bot.get_group_member_list.return_value = [
        {"user_id": value, "nickname": "同名", "card": ""} for value in (100, 200, 300)
    ]
    with bind_call_origin(origin):
        first = await service.execute("group_chat_find_members", {"query": "同名", "limit": 1})
        adapter._bot.get_group_member_list.return_value = []
        second = await service.execute("group_chat_find_members", {"query": "同名", "limit": 2, "cursor": first["next_cursor"]})
        wrong = await service.execute("group_chat_find_members", {"query": "另一个名字", "cursor": first["next_cursor"]})
        clock[0] = 100
        expired = await service.execute("group_chat_find_members", {"query": "同名", "cursor": first["next_cursor"]})
    assert [item["user_id"] for item in first["items"] + second["items"]] == ["100", "200", "300"]
    assert adapter._bot.get_group_member_list.await_count == 1
    assert wrong["error"]["code"] == "invalid_argument"
    assert expired["error"]["code"] == "cursor_expired"


@pytest.mark.asyncio
async def test_connection_generation_invalidates_member_cursor_and_cached_roster(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_group_member_list.return_value = [
        {"user_id": 100, "nickname": "同名", "card": ""}, {"user_id": 200, "nickname": "同名", "card": ""},
    ]
    with bind_call_origin(origin):
        first = await service.execute("group_chat_find_members", {"query": "同名", "limit": 1})
        adapter._connection_generation += 1
        invalid = await service.execute("group_chat_find_members", {"query": "同名", "cursor": first["next_cursor"]})
        adapter._bot.get_group_member_list.return_value = [{"user_id": 300, "nickname": "同名", "card": ""}]
        fresh = await service.execute("group_chat_find_members", {"query": "同名"})
    assert invalid["error"]["code"] == "stale_call"
    assert [item["user_id"] for item in fresh["items"]] == ["300"]
    assert adapter._bot.get_group_member_list.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [{"group_id": 999, "user_id": 123}, {"group_id": 456, "user_id": 789},
                                    {"user_id": 123}, {"group_id": 456, "user_id": 123, "self_id": 20000}])
async def test_member_detail_rejects_missing_or_conflicting_identity(tmp_path: Path, reply: dict) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_group_member_info.return_value = reply
    with bind_call_origin(origin):
        result = await service.execute("group_chat_get_member", {"user_id": "123"})
    assert result["error"]["code"] == "unverified_target"


@pytest.mark.asyncio
async def test_member_detail_returns_confirmed_current_group_identity(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_group_member_info.return_value = {"group_id": 456, "user_id": 123, "nickname": "甲", "card": "名片"}
    with bind_call_origin(origin):
        result = await service.execute("group_chat_get_member", {"user_id": "123"})
    assert result["ok"] and result["item"] == {"user_id": "123", "nickname": "甲", "card": "名片", "verified": True}
    adapter._bot.get_group_member_info.assert_awaited_once_with(group_id=456, user_id=123, no_cache=True)


@pytest.mark.asyncio
async def test_verified_message_backfill_then_archive_hit_preserves_sender_and_budget(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_msg.return_value = _raw(text="长" * 1000)
    with bind_call_origin(origin):
        first = await service.execute("group_chat_get_message", {"message_id": "77"}, limits=GroupChatLimits(text_budget=128))
        second = await service.execute("group_chat_get_message", {"message_id": "77"})
    assert first["source"] == "adapter_verified" and first["item"]["sender_id"] == "123"
    assert first["item"]["mentions"] == ["789"] and first["truncated"] is True
    assert len(first["item"]["text"]) == 128
    assert second["source"] == "local_archive" and second["item"]["text"] == "长" * 1000
    adapter._bot.get_msg.assert_awaited_once_with(message_id=77)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"group_id": 999}, {"self_id": 20000}, {"message_id": 88}, {"time": None},
                                   {"time": True}, {"message_type": "private"}, {"user_id": 999}])
async def test_message_backfill_rejects_cross_scope_id_and_unverified_time(tmp_path: Path, change: dict) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_msg.return_value = {**_raw(), **change}
    with bind_call_origin(origin):
        result = await service.execute("group_chat_get_message", {"message_id": "77"})
    assert result["error"]["code"] == "unverified_target"
    assert not adapter.message_archive.database.exists()


@pytest.mark.asyncio
async def test_deleted_recalled_and_expired_messages_do_not_trigger_backfill(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    store = adapter.message_archive
    store.record(SCOPE, ArchiveMessage("deleted", "123", NOW, "待删除"))
    store.delete(SCOPE, message_ids=["deleted"], expected_revision=0)
    store.recall(SCOPE, "recalled")
    store.record(SCOPE, ArchiveMessage("expired", "123", NOW - 31 * 86400, "过期"))
    with bind_call_origin(origin):
        for message_id in ("deleted", "recalled", "expired"):
            result = await service.execute("group_chat_get_message", {"message_id": message_id})
            assert result["error"]["code"] == "message_" + message_id
    adapter._bot.get_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_horizon_blocks_missing_old_message_after_remote_time_verification(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter.message_archive.delete(SCOPE, expected_revision=0)
    adapter._bot.get_msg.return_value = _raw()
    with bind_call_origin(origin):
        result = await service.execute("group_chat_get_message", {"message_id": "77"})
    assert result["error"]["code"] == "message_deleted_or_expired"
    assert adapter.message_archive.get(SCOPE, "77") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["account", "connection", "scope_end", "instance", "group_disabled", "deleted"])
async def test_inflight_message_lookup_revalidates_before_releasing_or_saving_body(tmp_path: Path, change: str) -> None:
    service, adapter, origin = _setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def lookup(**kwargs):
        entered.set()
        await release.wait()
        return _raw()
    adapter._bot.get_msg.side_effect = lookup
    with bind_call_origin(origin):
        task = asyncio.create_task(service.execute("group_chat_get_message", {"message_id": "77"}))
        await entered.wait()
        if change == "account":
            adapter.bot_self_id = adapter.client_self_id = "20000"
        elif change == "connection":
            adapter._connection_generation += 1
        elif change == "instance":
            _setup(tmp_path / "replacement")
        elif change == "group_disabled":
            adapter.config.settings["enable_group"] = False
        elif change == "deleted":
            adapter.message_archive.delete(SCOPE, message_ids=["77"], expected_revision=0)
        if change != "scope_end":
            release.set()
            result = await task
    if change == "scope_end":
        release.set()
        result = await task
    assert result["ok"] is False
    row = adapter.message_archive.get(SCOPE, "77")
    assert row is None or row["status"] == "deleted"


@pytest.mark.asyncio
async def test_route_switch_with_real_persistent_generations_discards_inflight_result(tmp_path: Path) -> None:
    service, adapter, _ = _setup(tmp_path)
    adapter.agent_route_store = AgentRouteStore(tmp_path / "platform.db")
    adapter.config.session_bindings = {"group": {"mode": "value", "provider": "edictum", "config_name": "a"}}
    await adapter._handle_group_message(_raw())
    event = adapter._event_queue.get_nowait()
    assert event.call_origin.agent_route_generation == 1
    entered, release = asyncio.Event(), asyncio.Event()
    async def members(**kwargs):
        entered.set()
        await release.wait()
        return [{"group_id": 456, "user_id": 123, "nickname": "甲", "card": ""}]
    adapter._bot.get_group_member_list.side_effect = members
    with bind_call_origin(event.call_origin):
        task = asyncio.create_task(service.execute("group_chat_find_members", {"query": "甲"}))
        await entered.wait()
        adapter.config.session_bindings["group"]["config_name"] = "b"
        assert adapter.apply_agent_routes() == 1
        release.set()
        result = await task
    assert result["error"]["code"] == "stale_call"


@pytest.mark.asyncio
async def test_enabling_first_type_binding_invalidates_legacy_frozen_origin(tmp_path: Path) -> None:
    service, adapter, _ = _setup(tmp_path)
    await adapter._handle_group_message(_raw())
    event = adapter._event_queue.get_nowait()
    assert event.call_origin.agent_route_generation == 0
    adapter.config.session_bindings = {"group": {"mode": "inherit"}}
    with bind_call_origin(event.call_origin):
        result = await service.execute("group_chat_recent_messages", {})
    assert result["error"]["code"] == "stale_call"


@pytest.mark.asyncio
async def test_cancellation_propagates_and_does_not_store_unfinished_lookup(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    entered = asyncio.Event()
    async def lookup(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    adapter._bot.get_msg.side_effect = lookup
    with bind_call_origin(origin):
        task = asyncio.create_task(service.execute("group_chat_get_message", {"message_id": "77"}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not adapter.message_archive.database.exists()


@pytest.mark.asyncio
async def test_misskey_room_can_query_archive_and_reports_unsupported_member_read(tmp_path: Path) -> None:
    registry = PlatformAdapterRegistry()
    registry.register("misskey", MisskeyAdapter)
    manager = PlatformAdapterManager(registry)
    adapter = manager.add_adapter(PlatformConfig(id="mk", type="misskey", settings={}))
    assert isinstance(adapter, MisskeyAdapter)
    adapter.bot_self_id = adapter.client_self_id = "bot"
    adapter.message_archive = PlatformMessageStore(tmp_path / "platform.db", "mk", clock=lambda: NOW)
    scope = MessageScope("mk", "bot", "group", "room")
    adapter.message_archive.record(scope, ArchiveMessage("id", "member", NOW, "房间讨论"))
    set_current_adapter_manager(manager)
    service = GroupChatService()
    origin = CallOrigin("mk", "bot", "GroupMessage", "room", "member", "id", "request")
    with bind_call_origin(origin):
        history = await service.execute("group_chat_recent_messages", {})
        members = await service.execute("group_chat_find_members", {"query": "成员"})
        missing = await service.execute("group_chat_get_message", {"message_id": "missing"})
    assert history["items"][0]["message_id"] == "id"
    assert members["error"]["code"] == missing["error"]["code"] == "unsupported"


@pytest.mark.asyncio
async def test_platform_missing_action_is_learned_and_connection_reset_allows_new_query(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_group_member_list.side_effect = ActionFailed({"retcode": 10002})
    with bind_call_origin(origin):
        first = await service.execute("group_chat_find_members", {"query": "甲"})
        second = await service.execute("group_chat_find_members", {"query": "甲"})
        assert first["error"]["code"] == second["error"]["code"] == "unsupported"
        assert adapter._bot.get_group_member_list.await_count == 1
        assert adapter.group_chat_capabilities()["member_list"]["state"] == "unsupported"
        adapter._connection_generation += 1
        adapter._bot.get_group_member_list.side_effect = None
        adapter._bot.get_group_member_list.return_value = []
        reset = await service.execute("group_chat_find_members", {"query": "甲"})
    assert reset["ok"] is True and reset["items"] == []
    assert adapter._bot.get_group_member_list.await_count == 2


@pytest.mark.asyncio
async def test_waiting_for_read_slot_has_total_timeout_and_logs_failure(tmp_path: Path, monkeypatch,
                                                                     caplog: pytest.LogCaptureFixture) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._message_lookup_slots = asyncio.Semaphore(0)
    wait_for = asyncio.wait_for
    async def short_wait(awaitable, timeout):
        return await wait_for(awaitable, 0.02)
    monkeypatch.setattr("satrap.core.platform.onebot.group_chat.asyncio.wait_for", short_wait)
    with bind_call_origin(origin), caplog.at_level(logging.WARNING):
        result = await service.execute("group_chat_get_message", {"message_id": "77"})
    assert result["error"]["code"] == "unavailable" and result["error"]["retryable"] is True
    assert any("平台读取暂不可用" in record.getMessage() for record in caplog.records)
    adapter._bot.get_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_while_waiting_for_slot_does_not_submit_old_request(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    entered = asyncio.Event()
    class ObservedSemaphore(asyncio.Semaphore):
        async def acquire(self):
            entered.set()
            return await super().acquire()
    adapter._message_lookup_slots = ObservedSemaphore(0)
    with bind_call_origin(origin):
        task = asyncio.create_task(service.execute("group_chat_get_message", {"message_id": "77"}))
        await entered.wait()
        adapter._connection_generation += 1
        adapter._message_lookup_slots.release()
        result = await task
    assert result["error"]["code"] == "stale_call"
    adapter._bot.get_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_platform_time_cannot_be_replaced_by_fresh_local_time_on_backfill(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_msg.return_value = {**_raw(), "time": 0}
    with bind_call_origin(origin):
        result = await service.execute("group_chat_get_message", {"message_id": "77"})
    assert result["error"]["code"] == "message_deleted_or_expired"
    assert not adapter.message_archive.database.exists()


class _FutureAdapter(PlatformAdapter):
    """用于证明宿主没有依赖 QQ 字段和数字 ID 的虚拟平台"""

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(id=self.config.id, name="future", adapter_display_name="未来平台")

    async def run(self) -> None:
        pass

    def group_chat_capabilities(self) -> dict[str, dict[str, str]]:
        result = super().group_chat_capabilities()
        for name in ("member_list", "member_info", "message_lookup"):
            result[name] = {"state": "supported", "reason": "virtual_adapter"}
        return result

    async def group_chat_members(self, scope: MessageScope) -> MemberSnapshot:
        return MemberSnapshot(scope, (MemberRecord("user:甲", "甲", "名片"),), NOW, True)

    async def group_chat_member(self, scope: MessageScope, user_id: str) -> VerifiedMember:
        return VerifiedMember(scope, MemberRecord(user_id, "甲", "名片"), NOW)

    async def group_chat_message(self, scope: MessageScope, message_id: str) -> VerifiedMessage:
        return VerifiedMessage(scope, ArchiveMessage(message_id, "user:甲", NOW, "未来平台原文"))


@pytest.mark.asyncio
async def test_new_platform_with_unicode_string_ids_requires_no_host_branch_changes(tmp_path: Path) -> None:
    registry = PlatformAdapterRegistry()
    registry.register("future", _FutureAdapter)
    manager = PlatformAdapterManager(registry)
    adapter = manager.add_adapter(PlatformConfig(id="future", type="future"))
    assert isinstance(adapter, _FutureAdapter)
    adapter.client_self_id = "robot:一"
    adapter.message_archive = PlatformMessageStore(tmp_path / "platform.db", "future", clock=lambda: NOW)
    set_current_adapter_manager(manager)
    origin = CallOrigin("future", "robot:一", "GroupMessage", "room/群", "user:甲", "message:甲", "request")
    service = GroupChatService()
    with bind_call_origin(origin):
        members = await service.execute("group_chat_find_members", {"query": "名片"})
        member = await service.execute("group_chat_get_member", {"user_id": "user:甲"})
        message = await service.execute("group_chat_get_message", {"message_id": "message:甲"})
    assert members["items"][0]["user_id"] == member["item"]["user_id"] == message["item"]["sender_id"] == "user:甲"
    assert message["item"]["message_id"] == "message:甲"
