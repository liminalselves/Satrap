"""业务链路场景与集中边界契约"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from unittest.mock import AsyncMock
import asyncio
import logging
import sqlite3
from aiocqhttp.exceptions import ActionFailed, ApiNotAvailable
import pytest
from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.core.config.agent_routing import AgentRouteStore
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.group_chat.service import GroupChatService
from satrap.core.group_chat.types import GroupChatLimits
from satrap.core.platform import PlatformAdapter, PlatformAdapterManager, PlatformAdapterRegistry, PlatformConfig
from satrap.core.platform import current_adapter_manager, set_current_adapter_manager
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.misskey.adapter import MisskeyAdapter
from satrap.core.platform.event import PlatformMetadata
from types import SimpleNamespace
from unittest.mock import Mock
import threading
from satrap.core.group_chat.types import GroupChatError
from support.group_read import NOW, _FutureAdapter, _setup
from typing import cast
from satrap.core.group_chat.reply import current_reply_turn
from satrap.core.call_context import bind_tool_workflow
from satrap.core.components import Plain, PlatformComponentType
from satrap.core.platform.receipt import SendReceipt
from satrap.core.platform.event import MessageEvent
from satrap.core.type import MessageMember, PlatformMessage, PlatformMessageType
from support.group_reply import COMPONENTS, SCOPE as _group_chat_reply_SCOPE, owner, restore_manager, send_mock, setup
import yaml
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.edictum.plugin_config import parse_config_schema
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.type import LLMCallResponse
from satrap.edictum import AsyncSimpleSession, SimpleSession
from support.group_models import AsyncModel, Invoker, Model, PLUGIN, Script, session

_group_chat_service_SCOPE = MessageScope("ob", "10000", "group", "456")

@pytest.fixture()
def _manager_scope():
    """恢复进程级管理器, 各测试不依赖其它测试留下的适配器"""
    previous = current_adapter_manager()
    yield
    set_current_adapter_manager(previous)

def _raw(message_id: int = 77, text: str = "原始平台消息") -> dict:
    """构造 OneBot 可核验群消息"""
    return {"message_type": "group", "self_id": 10000, "group_id": 456, "message_id": message_id,
            "user_id": 123, "sender": {"user_id": 123, "nickname": "甲", "card": "群名片"}, "time": NOW - 10,
            "message": [{"type": "text", "data": {"text": text}}, {"type": "at", "data": {"qq": 789}}]}

@pytest.mark.usefixtures('_manager_scope')
@pytest.mark.asyncio
async def test_local_queries_preserve_sender_ids_and_never_use_model_history(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    store = adapter.message_archive
    store.record(_group_chat_service_SCOPE, ArchiveMessage("a", "123", NOW - 20, "讨论 A", mentions=["789"]))
    store.record(_group_chat_service_SCOPE, ArchiveMessage("b", "789", NOW - 10, "讨论 B"))
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

@pytest.mark.usefixtures('_manager_scope')
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
        store.record(_group_chat_service_SCOPE, ArchiveMessage(name, "123", when, "讨论"))
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

@pytest.mark.usefixtures('_manager_scope')
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
    store.record(_group_chat_service_SCOPE, ArchiveMessage("a", "123", NOW - 20, "讨论"))
    with bind_call_origin(origin):
        invalid = await service.execute("group_chat_search_messages", bad_bounds)
        next_query = await service.execute("group_chat_search_messages", {})
    assert invalid["ok"] is False and invalid["error"]["code"] == "invalid_argument"
    assert next_query["ok"] and [item["message_id"] for item in next_query["items"]] == ["a"]

@pytest.mark.usefixtures('_manager_scope')
@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"chat_type": "FriendMessage"}, {"self_id": "20000"}, {"adapter_id": "missing"},
                                   {"conversation_kind": "group", "conversation_id": "other"}])
async def test_private_foreign_or_inconsistent_origins_cannot_read_archive(tmp_path: Path, change: dict) -> None:
    service, _, origin = _setup(tmp_path)
    with bind_call_origin(replace(origin, **change)):
        result = await service.execute("group_chat_recent_messages", {})
    assert result["ok"] is False

@pytest.mark.usefixtures('_manager_scope')
@pytest.mark.asyncio
async def test_missing_and_ended_call_scope_returns_stale_call(tmp_path: Path) -> None:
    service, _, origin = _setup(tmp_path)
    assert (await service.execute("group_chat_recent_messages", {}))["error"]["code"] == "stale_call"
    with bind_call_origin(origin):
        pass
    assert (await service.execute("group_chat_recent_messages", {}))["error"]["code"] == "stale_call"

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
@pytest.mark.asyncio
async def test_corrupt_message_json_is_archive_failure_not_invalid_model_argument(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter.message_archive.record(_group_chat_service_SCOPE, ArchiveMessage("a", "123", NOW, "正文"))
    with sqlite3.connect(adapter.message_archive.database) as connection:
        connection.execute("UPDATE platform_messages SET mentions_json='{' WHERE message_id='a'")
    with bind_call_origin(origin):
        result = await service.execute("group_chat_get_message", {"message_id": "a"})
    assert result["error"]["code"] == "archive_unavailable"

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [{"group_id": 999, "user_id": 123}, {"group_id": 456, "user_id": 789},
                                    {"user_id": 123}, {"group_id": 456, "user_id": 123, "self_id": 20000}])
async def test_member_detail_rejects_missing_or_conflicting_identity(tmp_path: Path, reply: dict) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_group_member_info.return_value = reply
    with bind_call_origin(origin):
        result = await service.execute("group_chat_get_member", {"user_id": "123"})
    assert result["error"]["code"] == "unverified_target"

@pytest.mark.usefixtures('_manager_scope')
@pytest.mark.asyncio
async def test_member_detail_returns_confirmed_current_group_identity(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_group_member_info.return_value = {"group_id": 456, "user_id": 123, "nickname": "甲", "card": "名片"}
    with bind_call_origin(origin):
        result = await service.execute("group_chat_get_member", {"user_id": "123"})
    assert result["ok"] and result["item"] == {"user_id": "123", "nickname": "甲", "card": "名片", "role": "", "verified": True}
    adapter._bot.get_group_member_info.assert_awaited_once_with(group_id=456, user_id=123, no_cache=True)

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
@pytest.mark.asyncio
async def test_deleted_recalled_and_expired_messages_do_not_trigger_backfill(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    store = adapter.message_archive
    store.record(_group_chat_service_SCOPE, ArchiveMessage("deleted", "123", NOW, "待删除"))
    store.delete(_group_chat_service_SCOPE, message_ids=["deleted"], expected_revision=0)
    store.recall(_group_chat_service_SCOPE, "recalled")
    store.record(_group_chat_service_SCOPE, ArchiveMessage("expired", "123", NOW - 31 * 86400, "过期"))
    with bind_call_origin(origin):
        for message_id in ("deleted", "recalled", "expired"):
            result = await service.execute("group_chat_get_message", {"message_id": message_id})
            assert result["error"]["code"] == "message_" + message_id
    adapter._bot.get_msg.assert_not_awaited()

@pytest.mark.usefixtures('_manager_scope')
@pytest.mark.asyncio
async def test_clear_horizon_blocks_missing_old_message_after_remote_time_verification(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter.message_archive.delete(_group_chat_service_SCOPE, expected_revision=0)
    adapter._bot.get_msg.return_value = _raw()
    with bind_call_origin(origin):
        result = await service.execute("group_chat_get_message", {"message_id": "77"})
    assert result["error"]["code"] == "message_deleted_or_expired"
    assert adapter.message_archive.get(_group_chat_service_SCOPE, "77") is None

@pytest.mark.usefixtures('_manager_scope')
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
            adapter.message_archive.delete(_group_chat_service_SCOPE, message_ids=["77"], expected_revision=0)
        if change != "scope_end":
            release.set()
            result = await task
    if change == "scope_end":
        release.set()
        result = await task
    assert result["ok"] is False
    row = adapter.message_archive.get(_group_chat_service_SCOPE, "77")
    assert row is None or row["status"] == "deleted"

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
@pytest.mark.asyncio
async def test_client_api_unavailable_is_retryable_and_reconnection_allows_member_lookup(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_group_member_info.side_effect = ApiNotAvailable()
    with bind_call_origin(origin):
        failed = await service.execute('group_chat_get_member', {'user_id': '123'})
        assert failed['error']['code'] == 'unavailable' and failed['error']['retryable'] is True
        adapter._bot.get_group_member_info.side_effect = None
        adapter._bot.get_group_member_info.return_value = {'group_id': 456, 'user_id': 123, 'nickname': '成员', 'card': ''}
        restored = await service.execute('group_chat_get_member', {'user_id': '123'})
    assert restored['ok'] and restored['item']['user_id'] == '123'

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
@pytest.mark.asyncio
async def test_zero_platform_time_cannot_be_replaced_by_fresh_local_time_on_backfill(tmp_path: Path) -> None:
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_msg.return_value = {**_raw(), "time": 0}
    with bind_call_origin(origin):
        result = await service.execute("group_chat_get_message", {"message_id": "77"})
    assert result["error"]["code"] == "message_deleted_or_expired"
    assert not adapter.message_archive.database.exists()

@pytest.mark.usefixtures('_manager_scope')
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

@pytest.mark.usefixtures('_manager_scope')
@pytest.mark.parametrize('kind', ['group', 'private'])
@pytest.mark.parametrize('result', ['current', 'changed', 'unavailable'])
async def test_both_origins_use_current_agent_revision_and_keep_group_only_checks(tmp_path, monkeypatch, kind, result):
    _, adapter, original = _setup(tmp_path)
    origin = replace(original, chat_type='GroupMessage' if kind == 'group' else 'FriendMessage',
        conversation_kind=kind, conversation_id=original.chat_id, agent_route_generation=1)
    read = Mock(return_value=1 if result == 'current' else 2)
    if result == 'unavailable':
        read.side_effect = OSError('database unavailable')
    monkeypatch.setattr(adapter, 'agent_route_store', SimpleNamespace(current_revision=read))
    group_route = Mock(return_value=({}, 0))
    monkeypatch.setattr(adapter, 'group_route', group_route)
    call = adapter.group_chat_scope if kind == 'group' else adapter.group_chat_private_scope
    if result == 'current':
        scope = await call(origin)
        assert scope.conversation_kind == kind
        assert group_route.call_count == int(kind == 'group')
    else:
        with pytest.raises(GroupChatError) as error:
            await call(origin)
        assert error.value.code == ('stale_call' if result == 'changed' else 'unavailable')
        assert error.value.retryable == (result == 'unavailable')
        group_route.assert_not_called()
    read.assert_called_once_with(origin.self_id, kind, origin.chat_id)

@pytest.mark.usefixtures('_manager_scope')
@pytest.mark.parametrize('kind', ['group', 'private'])
async def test_route_change_while_revision_read_is_pending_denies_old_origin(tmp_path, monkeypatch, kind):
    _, adapter, original = _setup(tmp_path)
    origin = replace(original, chat_type='GroupMessage' if kind == 'group' else 'FriendMessage',
        conversation_kind=kind, conversation_id=original.chat_id, agent_route_generation=1)
    started, released = threading.Event(), threading.Event()
    revision = [1]

    def read(*_):
        started.set()
        assert released.wait(3)
        return revision[0]

    monkeypatch.setattr(adapter, 'agent_route_store', SimpleNamespace(current_revision=read))
    group_route = Mock(return_value=({}, 0))
    monkeypatch.setattr(adapter, 'group_route', group_route)
    call = adapter.group_chat_scope if kind == 'group' else adapter.group_chat_private_scope
    task = asyncio.create_task(call(origin))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        revision[0] = 2
    finally:
        released.set()
    with pytest.raises(GroupChatError) as error:
        await task
    assert error.value.code == 'stale_call'
    group_route.assert_not_called()

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_prepare_is_atomic_sender_based_and_sends_only_once(tmp_path):
    adapter, event, service = setup(tmp_path)
    with owner(event) as turn:
        result = await service.execute("group_chat_reply", {"components": COMPONENTS})
        assert result["status"] == "prepared" and result["sent"] is False
        send_mock(adapter).assert_not_awaited()
        again = await service.execute("group_chat_reply", {"components": COMPONENTS})
        assert again["error"]["code"] == "already_prepared"
        assert await turn.commit("模型重复的最终文本")
        assert await turn.commit("再次调用宿主也不能重发")
    assert current_reply_turn() is None
    send_mock(adapter).assert_awaited_once()
    chain = send_mock(adapter).call_args.args[1]
    assert [c.type for c in chain.components] == [PlatformComponentType.Reply, PlatformComponentType.Plain, PlatformComponentType.At,
                                                PlatformComponentType.Plain, PlatformComponentType.At, PlatformComponentType.Plain]
    assert [c.qq for c in chain.components if c.type == PlatformComponentType.At] == ["123", "789"]
    assert chain.components[0].id == "77"
    assert "重复" not in "".join(c.text for c in chain.components if c.type == PlatformComponentType.Plain)

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("components", [[], [{"type": "image", "url": "x"}], [{"type": "mention", "user_id": "789", "source_message_id": "77"}],
    [{"type": "quote", "message_id": "77"}, {"type": "quote", "message_id": "77"}, {"type": "text", "text": "x"}],
    [{"type": "text", "text": "x", "group_id": "other"}], [{"type": "mention", "user_id": True}],
    [{"type": "quote", "message_id": "77"}], [{"type": "text", "text": "x" * 100001}],
])
async def test_invalid_components_never_leave_half_a_draft(tmp_path, components):
    adapter, event, service = setup(tmp_path)
    with owner(event) as turn:
        result = await service.execute("group_chat_reply", {"components": components})
        assert result["ok"] is False and turn.draft is None
        result = await service.execute("group_chat_reply", {"components": [{"type": "text", "text": "完整草稿"}]})
        assert result["ok"]
    send_mock(adapter).assert_not_awaited()

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_prepare_concurrency_keeps_one_draft(tmp_path):
    _, event, service = setup(tmp_path)
    with owner(event):
        results = await asyncio.gather(*(service.execute("group_chat_reply", {"components": COMPONENTS}) for _ in range(2)))
        assert sum(result["ok"] for result in results) == 1
        assert [result["error"]["code"] for result in results if not result["ok"]] == ["already_prepared"]

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "recalled", "unverified", "foreign", "connection", "disabled", "failed", "subagent", "route", "instance"])
async def test_untrusted_or_changed_targets_are_not_submitted(tmp_path, change):
    adapter, event, service = setup(tmp_path)
    store = adapter.message_archive
    assert store is not None
    if change == "unverified":
        import time
        store.record(_group_chat_reply_SCOPE, ArchiveMessage("bad", "123", time.time(), "未核验"))
        import sqlite3
        with sqlite3.connect(store.database) as connection:
            connection.execute("UPDATE platform_messages SET verified=0 WHERE message_id='bad'")
    if change == "foreign":
        adapter._bot.get_msg.return_value = {"message_type": "group", "self_id": 10000, "group_id": 999, "message_id": 88,
                                             "user_id": 123, "sender": {"user_id": 123}, "time": 1800000000, "message": []}
    with owner(event) as turn:
        if change == "subagent":
            with bind_tool_workflow(object()):
                result = await service.execute("group_chat_reply", {"components": COMPONENTS})
            assert result["error"]["code"] == "wrong_executor" and turn.draft is None
        elif change in {"unverified", "foreign"}:
            result = await service.execute("group_chat_reply", {"components": [{"type": "mention", "source_message_id": "bad" if change == "unverified" else "88"}]})
            assert result["ok"] is False and turn.draft is None
        else:
            assert (await service.execute("group_chat_reply", {"components": COMPONENTS}))["ok"]
            if change == "deleted":
                store.delete(_group_chat_reply_SCOPE, message_ids=["77"], expected_revision=0)
            elif change == "recalled":
                store.recall(_group_chat_reply_SCOPE, "77")
            elif change == "connection":
                adapter._bot = AsyncMock()
            elif change == "disabled":
                adapter.config.enable = False
            elif change == "failed":
                turn.abort()
            elif change == "route":
                adapter.config.session_bindings = {"group": {"mode": "value", "provider": "edictum", "config_name": "new-agent"}}
                adapter.apply_agent_routes()
            elif change == "instance":
                set_current_adapter_manager(PlatformAdapterManager(PlatformAdapterRegistry()))
            await turn.commit("不能兜底发送此正文")
    send_mock(adapter).assert_not_awaited()

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "partial", "failed", "unknown"])
async def test_receipts_are_preserved_without_fallback_resend(tmp_path, status):
    adapter, event, service = setup(tmp_path)
    receipt = SendReceipt(status, ("segment-1",) if status in {"success", "partial"} else (), reason="test")
    send_mock(adapter).return_value = receipt
    with owner(event) as turn:
        assert (await service.execute("group_chat_reply", {"components": COMPONENTS}))["ok"]
        await turn.commit("正文")
        await turn.commit("正文")
    assert event.last_business_receipt is receipt
    send_mock(adapter).assert_awaited_once()

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_cancellation_after_submit_is_unknown_and_cannot_resend(tmp_path):
    adapter, event, service = setup(tmp_path)
    started = asyncio.Event()
    async def slow(*args, **kwargs):
        started.set()
        await asyncio.Future()
    send_mock(adapter).side_effect = slow
    with owner(event) as turn:
        assert (await service.execute("group_chat_reply", {"components": COMPONENTS}))["ok"]
        task = asyncio.create_task(turn.commit("正文"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert event.last_business_receipt is not None and event.last_business_receipt.status == "unknown"
        await turn.commit("不能重发")
    send_mock(adapter).assert_awaited_once()

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["", "最终正文"])
async def test_fallback_uses_only_final_response_not_tool_preface(tmp_path, response):
    adapter, event, _ = setup(tmp_path)
    with owner(event) as turn:
        turn.capture("查询前的说明, 不能作为最终回复")
        assert await turn.commit(response)
    if not response:
        send_mock(adapter).assert_not_awaited()
    else:
        send_mock(adapter).assert_awaited_once()
        text = "".join(component.text for component in send_mock(adapter).call_args.args[1].components if component.type == PlatformComponentType.Plain)
        assert text.lstrip() == response

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_explicit_text_suppresses_auto_quote_and_mention_but_fallback_keeps_them(tmp_path):
    adapter, event, service = setup(tmp_path)
    with owner(event) as turn:
        assert (await service.execute("group_chat_reply", {"components": [{"type": "text", "text": "显式文本"}]}))["ok"]
        await turn.commit("不重复")
    assert [component.type for component in send_mock(adapter).call_args.args[1].components] == [PlatformComponentType.Plain]
    send_mock(adapter).reset_mock()
    with owner(event) as turn:
        event._has_send_oper = False
        await turn.commit("兜底文本")
    assert [component.type for component in send_mock(adapter).call_args.args[1].components] == [PlatformComponentType.Reply, PlatformComponentType.At, PlatformComponentType.Plain]

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_structured_reply_uses_actual_onebot_split_queue_and_archives_confirmed_segments(tmp_path, failed):
    from aiocqhttp.exceptions import ActionFailed
    adapter, event, service = setup(tmp_path)
    adapter.send_message = OneBotAdapter.send_message.__get__(adapter)
    adapter.config.settings["message_text_limit"] = 64
    adapter._bot.send_group_msg.side_effect = [{"message_id": 900}, ActionFailed({"retcode": 100, "msg": "模拟失败"}) if failed else {"message_id": 901}, {"message_id": 902}]
    components = [{"type": "quote", "message_id": "77"}, {"type": "mention", "source_message_id": "77"},
                  {"type": "text", "text": "甲" * 150}, {"type": "mention", "user_id": "789"}]
    with owner(event) as turn:
        assert (await service.execute("group_chat_reply", {"components": components}))["ok"]
        await turn.commit("不能重发")
        await turn.commit("再次提交")
    receipt = event.last_business_receipt
    assert receipt is not None and receipt.status == ("partial" if failed else "success")
    assert receipt.message_ids == (("900",) if failed else ("900", "901", "902"))
    calls = adapter._bot.send_group_msg.await_args_list
    assert len(calls) == (2 if failed else 3)
    assert [segment["type"] for segment in calls[0].kwargs["message"][:2]] == ["reply", "at"]
    if not failed:
        assert "".join(segment["data"]["text"] for call in calls for segment in call.kwargs["message"] if segment["type"] == "text") == "甲" * 150
        assert calls[-1].kwargs["message"][-1]["data"]["qq"] == "789"
    await adapter.drain_message_archive()
    assert adapter.message_archive is not None
    assert adapter.message_archive.get(_group_chat_reply_SCOPE, "900") is not None
    assert (adapter.message_archive.get(_group_chat_reply_SCOPE, "901") is None) == failed

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_ended_scope_copies_cannot_prepare_or_submit(tmp_path):
    adapter, event, service = setup(tmp_path)
    ready = asyncio.Event()
    async def copied():
        await ready.wait()
        return await service.execute("group_chat_reply", {"components": COMPONENTS})
    with owner(event):
        task = asyncio.create_task(copied())
    ready.set()
    result = await task
    assert result["error"]["code"] == "stale_call"
    send_mock(adapter).assert_not_awaited()

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_new_adapter_and_opaque_ids_use_same_reply_contract(tmp_path):
    class OpaqueAdapter(PlatformAdapter):
        async def run(self):
            return None

        def meta(self):
            from satrap.core.platform.event import PlatformMetadata
            return PlatformMetadata("扩展平台", self.config.id)

        def group_chat_capabilities(self):
            return {**super().group_chat_capabilities(), **{key: {"state": "supported"} for key in ("text", "quote", "mention")}}

    registry = PlatformAdapterRegistry()
    registry.register("opaque", OpaqueAdapter)
    manager = PlatformAdapterManager(registry)
    adapter = manager.add_adapter(PlatformConfig(id="extra", type="opaque"))
    assert isinstance(adapter, OpaqueAdapter)
    adapter.client_self_id = "bot:abc"
    adapter.message_archive = PlatformMessageStore(tmp_path / "extra.db", "extra")
    import time
    scope = MessageScope("extra", "bot:abc", "group", "room:abc")
    adapter.message_archive.record(scope, ArchiveMessage('msg:"<a>', 'member:"<b>', time.time(), "资料"))
    adapter.send_message = AsyncMock(return_value=SendReceipt("success", ("out:1",)))
    set_current_adapter_manager(manager)
    message = PlatformMessage()
    message.type = PlatformMessageType.GROUP_MESSAGE
    message.self_id, message.group_id, message.session_id = "bot:abc", "room:abc", "room:abc"
    message.message_id = 'msg:"<a>'
    message.sender = MessageMember(user_id='member:"<b>', nickname="甲")
    message.message = [Plain("新平台")]
    event = MessageEvent("新平台", message, adapter.meta(), message.session_id, adapter)
    with owner(event) as turn:
        result = await GroupChatService().execute("group_chat_reply", {"components": [
            {"type": "quote", "message_id": 'msg:"<a>'}, {"type": "mention", "user_id": 'member:"<b>'},
            {"type": "text", "text": " 保留字符"},
        ]})
        assert result["ok"]
        await turn.commit("只发送草稿")
    sent = cast(AsyncMock, adapter.send_message)
    sent.assert_awaited_once()
    assert sent.call_args.args[1].components[0].id == 'msg:"<a>'
    assert sent.call_args.args[1].components[1].qq == 'member:"<b>'

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_first_model_request_has_skill_tools_environment_and_no_early_output(tmp_path, asynchronous, stream):
    adapter, event, _ = setup(tmp_path)
    script = Script([LLMCallResponse("tools_call", "提前输出的正文", tool_calls=[{"id": "reply-1", "name": "group_chat_reply", "arguments": {"components": COMPONENTS}}]),
                     LLMCallResponse("message", "重复的最终文本")])
    instance = await session(tmp_path, script, asynchronous, stream)
    seen = []
    async def output_async(text):
        seen.append(text)
    def output_sync(text):
        seen.append(text)
    output = output_async if asynchronous else output_sync
    instance._wf.content_callback = output
    script.observe = lambda: send_mock(adapter).assert_not_awaited()
    await PipelineScheduler(cast(SessionManager, Invoker(instance, asynchronous))).execute(event)
    assert seen == []
    assert instance._wf.content_callback is output
    first, args = script.requests[0]
    assert first[0]["content"].startswith("基础提示词") and "<skill:group_chat>" in first[0]["content"]
    assert {tool["function"]["name"] for tool in args["tools"] if tool["function"]["name"].startswith("group_chat_")} == (set(yaml.safe_load((PLUGIN / "meta.yaml").read_text(encoding="utf-8"))["tools"]))
    assert '"speaker_id": "123"' in str(first) and '"source_message_id": "77"' in str(first)
    assert '"nickname": "机器人"' in str(first)
    send_mock(adapter).assert_awaited_once()
    assert "回复甲" in "".join(c.text for c in send_mock(adapter).call_args.args[1].components if c.type == PlatformComponentType.Plain)
    assert event.last_business_receipt is not None and event.last_business_receipt.status == "success"

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_failed_model_after_prepared_draft_never_sends(tmp_path, asynchronous):
    adapter, event, _ = setup(tmp_path)
    script = Script([LLMCallResponse("tools_call", "", tool_calls=[{"id": "r", "name": "group_chat_reply", "arguments": {"components": COMPONENTS}}]), RuntimeError("模型失败")])
    instance = await session(tmp_path, script, asynchronous, False)
    await PipelineScheduler(cast(SessionManager, Invoker(instance, asynchronous))).execute(event)
    send_mock(adapter).assert_not_awaited()

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_plugin_disable_enable_preserves_definitions_in_private_chat(tmp_path, asynchronous):
    adapter, event, _ = setup(tmp_path)
    script = Script([LLMCallResponse("message", "普通最终文本") for _ in range(3)])
    instance = await session(tmp_path, script, asynchronous, True)
    async def toggle(enabled):
        method = instance.enable_plugin if enabled else instance.disable_plugin
        if asynchronous:
            await method("group_chat")
        else:
            method("group_chat")
    await toggle(False)
    await PipelineScheduler(cast(SessionManager, Invoker(instance, asynchronous))).execute(event)
    assert "<skill:group_chat>" not in script.requests[0][0][0]["content"]
    assert not script.requests[0][1]["tools"]
    send_mock(adapter).reset_mock()
    await toggle(True)
    _, event, _ = setup(tmp_path)
    await PipelineScheduler(cast(SessionManager, Invoker(instance, asynchronous))).execute(event)
    assert script.requests[1][0][0]["content"].count("<skill:group_chat>") == 1
    adapter, private, _ = setup(tmp_path, private=True)
    await PipelineScheduler(cast(SessionManager, Invoker(instance, asynchronous))).execute(private)
    assert script.requests[2][1]["tools"] == script.requests[1][1]["tools"]
    assert "群聊环境资料" not in script.requests[2][0][-1]["content"]
    send_mock(adapter).assert_awaited_once()

@pytest.mark.usefixtures('restore_manager')
def test_plugin_catalog_schema_and_declared_capabilities():
    from satrap.expend.plugins.group_chat.tools import DEFINITIONS
    meta = yaml.safe_load((PLUGIN / "meta.yaml").read_text(encoding="utf-8"))
    assert set(meta["tools"]) == set(DEFINITIONS)
    schema = parse_config_schema(meta)
    assert schema["member_limit"].validate_strict(10) == 10
    with pytest.raises(ValueError):
        schema["member_limit"].validate_strict(51)
    catalog = PluginCatalog(preset_dir=PLUGIN.parent, user_dir=PLUGIN / "absent")
    entry = catalog.get("group_chat")
    assert entry is not None and entry.capabilities["skills"] == meta["skills"]

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_named_backend_agent_first_load_restart_and_plugin_disable(tmp_path, monkeypatch, asynchronous):
    import importlib
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.core.framework.providers import EdictumProvider
    from satrap.edictum.config import EdictumConfigManager
    from satrap.edictum.registry import create_default_edictum_type_registry
    adapter, event, _ = setup(tmp_path)
    script = Script([
        LLMCallResponse("tools_call", "查询前的文本", tool_calls=[{"id": "history", "name": "group_chat_recent_messages", "arguments": {"limit": 1}}]),
        LLMCallResponse("tools_call", "回复前的文本", tool_calls=[{"id": "reply", "name": "group_chat_reply", "arguments": {"components": COMPONENTS}}]),
        LLMCallResponse("message", "不再重复结构化正文"), LLMCallResponse("message", "重启后回复"), LLMCallResponse("message", "停用后回复"),
    ])
    registry = create_default_edictum_type_registry()
    catalog = PluginCatalog(preset_dir=PLUGIN.parent, user_dir=tmp_path / "user-plugins")
    configs = EdictumConfigManager(registry, tmp_path / "edictum.json")
    configs.plugin_catalog = catalog
    configs.create("group-agent", {"edictum_type": "async_simple" if asynchronous else "simple", "model_name": "test",
                                   "params": {"system_prompt": "群基础提示词", "stream": True}, "plugins": ["group_chat"]})
    manager = SessionManager(db_path=tmp_path / "agent.db", platform_id="ob")
    manager.plugin_environment = PluginEnvironment(session_type="platform", platform_type="onebot")
    provider = EdictumProvider(configs, registry, default_checkpoint_db=str(tmp_path / "agent.db"))
    provider.plugin_catalog = catalog
    manager.register_provider(provider)
    models = ModelConfigManager(storage_path=tmp_path / "models.json")
    models.update_llm_config("test", api_key="offline", model="test")
    manager.model_config_manager = models
    model = (AsyncModel if asynchronous else Model)(script)
    monkeypatch.setattr(importlib.import_module("satrap.core.framework.SessionManager"), "build_llm_from_config", lambda *args, **kwargs: model)
    manager.register_session_from_provider_config("edictum", "group-agent", session_id=event.session_id, extra_params={"adapter_id": "ob"})
    event.session_provider, event.session_type = "edictum", "group-agent"
    scheduler = PipelineScheduler(manager)
    await scheduler.execute(event)
    send_mock(adapter).assert_awaited_once()
    first, args = script.requests[0]
    assert first[0]["content"].count("<skill:group_chat>") == 1
    assert {tool["function"]["name"] for tool in args["tools"]} == (set(yaml.safe_load((PLUGIN / "meta.yaml").read_text(encoding="utf-8"))["tools"]))
    assert any(message["role"] == "tool" and '"message_id": "77"' in message["content"] for message in script.requests[1][0])
    await manager.unload_session_async(event.session_id)
    send_mock(adapter).reset_mock()
    event = MessageEvent("重启后检查", event.platform_message, adapter.meta(), event.session_id, adapter, "edictum", "group-agent")
    event.is_wake = True
    await scheduler.execute(event)
    assert script.requests[3][0][0]["content"].count("<skill:group_chat>") == 1
    send_mock(adapter).assert_awaited_once()
    configs.update("group-agent", {"plugins": []})
    results = await manager.reconcile_edictum_runtime_async(config_name="group-agent")
    assert results and results[0]["ok"]
    send_mock(adapter).reset_mock()
    event = MessageEvent("停用后检查", event.platform_message, adapter.meta(), event.session_id, adapter, "edictum", "group-agent")
    event.is_wake = True
    await scheduler.execute(event)
    assert "<skill:group_chat>" not in script.requests[4][0][0]["content"]
    assert script.requests[4][1]["tools"] == []
    send_mock(adapter).assert_awaited_once()
    await manager.unload_session_async(event.session_id)
