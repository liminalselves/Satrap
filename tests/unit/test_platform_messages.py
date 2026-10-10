"""平台原始消息档案的身份隔离, 查询覆盖范围和删除恢复反例"""
from __future__ import annotations

from pathlib import Path
import json
import sqlite3

import pytest

from satrap.core.config.platform_messages import (
    ArchiveMessage, MessageArchiveError, MessageScope, PlatformMessageStore, archive_time,
)
from satrap.core.config.platform_schema import PLATFORM_SCHEMA_VERSION, ensure_platform_tables


NOW = 1_800_000_000.0
SCOPE = MessageScope("adapter", "bot", "group", "group-a")


def _store(tmp_path: Path, clock=lambda: NOW) -> PlatformMessageStore:
    """使用固定时钟创建独立的平台档案"""
    return PlatformMessageStore(tmp_path / "platform.db", "adapter", clock=clock)


def _message(message_id: str = "m1", *, sender: str = "u1", when: float = NOW - 10,
             text: str = "原始群消息", direction: str = "inbound") -> ArchiveMessage:
    """构造已核验的真实消息快照"""
    return ArchiveMessage(message_id, sender, when, text, nickname="用户", card="群名片", direction=direction,
                          reply_to_message_id="quote-id", mentions=["u2", "u3"], components=[{"type": "text"}])


def test_cold_reads_and_maintenance_do_not_create_database(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get(SCOPE, "absent") is None
    result = store.query(SCOPE)
    assert result["items"] == [] and result["coverage"]["complete"] is False
    assert store.backfill_allowed(SCOPE, "absent")
    assert store.purge() == {"expired_count": 0, "backup_count": 0}
    assert list(tmp_path.iterdir()) == []


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
        connection.execute("PRAGMA user_version=3")
    store = _store(tmp_path)
    assert store.query(SCOPE)["items"] == []
    assert store.record(SCOPE, _message())
    store.delete(SCOPE, expected_revision=0)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == PLATFORM_SCHEMA_VERSION
        assert connection.execute("SELECT content FROM chat_history").fetchone()[0] == "模型上下文"
        assert connection.execute("SELECT revision FROM session_config_overrides").fetchone()[0] == 2


def test_declared_missing_archive_table_is_damage_not_empty_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(SCOPE, _message())
    with sqlite3.connect(store.database) as connection:
        connection.execute("DROP TABLE platform_message_backups")
    with pytest.raises(RuntimeError, match="platform_message_backups"):
        store.query(SCOPE)
    with pytest.raises(RuntimeError, match="platform_message_backups"):
        store.record(SCOPE, _message("m2"))


def test_message_ids_are_isolated_by_account_kind_and_chat(tmp_path: Path) -> None:
    store = _store(tmp_path)
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
    store = _store(tmp_path)
    assert store.record(SCOPE, _message(sender="bot"))
    restarted = _store(tmp_path)
    assert not restarted.record(SCOPE, _message(text="重复投递不能改原文", sender="bot", direction="outbound"))
    message = restarted.get(SCOPE, "m1")
    assert message["direction"] == "outbound" and message["text"] == "原始群消息"
    assert len(restarted.query(SCOPE)["items"]) == 1


def test_conflicting_echo_cannot_change_verified_sender_or_direction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(SCOPE, _message())
    with pytest.raises(MessageArchiveError) as failure:
        store.record(SCOPE, _message(sender="bot", direction="outbound"))
    assert failure.value.code == "unverified_target"
    result = store.get(SCOPE, "m1")
    assert result["sender_id"] == "u1" and result["direction"] == "inbound"


def test_search_intersects_conditions_and_treats_keywords_as_plain_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
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
    store = _store(tmp_path)
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
        _store(tmp_path).query(SCOPE, **options)


def test_text_budget_preserves_message_sender_and_quote_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
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
    store = _store(tmp_path)
    store.recall(SCOPE, "m1")
    assert not store.record(SCOPE, _message())
    assert store.get(SCOPE, "m1")["status"] == "recalled"
    assert store.get(SCOPE, "m1")["text"] == ""
    assert store.query(SCOPE)["items"] == []
    assert not store.backfill_allowed(SCOPE, "m1", NOW - 10)


def test_delete_clears_searchable_data_blocks_backfill_and_restores_only_same_scope(tmp_path: Path) -> None:
    store = _store(tmp_path)
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
    store = _store(tmp_path)
    deleted = store.delete(SCOPE, message_ids=["unknown"], expected_revision=0)
    assert not store.backfill_allowed(SCOPE, "unknown")
    store.restore(SCOPE, deleted["backup_id"], expected_revision=1)
    assert store.backfill_allowed(SCOPE, "unknown")
    assert store.record(SCOPE, _message("unknown"))


def test_clear_restore_preserves_new_messages_and_previous_delete_state(tmp_path: Path) -> None:
    now = [NOW]
    store = _store(tmp_path, lambda: now[0])
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
    store = _store(tmp_path)
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
    store = _store(tmp_path)
    store.record(SCOPE, _message("a"))
    store.record(SCOPE, _message("b"))
    store.delete(SCOPE, message_ids=["a"], expected_revision=0)
    with pytest.raises(MessageArchiveError) as failure:
        store.delete(SCOPE, message_ids=["b"], expected_revision=0)
    assert failure.value.code == "revision_conflict"
    assert store.get(SCOPE, "b")["status"] == "active"


def test_retention_hides_old_body_before_cleanup_and_purge_erases_backup(tmp_path: Path) -> None:
    now = [NOW]
    store = _store(tmp_path, lambda: now[0])
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
    store = _store(tmp_path)
    assert not store.backfill_allowed(SCOPE, "old", NOW - 31 * 86400)
    assert store.record(SCOPE, _message("old", when=NOW - 31 * 86400))
    assert store.get(SCOPE, "old")["status"] == "expired"
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("SELECT text FROM platform_messages WHERE message_id='old'").fetchone()[0] == ""


def test_snapshot_boundaries_keep_delimiter_and_unicode_ids_distinct(tmp_path: Path) -> None:
    first = MessageScope("adapter", "bot", "group", "群:1\"\\")
    second = MessageScope("adapter", "bot", "group", "群:1")
    store = _store(tmp_path)
    store.record(first, _message("消息\"1", text="中文🙂"))
    assert first.key != second.key and store.get(second, "消息\"1") is None
    assert store.get(first, "消息\"1")["text"] == "中文🙂"
    with sqlite3.connect(store.database) as connection:
        key = connection.execute("SELECT scope_key FROM platform_message_chats").fetchone()[0]
        assert json.loads(key.split(":", 2)[2])[-1] == first.chat_id
