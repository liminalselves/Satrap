from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.group_chat.summaries import SummaryLimits, SummaryStore, _cursor
from satrap.core.group_chat.types import GroupChatError


NOW = 1_800_000_000.0
SCOPE = MessageScope("test-platform", "bot:id", "group", "room:/alpha")
OTHER = MessageScope("test-platform", "bot:id", "group", "room:/beta")


def setup(tmp_path: Path):
    clock = [NOW]
    archive = PlatformMessageStore(tmp_path / "platform.db", SCOPE.adapter_id, clock=lambda: clock[0])
    for index in range(5):
        archive.record(SCOPE, ArchiveMessage(f"m{index}", "member:a", NOW - 10 + index, f"讨论 {index}"))
    archive.record(OTHER, ArchiveMessage("other", "member:a", NOW - 5, "不能混入"))
    archive.record(SCOPE, ArchiveMessage("bot", SCOPE.self_id, NOW - 4, "旧摘要", direction="outbound"))
    return archive, SummaryStore(archive), clock


def prepare(store: SummaryStore, *, limits=None, owner="request:a"):
    return store.prepare(SCOPE, owner, start_time=datetime.fromtimestamp(NOW - 20, timezone.utc).isoformat(),
                         end_time=datetime.fromtimestamp(NOW, timezone.utc).isoformat(), limits=limits)


def save(store: SummaryStore, snapshot: dict, *, owner="request:a"):
    return store.save(SCOPE, owner, snapshot["snapshot_id"], "讨论摘要",
                      [{"text": "大家讨论了项目", "source_message_ids": ["m0", "m4"]}])


def test_frozen_pages_source_validation_and_idempotent_save(tmp_path):
    archive, store, _ = setup(tmp_path)
    snapshot = prepare(store, limits=SummaryLimits(page_limit=2))
    assert [item["message_id"] for item in snapshot["items"]] == ["m0", "m1"]
    assert snapshot["selection"]["all_local_matches_selected"]
    assert not snapshot["archive_coverage"]["platform_history_complete"]
    archive.record(SCOPE, ArchiveMessage("new", "member:a", NOW - 1, "快照后新增"))
    with pytest.raises(GroupChatError, match="全部分页"):
        save(store, snapshot)
    with pytest.raises(GroupChatError, match="跳过"):
        store.read_sources(SCOPE, "request:a", snapshot["snapshot_id"], _cursor([1, snapshot["snapshot_id"], 4]))
    page = store.read_sources(SCOPE, "request:a", snapshot["snapshot_id"], snapshot["next_cursor"])
    last = store.read_sources(SCOPE, "request:a", snapshot["snapshot_id"], page["next_cursor"])
    assert [item["message_id"] for item in last["items"]] == ["m4"]
    assert last["next_cursor"] is None
    result = save(store, snapshot)
    replay = save(store, snapshot)
    assert replay["replayed"] and replay["summary"]["summary_id"] == result["summary"]["summary_id"]
    assert store.list(SCOPE)["items"] == [result["summary"]]


@pytest.mark.parametrize("operation", ["delete", "recall", "expire"])
def test_removed_source_erases_derived_content_in_same_maintenance_transaction(tmp_path, operation):
    archive, store, clock = setup(tmp_path)
    snapshot = prepare(store)
    saved = save(store, snapshot)["summary"]
    if operation == "delete":
        backup = archive.delete(SCOPE, message_ids=["m0"], expected_revision=0)
    elif operation == "recall":
        archive.recall(SCOPE, "m0")
    else:
        clock[0] += 31 * 86400
        archive.purge()
    with sqlite3.connect(archive.database) as connection:
        row = connection.execute("SELECT state, title, points_json FROM group_chat_summaries").fetchone()
        assert row == ("source_unavailable", "", "[]")
        assert connection.execute("SELECT COUNT(*) FROM group_chat_summary_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM group_chat_summary_refs").fetchone()[0] == 0
    if operation == "delete":
        archive.restore(SCOPE, backup["backup_id"], expected_revision=1)
    detail = store.get(SCOPE, saved["summary_id"])["summary"]
    assert detail["state"] == "source_unavailable" and detail["revision"] == 2
    assert detail["points"] == []


def test_scope_owner_and_arbitrary_citations_are_rejected(tmp_path):
    _, store, _ = setup(tmp_path)
    snapshot = prepare(store)
    for scope, owner in ((OTHER, "request:a"), (SCOPE, "request:b")):
        with pytest.raises(GroupChatError):
            store.read_sources(scope, owner, snapshot["snapshot_id"], _cursor([1, snapshot["snapshot_id"], 0]))
    with pytest.raises(GroupChatError, match="之外"):
        store.save(SCOPE, "request:a", snapshot["snapshot_id"], "摘要", [{"text": "伪造", "source_message_ids": ["other"]}])
    saved = save(store, snapshot)["summary"]
    with pytest.raises(GroupChatError):
        store.get(OTHER, saved["summary_id"])
    with pytest.raises(GroupChatError):
        store.delete(SCOPE, saved["summary_id"], expected_revision=2)
    assert store.get(SCOPE, saved["summary_id"])["summary"]["state"] == "active"


def test_budget_and_empty_results_never_claim_complete_platform_history(tmp_path):
    _, store, _ = setup(tmp_path)
    snapshot = prepare(store, limits=SummaryLimits(message_limit=2))
    assert snapshot["selection"] == {"selected_count": 2, "all_local_matches_selected": False,
                                      "truncated": True, "reasons": ["message_limit"]}
    result = store.prepare(SCOPE, "request:a", start_time=datetime.fromtimestamp(NOW - 20, timezone.utc).isoformat(),
                           end_time=datetime.fromtimestamp(NOW, timezone.utc).isoformat(), keyword="不存在的文字")
    assert result["items"] == [] and result["selection"]["selected_count"] == 0
    with pytest.raises(GroupChatError):
        store.save(SCOPE, "request:a", result["snapshot_id"], "无人讨论", [{"text": "无", "source_message_ids": ["m0"]}])


def test_cold_list_prepare_and_purge_do_not_create_database(tmp_path):
    archive = PlatformMessageStore(tmp_path / "platform.db", SCOPE.adapter_id)
    store = SummaryStore(archive)
    assert store.list(SCOPE)["items"] == []
    assert prepare(store)["snapshot_id"] is None
    assert archive.purge() == {"expired_count": 0, "backup_count": 0}
    assert not archive.database.exists()


def test_snapshot_expiry_quota_and_source_restore_do_not_revive_snapshot(tmp_path):
    _, store, clock = setup(tmp_path)
    first = prepare(store)
    for index in range(4):
        prepare(store, owner=f"other:{index}")
    with pytest.raises(GroupChatError, match="过多"):
        prepare(store, owner="sixth")
    clock[0] += 901
    with pytest.raises(GroupChatError):
        save(store, first)
    assert prepare(store, owner="sixth")["snapshot_id"] != first["snapshot_id"]


def test_large_source_is_explicitly_partial_and_not_silently_complete(tmp_path):
    archive, store, _ = setup(tmp_path)
    archive.record(SCOPE, ArchiveMessage("long", "member:a", NOW - 15, "字" * 2000))
    snapshot = prepare(store, limits=SummaryLimits(text_budget=1000, page_budget=500))
    assert snapshot["items"][0]["truncated"]
    assert "page_budget" in snapshot["selection"]["reasons"]
    assert "text_budget" in snapshot["selection"]["reasons"]
    assert not snapshot["selection"]["all_local_matches_selected"]


def test_list_cursor_bound_to_scope_and_keyword_and_revision_delete(tmp_path):
    _, store, _ = setup(tmp_path)
    snapshot = prepare(store)
    a = save(store, snapshot)["summary"]
    b = store.save(SCOPE, "request:a", snapshot["snapshot_id"], "另一个摘要",
                   [{"text": "其它概括", "source_message_ids": ["m1"]}])["summary"]
    page = store.list(SCOPE, limit=1)
    assert page["has_more"]
    with pytest.raises(ValueError):
        store.list(OTHER, cursor=page["next_cursor"])
    rest = store.list(SCOPE, limit=1, cursor=page["next_cursor"])
    assert {item["summary_id"] for item in page["items"] + rest["items"]} == {a["summary_id"], b["summary_id"]}
    assert store.delete(SCOPE, a["summary_id"], a["revision"])["status"] == "deleted"
    assert len(store.list(SCOPE)["items"]) == 1
