"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import pytest
from satrap.core.config.platform_messages import ArchiveMessage, MessageArchiveError, MessageScope
from satrap.core.config.platform_messages import PlatformMessageStore, archive_time
from satrap.core.config.platform_schema import PLATFORM_SCHEMA_VERSION, ensure_platform_tables
import asyncio
from time import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from satrap.core.backend import control_server as control
from satrap.core.config.platform_message_data import PlatformMessageDataService, platform_archive_catalog
from satrap.core.platform import registry
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.storage.layout import StorageLayout
import importlib
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.storage.maintenance import StorageMaintenanceService
from satrap.core.components import At, File, Node, Plain, Reply
from satrap.core.platform import PlatformConfig
from satrap.core.platform.event import MessageChain
from satrap.core.platform.misskey.adapter import MisskeyAdapter

NOW = 1_800_000_000.0

SCOPE = MessageScope("adapter", "bot", "group", "group-a")

def _platform_messages_store(tmp_path: Path, clock=lambda: NOW) -> PlatformMessageStore:
    """使用固定时钟创建独立的平台档案"""
    return PlatformMessageStore(tmp_path / "platform.db", "adapter", clock=clock)

def _message(message_id: str = "m1", *, sender: str = "u1", when: float = NOW - 10,
             text: str = "原始群消息", direction: str = "inbound") -> ArchiveMessage:
    """构造已核验的真实消息快照"""
    return ArchiveMessage(message_id, sender, when, text, nickname="用户", card="群名片", direction=direction,
                          reply_to_message_id="quote-id", mentions=["u2", "u3"], components=[{"type": "text"}])

def test_cold_reads_and_maintenance_do_not_create_database(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    assert store.get(SCOPE, "absent") is None
    result = store.query(SCOPE)
    assert result["items"] == [] and result["coverage"]["complete"] is False
    assert store.backfill_allowed(SCOPE, "absent")
    assert store.purge() == {"expired_count": 0, "backup_count": 0}
    assert list(tmp_path.iterdir()) == []
    cold = StorageLayout(tmp_path / "cold")
    assert StorageMaintenanceService(cold).expire_platform_messages({"future": 30}) == {"items": []}
    assert not cold.platforms_root.exists()

def test_real_platform_database_migration_preserves_context_and_configs(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    with sqlite3.connect(database) as connection:
        ensure_platform_tables(connection)
        connection.execute("CREATE TABLE chat_history(session_id TEXT, content TEXT)")
        connection.execute("INSERT INTO chat_history VALUES('session-1', '模型上下文')")
        connection.execute("INSERT INTO session_config_overrides VALUES('session-1', 'prompt', '{}', 1, 2, 1)")
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'group_chat_%'").fetchall():
            connection.execute(f'DROP TABLE "{table}"')
        for table in ("memory_refs", "memory_proposals", "memory_operations", "memory_audit", "memories", "reminder_operations",
                      "friend_actions", "friend_policies", "platform_message_policy", "platform_message_backups", "platform_messages", "platform_message_chats"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DROP TRIGGER group_actions_action_revision")
        for column in ("action_revision", "decision", "decided_by_json", "decision_note", "risk", "risk_source"):
            connection.execute(f"ALTER TABLE group_actions DROP COLUMN {column}")
        connection.execute("PRAGMA user_version=3")
    store = _platform_messages_store(tmp_path)
    assert store.query(SCOPE)["items"] == []
    assert store.record(SCOPE, _message())
    store.delete(SCOPE, expected_revision=0)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == PLATFORM_SCHEMA_VERSION
        assert connection.execute("SELECT content FROM chat_history").fetchone()[0] == "模型上下文"
        assert connection.execute("SELECT revision FROM session_config_overrides").fetchone()[0] == 2

def test_declared_missing_archive_table_is_damage_not_empty_history(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    store.record(SCOPE, _message())
    with sqlite3.connect(store.database) as connection:
        connection.execute("DROP TABLE platform_message_backups")
    with pytest.raises(RuntimeError, match="platform_message_backups"):
        store.query(SCOPE)
    with pytest.raises(RuntimeError, match="platform_message_backups"):
        store.record(SCOPE, _message("m2"))

def test_message_ids_are_isolated_by_account_kind_and_chat(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    scopes = [SCOPE, MessageScope("adapter", "bot", "group", "group-b"),
              MessageScope("adapter", "other-bot", "group", "group-a"),
              MessageScope("adapter", "bot", "private", "group-a"),
              MessageScope("adapter", "bot", "custom-topic", "group-a")]
    for index, scope in enumerate(scopes):
        assert store.record(scope, _message("-123", text=f"消息{index}"))
    for index, scope in enumerate(scopes):
        assert store.get(scope, "-123")["text"] == f"消息{index}"
        assert len(store.query(scope)["items"]) == 1
    wrong = MessageScope("other-adapter", "bot", "group", "group-a")
    with pytest.raises(MessageArchiveError) as failure:
        store.query(wrong)
    assert failure.value.code == "wrong_conversation"

def test_replay_and_outbound_echo_are_deduplicated_across_restart(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    assert store.record(SCOPE, _message(sender="bot"))
    restarted = _platform_messages_store(tmp_path)
    assert not restarted.record(SCOPE, _message(text="重复投递不能改原文", sender="bot", direction="outbound"))
    message = restarted.get(SCOPE, "m1")
    assert message["direction"] == "outbound" and message["text"] == "原始群消息"
    assert len(restarted.query(SCOPE)["items"]) == 1

def test_conflicting_echo_cannot_change_verified_sender_or_direction(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    store.record(SCOPE, _message())
    with pytest.raises(MessageArchiveError) as failure:
        store.record(SCOPE, _message(sender="bot", direction="outbound"))
    assert failure.value.code == "unverified_target"
    result = store.get(SCOPE, "m1")
    assert result["sender_id"] == "u1" and result["direction"] == "inbound"

def test_search_intersects_conditions_and_treats_keywords_as_plain_text(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    store.record(SCOPE, _message("a", text="讨论 100%_SQL 中文", when=NOW - 30))
    store.record(SCOPE, _message("b", text="讨论 100%_SQL 中文", when=NOW - 20, sender="u2"))
    store.record(SCOPE, _message("c", text="讨论普通消息", when=NOW - 10))
    assert [item["message_id"] for item in store.query(SCOPE, keyword="%_SQL", sender_id="u1")["items"]] == ["a"]
    assert store.query(SCOPE, keyword="' OR 1=1 --")["items"] == []
    start = "2027-01-15T15:59:35+08:00"
    end = "2027-01-15T07:59:45Z"
    assert archive_time(start) == NOW - 25
    assert [item["message_id"] for item in store.query(SCOPE, start_time=start, end_time=end)["items"]] == ["b"]
    result = store.query(SCOPE, keyword="没有此词")
    assert result["coverage"]["archived_from"] == NOW - 30
    assert result["coverage"]["archived_to"] == NOW - 10
    assert result["coverage"]["complete"] is False

def test_same_timestamp_pagination_is_stable_and_cursor_cannot_cross_scope_or_filter(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    for message_id in ("a", "b", "c", "d", "e"):
        store.record(SCOPE, _message(message_id))
    first = store.query(SCOPE, limit=2)
    assert [item["message_id"] for item in first["items"]] == ["d", "e"]
    assert first["has_more"]
    second = store.query(SCOPE, limit=2, cursor=first["next_cursor"])
    third = store.query(SCOPE, limit=2, cursor=second["next_cursor"])
    assert [item["message_id"] for item in second["items"] + third["items"]] == ["b", "c", "a"]
    assert third["has_more"] is False and third["next_cursor"] is None
    other = MessageScope("adapter", "bot", "group", "other")
    for scope, options in ((other, {}), (SCOPE, {"keyword": "不同筛选"}), (SCOPE, {"sender_id": "u2"})):
        with pytest.raises(ValueError, match="游标"):
            store.query(scope, cursor=first["next_cursor"], **options)
    assert [item["message_id"] for item in store.query(SCOPE, before_message_id="c")["items"]] == ["a", "b"]
    with pytest.raises(MessageArchiveError) as failure:
        store.query(SCOPE, before_message_id="other-group-message")
    assert failure.value.code == "message_not_found"

@pytest.mark.parametrize("options", [
    {"limit": True}, {"limit": 101}, {"text_budget": 127}, {"cursor": "bad"},
    {"start_time": "2027-01-15T07:59:00"}, {"start_time": "bad"},
    {"start_time": "2027-01-16T00:00:00Z", "end_time": "2027-01-15T00:00:00Z"},
])
def test_invalid_queries_are_not_empty_success(tmp_path: Path, options: dict) -> None:
    with pytest.raises(ValueError):
        _platform_messages_store(tmp_path).query(SCOPE, **options)

def test_text_budget_preserves_message_sender_and_quote_id(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    store.record(SCOPE, _message("a", text="中" * 100))
    store.record(SCOPE, _message("b", text="文" * 100))
    result = store.query(SCOPE, text_budget=128)
    assert sum(len(item["text"]) for item in result["items"]) == 128
    assert result["truncated"] is True
    for item in result["items"]:
        assert item["message_id"] in {"a", "b"} and item["sender_id"] == "u1"
        assert item["reply_to_message_id"] == "quote-id" and item["mentions"] == ["u2", "u3"]
    assert store.get(SCOPE, "b")["text"] == "文" * 100

def test_recall_before_message_arrival_blocks_replay_and_search(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    store.recall(SCOPE, "m1")
    assert not store.record(SCOPE, _message())
    assert store.get(SCOPE, "m1")["status"] == "recalled"
    assert store.get(SCOPE, "m1")["text"] == ""
    assert store.query(SCOPE)["items"] == []
    assert not store.backfill_allowed(SCOPE, "m1", NOW - 10)

def test_delete_clears_searchable_data_blocks_backfill_and_restores_only_same_scope(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    store.record(SCOPE, _message())
    deleted = store.delete(SCOPE, message_ids=["m1"], expected_revision=0)
    assert deleted["deleted_count"] == 1 and deleted["revision"] == 1
    assert store.query(SCOPE)["items"] == [] and not store.backfill_allowed(SCOPE, "m1")
    assert not store.record(SCOPE, _message(text="回源不能恢复"))
    row = store.get(SCOPE, "m1")
    assert row["text"] == row["sender_id"] == row["nickname"] == row["card"] == ""
    assert row["components"] == row["mentions"] == row["media"] == []
    wrong = MessageScope("adapter", "bot", "group", "other")
    with pytest.raises(MessageArchiveError) as failure:
        store.restore(wrong, deleted["backup_id"], expected_revision=0)
    assert failure.value.code == "backup_not_found"
    restored = store.restore(SCOPE, deleted["backup_id"], expected_revision=1)
    assert restored["restored_count"] == 1 and store.get(SCOPE, "m1")["text"] == "原始群消息"

def test_restoring_unknown_id_deletion_releases_only_its_tombstone(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    deleted = store.delete(SCOPE, message_ids=["unknown"], expected_revision=0)
    assert not store.backfill_allowed(SCOPE, "unknown")
    store.restore(SCOPE, deleted["backup_id"], expected_revision=1)
    assert store.backfill_allowed(SCOPE, "unknown")
    assert store.record(SCOPE, _message("unknown"))

def test_clear_restore_preserves_new_messages_and_previous_delete_state(tmp_path: Path) -> None:
    now = [NOW]
    store = _platform_messages_store(tmp_path, lambda: now[0])
    store.record(SCOPE, _message("a"))
    store.record(SCOPE, _message("b"))
    old_delete = store.delete(SCOPE, message_ids=["a"], expected_revision=0)
    cleared = store.delete(SCOPE, expected_revision=1)
    assert not store.backfill_allowed(SCOPE, "missing", NOW - 1)
    assert not store.record(SCOPE, _message("replayed", when=NOW - 1))
    now[0] += 10
    assert store.record(SCOPE, _message("new", when=NOW + 5))
    with pytest.raises(MessageArchiveError) as failure:
        store.restore(SCOPE, old_delete["backup_id"], expected_revision=2)
    assert failure.value.code == "backup_superseded"
    restored = store.restore(SCOPE, cleared["backup_id"], expected_revision=2)
    assert restored["restored_count"] == 2
    assert store.get(SCOPE, "a")["status"] == "deleted"
    assert [item["message_id"] for item in store.query(SCOPE)["items"]] == ["b", "new"]
    assert store.backfill_allowed(SCOPE, "missing", NOW - 1)
    assert store.restore(SCOPE, old_delete["backup_id"], expected_revision=3)["restored_count"] == 1
    assert store.get(SCOPE, "a")["status"] == "active"

def test_newer_delete_and_recall_cannot_be_undone_by_old_backup(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    store.record(SCOPE, _message("a"))
    store.record(SCOPE, _message("b"))
    deleted = store.delete(SCOPE, message_ids=["a", "b"], expected_revision=0)
    store.recall(SCOPE, "a")
    newer = store.delete(SCOPE, message_ids=["b"], expected_revision=2)
    with pytest.raises(MessageArchiveError) as failure:
        store.restore(SCOPE, deleted["backup_id"], expected_revision=3)
    assert failure.value.code == "backup_superseded"
    assert store.get(SCOPE, "a")["status"] == "recalled"
    assert store.get(SCOPE, "b")["status"] == "deleted"
    assert store.restore(SCOPE, newer["backup_id"], expected_revision=3)["restored_count"] == 1
    assert store.get(SCOPE, "b")["status"] == "deleted"
    restored = store.restore(SCOPE, deleted["backup_id"], expected_revision=4)
    assert restored["restored_count"] == 1 and restored["skipped_count"] == 1
    assert store.get(SCOPE, "b")["status"] == "active"

def test_concurrent_management_revision_is_rejected_without_partial_delete(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    store.record(SCOPE, _message("a"))
    store.record(SCOPE, _message("b"))
    store.delete(SCOPE, message_ids=["a"], expected_revision=0)
    with pytest.raises(MessageArchiveError) as failure:
        store.delete(SCOPE, message_ids=["b"], expected_revision=0)
    assert failure.value.code == "revision_conflict"
    assert store.get(SCOPE, "b")["status"] == "active"

def test_retention_hides_old_body_before_cleanup_and_purge_erases_backup(tmp_path: Path) -> None:
    now = [NOW]
    store = _platform_messages_store(tmp_path, lambda: now[0])
    store.record(SCOPE, _message("a"))
    store.record(SCOPE, _message("b"))
    deleted = store.delete(SCOPE, message_ids=["b"], expected_revision=0)
    now[0] += 31 * 86400
    assert store.get(SCOPE, "a")["status"] == "expired" and store.get(SCOPE, "a")["text"] == ""
    assert store.query(SCOPE)["items"] == []
    assert store.purge() == {"expired_count": 1, "backup_count": 1}
    assert not store.record(SCOPE, _message("a", when=now[0]))
    assert not store.backfill_allowed(SCOPE, "a")
    with pytest.raises(MessageArchiveError) as failure:
        store.restore(SCOPE, deleted["backup_id"], expected_revision=1)
    assert failure.value.code == "backup_not_found"
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("SELECT text FROM platform_messages WHERE message_id='a'").fetchone()[0] == ""
        assert connection.execute("SELECT COUNT(*) FROM platform_message_backups").fetchone()[0] == 0

def test_expired_backfill_is_not_stored_with_retrievable_body(tmp_path: Path) -> None:
    store = _platform_messages_store(tmp_path)
    assert not store.backfill_allowed(SCOPE, "old", NOW - 31 * 86400)
    assert store.record(SCOPE, _message("old", when=NOW - 31 * 86400))
    assert store.get(SCOPE, "old")["status"] == "expired"
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("SELECT text FROM platform_messages WHERE message_id='old'").fetchone()[0] == ""

def test_snapshot_boundaries_keep_delimiter_and_unicode_ids_distinct(tmp_path: Path) -> None:
    first = MessageScope("adapter", "bot", "group", "群:1\"\\")
    second = MessageScope("adapter", "bot", "group", "群:1")
    store = _platform_messages_store(tmp_path)
    store.record(first, _message("消息\"1", text="中文🙂"))
    assert first.key != second.key and store.get(second, "消息\"1") is None
    assert store.get(first, "消息\"1")["text"] == "中文🙂"
    with sqlite3.connect(store.database) as connection:
        key = connection.execute("SELECT scope_key FROM platform_message_chats").fetchone()[0]
        assert json.loads(key.split(":", 2)[2])[-1] == first.chat_id

class FutureAdapter(OneBotAdapter):
    """只声明额外对话类型, 禁止目录实例化适配器"""

    conversation_kinds = {"circle": "圈子讨论"}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("只读目录不能创建适配器或连接平台")

@pytest.fixture
def saved(tmp_path: Path, monkeypatch):
    document = {"data_root": str(tmp_path / "data"), "platforms": [
        {"id": "alpha", "type": "future"}, {"id": "beta", "type": "future"}]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    monkeypatch.setitem(registry._mapping, "future", FutureAdapter)
    layout = StorageLayout(tmp_path / "data")
    scopes = [MessageScope("alpha", "bot-1", "circle", "会话 A"), MessageScope("alpha", "bot-2", "circle", "会话 A"),
              MessageScope("beta", "bot-1", "circle", "会话 B")]
    for scope in scopes:
        layout.ensure_platform(scope.adapter_id, platform_type="future")
        store = PlatformMessageStore(layout.platform_db(scope.adapter_id), scope.adapter_id, retention_days=7)
        store.record(scope, ArchiveMessage("same", "member", time(), "真实原文", nickname="昵称"), label="讨论 % _")
    with sqlite3.connect(layout.platform_db("alpha")) as connection:
        connection.execute("CREATE TABLE chat_history(content TEXT)")
        connection.execute("INSERT INTO chat_history VALUES('模型上下文')")
    return layout, document, scopes

def _payload(scope: MessageScope, **values: Any) -> dict[str, Any]:
    """将明确选中的档案身份组成管理请求"""
    return {"platform_id": scope.adapter_id, "self_id": scope.self_id, "conversation_kind": scope.conversation_kind,
            "chat_id": scope.chat_id, **values}

async def _request(path: str, payload: dict[str, Any] | None = None, *, authorized: bool = True) -> tuple[int, dict[str, Any]]:
    """经完整认证与路由分派读取控制响应, 不启动外部服务器"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    method = "POST" if payload is not None else "GET"
    headers = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
               f"Authorization: Bearer {control._CONTROL_AUTH.token if authorized else 'invalid'}\r\n"
               f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("utf-8")
    reader = asyncio.StreamReader()
    reader.feed_data(headers + body)
    reader.feed_eof()
    response = bytearray()
    writer = SimpleNamespace(write=response.extend, drain=AsyncMock(), close=Mock())
    await control._handle_request(reader, cast(asyncio.StreamWriter, writer))
    header, content = bytes(response).split(b"\r\n\r\n", 1)
    return int(header.split()[1]), json.loads(content.decode("utf-8"))

@pytest.mark.asyncio
async def test_catalog_and_archive_http_read_search_and_message_details(saved) -> None:
    """目录聚合与 HTTP 读取共享动态对话类型, 账号及跨平台分页"""
    layout, document, scopes = saved
    catalog = platform_archive_catalog(layout, document, {"limit": "2"})
    assert catalog["total"] == 3 and len(catalog["items"]) == 2
    assert catalog["conversation_kinds"] == [{"value": "circle", "label": "圈子讨论"}]
    assert catalog["self_ids"] == ["bot-1", "bot-2"]
    second = platform_archive_catalog(layout, document, {"offset": "2", "limit": "2"})
    assert len(second["items"]) == 1 and second["items"][0]["platform_id"] == "beta"
    selected = platform_archive_catalog(layout, document, {"platform_id": "alpha", "self_id": "bot-2", "q": "% _"})
    assert selected["total"] == 1 and selected["items"][0]["message_count"] == 1
    assert platform_archive_catalog(layout, document, {"q": "%%%%"})["total"] == 0

    status, http_catalog = await _request("/config/conversations/archive?platform_id=alpha&conversation_kind=circle")
    assert status == 200 and http_catalog["total"] == 2
    status, data = await _request("/config/conversations/archive/data", _payload(scopes[0], keyword="原文", sender_id="member"))
    assert status == 200 and data["items"][0]["message_id"] == "same"
    assert data["retention_days"] == 7 and data["scope"]["self_id"] == "bot-1"
    assert data["backups"] == [] and data["coverage"]["complete"] is False
    status, message = await _request("/config/conversations/archive/data", _payload(scopes[1], action="message", message_id="same"))
    assert status == 200 and message["item"]["nickname"] == "昵称"
    status, missing = await _request("/config/conversations/archive/data", _payload(scopes[0], action="message", message_id="other"))
    assert status == 404 and missing["code"] == "not_found"

@pytest.mark.asyncio
async def test_delete_clear_restore_and_revision_preserve_other_scopes_and_context(saved) -> None:
    layout, _, scopes = saved
    route = "/config/conversations/archive/data"
    status, deleted = await _request(route, _payload(scopes[0], action="delete", message_ids=["same"], expected_revision=0))
    assert status == 200 and deleted["deleted_count"] == 1
    status, state = await _request(route, _payload(scopes[0]))
    assert status == 200 and state["items"] == [] and state["revision"] == 1
    assert set(state["backups"][0]) == {"backup_id", "action", "created_at", "expires_at"}
    for scope in scopes[1:]:
        status, data = await _request(route, _payload(scope))
        assert status == 200 and data["items"][0]["text"] == "真实原文"
    status, conflict = await _request(route, _payload(scopes[0], action="clear", expected_revision=0))
    assert status == 409 and conflict["code"] == "revision_conflict"
    status, restored = await _request(route, _payload(scopes[0], action="restore", backup_id=deleted["backup_id"], expected_revision=1))
    assert status == 200 and restored["restored_count"] == 1
    status, cleared = await _request(route, _payload(scopes[0], action="clear", expected_revision=2))
    assert status == 200 and cleared["deleted_count"] == 1
    status, message = await _request(route, _payload(scopes[0], action="message", message_id="same"))
    assert status == 200 and message["item"]["status"] == "deleted" and message["item"]["text"] == ""
    status, cross_scope = await _request(route, _payload(scopes[1], action="restore", backup_id=cleared["backup_id"], expected_revision=0))
    assert status == 404 and cross_scope["code"] == "backup_not_found"
    with sqlite3.connect(layout.platform_db("alpha")) as connection:
        assert connection.execute("SELECT content FROM chat_history").fetchone()[0] == "模型上下文"

@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"action": "edit", "text": "篡改"}, {"action": "read", "database": "other.db"},
                                    {"action": "delete", "expected_revision": 0, "message_ids": []},
                                    {"action": "delete", "message_ids": ["same"]}, {"action": "restore", "expected_revision": 0},
                                    {"action": "message"}, {"action": "read", "limit": True},
                                    {"action": "read", "start_time": "2026-10-03T01:00:00"},
                                    {"action": "clear", "expected_revision": True}])
async def test_invalid_management_requests_are_explicit(saved, extra: dict[str, Any]) -> None:
    _, _, scopes = saved
    status, error = await _request("/config/conversations/archive/data", _payload(scopes[0], **extra))
    assert status == 400 and error["code"] == "invalid_argument"

@pytest.mark.asyncio
async def test_unauthorized_and_unknown_platform_requests_do_not_mutate(saved) -> None:
    layout, _, scopes = saved
    status, _ = await _request("/config/conversations/archive", authorized=False)
    assert status == 401
    status, _ = await _request("/config/conversations/archive/data", _payload(scopes[0], action="clear", expected_revision=0), authorized=False)
    assert status == 401
    status, _ = await _request("/config/conversations/archive/data", {**_payload(scopes[0]), "platform_id": "../../arbitrary"})
    assert status == 404 and not layout.platform_db("../../arbitrary").exists()
    status, _ = await _request("/config/conversations/archive/data", {**_payload(scopes[0]), "chat_id": "unknown"})
    assert status == 404
    status, data = await _request("/config/conversations/archive/data", _payload(scopes[0]))
    assert status == 200 and data["revision"] == 0 and len(data["items"]) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["db_bytes", "component"])
async def test_corrupt_archive_isolates_to_failed_scope(saved, caplog, kind: str) -> None:
    """损坏数据库或组件字段都只影响该平台, 其它平台档案仍可读且可管理"""
    layout, _, scopes = saved
    if kind == "db_bytes":
        layout.platform_db("alpha").write_bytes(b"corrupt")
        status, catalog = await _request("/config/conversations/archive")
        assert status == 200 and catalog["total"] == 1
        assert catalog["warnings"] == [{"platform_id": "alpha", "error": "archive_unavailable"}]
        status, error = await _request("/config/conversations/archive?platform_id=alpha")
        assert status == 503 and error["code"] == "archive_unavailable"
        assert "目录读取失败" in caplog.text and "管理操作失败" in caplog.text
    else:
        with sqlite3.connect(layout.platform_db("alpha")) as connection:
            connection.execute("UPDATE platform_messages SET components_json=? WHERE scope_key=?", ("{", scopes[0].key))
        status, error = await _request("/config/conversations/archive/data", _payload(scopes[0]))
        assert status == 503 and error["code"] == "archive_unavailable"
    # 数据库整体损坏时其它平台仍可读; 单字段损坏时同库其它会话仍可读
    other = scopes[2] if kind == "db_bytes" else scopes[1]
    status, data = await _request("/config/conversations/archive/data", _payload(other))
    assert status == 200 and data["items"][0]["text"] == "真实原文"

def test_management_read_respects_concurrent_deletion_revision(saved, monkeypatch) -> None:
    layout, _, scopes = saved
    scope = scopes[0]
    store = PlatformMessageStore(layout.platform_db(scope.adapter_id), scope.adapter_id)
    original = store.query

    def raced_query(*args, **kwargs) -> dict[str, Any]:
        store.delete(scope, expected_revision=0)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "query", raced_query)
    with pytest.raises(MessageArchiveError, match="状态发生变化"):
        PlatformMessageDataService(store).operate({key: value for key, value in _payload(scope).items() if key != "platform_id"})

def _platform_message_maintenance_item(store: PlatformMessageStore, scope: MessageScope, message_id: str) -> dict[str, Any]:
    """核验被维护的消息仍保留身份标记"""
    item = store.get(scope, message_id)
    assert item is not None
    return item

def _record(layout: StorageLayout, platform_id: str, *, age: int, retention: int = 30) -> MessageScope:
    """在过去的接收时间生成真实平台数据库档案"""
    layout.ensure_platform(platform_id)
    now = time() - age * 86400
    scope = MessageScope(platform_id, "bot", "group", "group")
    store = PlatformMessageStore(layout.platform_db(platform_id), platform_id, retention_days=retention, clock=lambda: now)
    store.record(scope, ArchiveMessage(str(age), "member", now, "原文"))
    return scope

def test_configured_and_detached_platforms_expire_under_last_policy(tmp_path: Path) -> None:
    layout = StorageLayout(tmp_path)
    configured = _record(layout, "configured", age=3)
    detached = _record(layout, "detached", age=10, retention=7)
    retained = _record(layout, "detached", age=3, retention=7)
    result = StorageMaintenanceService(layout).expire_platform_messages({"configured": 1, "cold": 1})
    assert {item["platform_id"]: item["expired_count"] for item in result["items"]} == {"configured": 1, "detached": 1}
    configured_store = PlatformMessageStore(layout.platform_db("configured"), "configured", retention_days=1)
    detached_store = PlatformMessageStore(layout.platform_db("detached"), "detached", retention_days=7)
    assert _platform_message_maintenance_item(configured_store, configured, "3")["status"] == "expired"
    assert _platform_message_maintenance_item(detached_store, detached, "10")["status"] == "expired"
    assert _platform_message_maintenance_item(detached_store, retained, "3")["text"] == "原文"
    assert configured_store.saved_retention_days() == 1 and detached_store.saved_retention_days() == 7
    assert not layout.platform_db("cold").exists()

def test_corrupt_platform_does_not_stop_other_platform_maintenance(tmp_path: Path, caplog) -> None:
    layout = StorageLayout(tmp_path)
    layout.ensure_platform("broken")
    layout.platform_db("broken").write_bytes(b"not sqlite")
    scope = _record(layout, "good", age=3)
    result = StorageMaintenanceService(layout).expire_platform_messages({"broken": 1, "good": 1})
    assert any(item["platform_id"] == "broken" and item["ok"] is False for item in result["items"])
    assert _platform_message_maintenance_item(PlatformMessageStore(layout.platform_db("good"), "good"), scope, "3")["status"] == "expired"
    assert "自动维护失败" in caplog.text

def test_manifest_cannot_redirect_to_other_platform(tmp_path: Path, caplog) -> None:
    layout = StorageLayout(tmp_path)
    scope = _record(layout, "saved", age=3)
    manifest = layout.platform_root("saved") / "platform.json"
    manifest.write_text(json.dumps({"platform_id": "other"}), encoding="utf-8")
    result = StorageMaintenanceService(layout).expire_platform_messages({})
    assert result["items"] == [{"platform_id": "", "ok": False, "error": "invalid_manifest"}]
    assert "身份与目录不一致" in caplog.text
    assert _platform_message_maintenance_item(PlatformMessageStore(layout.platform_db("saved"), "saved"), scope, "3")["text"] == "原文"

def test_v4_archive_policy_migrates_without_losing_messages(tmp_path: Path) -> None:
    layout = StorageLayout(tmp_path)
    scope = _record(layout, "legacy", age=3)
    database = layout.platform_db("legacy")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE platform_message_policy")
        connection.execute("DROP TABLE friend_actions")
        connection.execute("DROP TABLE friend_policies")
        for table in ("memory_refs", "memory_proposals", "memory_operations", "memory_audit", "memories", "reminder_operations"):
            connection.execute(f"DROP TABLE {table}")
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'group_chat_%'").fetchall():
            connection.execute(f'DROP TABLE "{table}"')
        connection.execute("DROP TRIGGER group_actions_action_revision")
        for column in ("action_revision", "decision", "decided_by_json", "decision_note", "risk", "risk_source"):
            connection.execute(f"ALTER TABLE group_actions DROP COLUMN {column}")
        connection.execute("PRAGMA user_version=4")
    store = PlatformMessageStore(database, "legacy", retention_days=7)
    assert _platform_message_maintenance_item(store, scope, "3")["text"] == "原文"
    assert store.saved_retention_days() is None
    store.purge()
    assert store.saved_retention_days() == 7
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == PLATFORM_SCHEMA_VERSION

@pytest.mark.asyncio
async def test_backend_maintenance_failure_recovers_and_uses_applied_policy(tmp_path: Path, monkeypatch, caplog) -> None:
    backend = cast(Any, BackendManager.__new__(BackendManager))
    backend._storage = StorageLayout(tmp_path)
    backend._platform_apply_lock = asyncio.Lock()
    backend._running = True
    backend.config = SimpleNamespace(platforms=[{"id": "disabled", "enable": False, "settings": {"message_archive_retention_days": 5}},
                                               {"id": "active", "settings": {"message_archive_retention_days": 1}}])
    backend._platform_active_configs = {"active": {"settings": {"message_archive_retention_days": 10}}}
    observed: list[dict[str, int]] = []

    def expire(service, policies) -> dict:
        observed.append(policies)
        if len(observed) == 1:
            raise OSError("临时故障")
        backend._running = False
        return {"items": []}

    sleeper = AsyncMock()
    monkeypatch.setattr(StorageMaintenanceService, "expire_platform_messages", expire)
    module = importlib.import_module("satrap.core.backend.BackendManager")
    monkeypatch.setattr(module.asyncio, "sleep", sleeper)
    await asyncio.wait_for(backend._maintain_message_archives(), timeout=5)
    assert observed == [{"disabled": 5, "active": 10}] * 2
    assert sleeper.call_args_list[0].args == (3600,)
    assert "自动维护轮次失败" in caplog.text

@pytest.mark.asyncio
async def test_backend_stop_cancels_owned_maintenance_task() -> None:
    backend = BackendManager.__new__(BackendManager)
    backend._running = True
    backend._http_server = None
    backend._platform_apply_lock = asyncio.Lock()
    backend._dispatch_task = None
    backend._scheduler = None
    backend._reminder_scheduler = None
    backend._adapter_mgr = None
    backend._platform_runtimes = {}
    backend._telemetry_writer = None
    entered, closed = asyncio.Event(), asyncio.Event()

    async def maintain() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(maintain())
    backend._message_archive_maintenance_task = task
    await entered.wait()
    await backend.stop()
    assert closed.is_set() and task.cancelled()
    assert backend._message_archive_maintenance_task is None and backend._running is False

def _onebot(tmp_path: Path, **settings: Any) -> OneBotAdapter:
    """装配实际归档和伪造发送端, 不连接外部平台"""
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={"self_id": "10000", **settings}))
    adapter.message_archive = PlatformMessageStore(tmp_path / "platform.db", "ob")
    adapter._bot = cast(Any, Mock(send_group_msg=AsyncMock(return_value={"message_id": 1}),
                                send_private_msg=AsyncMock(return_value={"message_id": 2})))
    return adapter

def _platform_message_outgoing_store(adapter: OneBotAdapter | MisskeyAdapter) -> PlatformMessageStore:
    """取得测试明确装配的档案存储"""
    assert adapter.message_archive is not None
    return adapter.message_archive

def _platform_message_outgoing_item(adapter: OneBotAdapter | MisskeyAdapter, scope: MessageScope, message_id: str) -> dict[str, Any]:
    """验证实际采集记录存在后返回其字段"""
    item = _platform_message_outgoing_store(adapter).get(scope, message_id)
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
    item = _platform_message_outgoing_item(adapter, MessageScope("ob", "10000", kind, chat_id), message_id)
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
    result = _platform_message_outgoing_store(adapter).query(MessageScope("ob", "10000", "group", "456"))
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
    assert receipt.status != "success" and not _platform_message_outgoing_store(adapter).database.exists()

@pytest.mark.asyncio
async def test_file_upload_id_is_not_a_message_id(tmp_path: Path, monkeypatch) -> None:
    from satrap.core.utils import paths

    monkeypatch.setattr(paths, "_configured_media_roots", (str(tmp_path),))
    adapter = _onebot(tmp_path)
    adapter._bot.upload_group_file = AsyncMock(return_value={"file_id": "file-1"})
    target = tmp_path / "asset.bin"
    target.write_bytes(b"content")
    receipt = await adapter.send_message("group%456", MessageChain([File(name="asset.bin", file=str(target))]))
    await adapter.drain_message_archive()
    assert receipt.status == "success" and receipt.message_ids == ("file-1",)
    assert not _platform_message_outgoing_store(adapter).database.exists()

@pytest.mark.asyncio
async def test_forward_archives_root_not_fabricated_node_messages(tmp_path: Path) -> None:
    adapter = _onebot(tmp_path)
    adapter._bot.send_group_forward_msg = AsyncMock(return_value={"message_id": 12, "forward_id": "forward-ref"})
    receipt = await adapter._send_forward("group%456", [Node(Plain("其它成员原文"), uin="123", name="成员")], 64)
    await adapter.drain_message_archive()
    item = _platform_message_outgoing_item(adapter, MessageScope("ob", "10000", "group", "456"), "12")
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
    assert len(_platform_message_outgoing_store(adapter).query(MessageScope("ob", "10000", "group", "456"))["items"]) == 1
    assert adapter._event_queue.empty()

@pytest.mark.asyncio
async def test_archive_failure_does_not_change_success_receipt(tmp_path: Path, caplog) -> None:
    adapter = _onebot(tmp_path)
    _platform_message_outgoing_store(adapter).record = Mock(side_effect=OSError("故障"))
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
    assert _platform_message_outgoing_item(adapter, MessageScope("ob", "10000", "group", "456"), "5")["text"] == "原账号"
    assert _platform_message_outgoing_store(adapter).get(MessageScope("ob", "20000", "group", "456"), "5") is None

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
    items = _platform_message_outgoing_store(adapter).query(MessageScope("ob", "10000", "group", "456"))["items"]
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
    assert _platform_message_outgoing_item(adapter, scope, "1")["text"] == "确认后取消"

def _misskey(tmp_path: Path) -> MisskeyAdapter:
    """装配 Misskey 原生回包和实际档案存储"""
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
    item = _platform_message_outgoing_item(adapter, MessageScope("mk", "bot", kind, chat_id), message_id)
    assert result.status == "success" and result.message_ids == (message_id,) and item["text"].endswith("abcd...")
    assert item["media"] == [{"type": "File", "native_id": "file-1"},
                              *([{"type": "File", "native_id": "file-2"}] if kind == "discussion" else [])]
    assert item["time_source"] == "local" and item["direction"] == "outbound"

@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {"id": True}, {"id": ""}, {"id": "x", "toRoomId": "wrong"},
                                    {"id": "x", "fromUserId": "wrong"}])
async def test_misskey_unconfirmed_or_mismatched_receipt_not_archived(tmp_path: Path, result: Any) -> None:
    adapter = _misskey(tmp_path)
    cast(Any, adapter._client).send_room_message = AsyncMock(return_value=result)
    receipt = await adapter.send_message("room%r", MessageChain.from_text("消息"))
    assert receipt.status == "unknown" and not receipt.message_ids
    await adapter.drain_message_archive()
    assert not _platform_message_outgoing_store(adapter).database.exists()

@pytest.mark.asyncio
async def test_misskey_frozen_account_and_native_timestamp(tmp_path: Path) -> None:
    adapter = _misskey(tmp_path)

    async def send(payload: Any) -> dict[str, Any]:
        adapter.bot_self_id = "replacement"
        return {"id": "confirmed", "fromUserId": "bot", "createdAt": "2026-10-03T12:00:00+08:00"}

    cast(Any, adapter._client).send_room_message = AsyncMock(side_effect=send)
    receipt = await adapter.send_message("room%r", MessageChain.from_text("原账号"))
    assert receipt.status == "success" and receipt.message_ids == ("confirmed",)
    await adapter.drain_message_archive()
    item = _platform_message_outgoing_item(adapter, MessageScope("mk", "bot", "group", "r"), "confirmed")
    assert item["sender_id"] == "bot" and item["time_source"] == "platform"
    assert _platform_message_outgoing_store(adapter).get(MessageScope("mk", "replacement", "group", "r"), "confirmed") is None
