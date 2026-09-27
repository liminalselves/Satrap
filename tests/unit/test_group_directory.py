"""群目录完整性, 退群判定和账号代次反例"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
import json
import sqlite3

import pytest

from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.conversation import ConversationRoute
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform import PlatformConfig


def _store(tmp_path: Path) -> GroupDirectoryStore:
    store = GroupDirectoryStore(tmp_path / "platform.db")
    store.adopt_legacy("100", {"group_management_version": 1})
    return store


@pytest.mark.asyncio
async def test_protocol_directory_does_not_truncate_at_old_tool_limit() -> None:
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={}))
    adapter._bot = AsyncMock()
    adapter._bot.get_group_list.return_value = [
        {"group_id": index, "group_name": f"群{index}", "member_count": 1, "max_member_count": 500}
        for index in range(1, 515)
    ]
    result = await adapter.admin.fetch_group_directory()
    assert result["complete"] is True
    assert len(result["items"]) == 514
    assert result["items"][-1]["group_id"] == "514"


@pytest.mark.asyncio
async def test_protocol_invalid_entry_keeps_valid_entries_but_marks_incomplete() -> None:
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={}))
    adapter._bot = AsyncMock()
    adapter._bot.get_group_list.return_value = [{"group_id": 123, "group_name": "有效"}, {"group_id": "bad"}]
    result = await adapter.admin.fetch_group_directory()
    assert result["complete"] is False
    assert result["truncated"] is True
    assert result["reason"] == "invalid_entries"
    assert [item["group_id"] for item in result["items"]] == ["123"]


def test_complete_empty_confirms_left_but_partial_keeps_previous(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.begin_sync("100", 1, "first")
    assert store.finish_sync("100", 1, "first", [
        {"group_id": "123", "group_name": "一"}, {"group_id": "456", "group_name": "二"},
    ], complete=True, truncated=False)
    store.begin_sync("100", 1, "partial")
    assert store.finish_sync("100", 1, "partial", [
        {"group_id": "123", "group_name": "新名字"},
    ], complete=False, truncated=True, reason="invalid_entries")
    assert store.list_groups("100", membership="joined")["total"] == 2
    assert store.sync_status("100")["last_complete_at"] is not None
    store.begin_sync("100", 1, "empty")
    assert store.finish_sync("100", 1, "empty", [], complete=True, truncated=False)
    assert store.list_groups("100", membership="joined")["total"] == 0
    assert store.list_groups("100", membership="left")["total"] == 2


def test_late_sync_is_discarded_and_failure_preserves_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.begin_sync("100", 1, "old")
    store.begin_sync("100", 2, "new")
    assert not store.finish_sync("100", 1, "old", [{"group_id": "123"}], complete=True, truncated=False)
    assert store.finish_sync("100", 2, "new", [{"group_id": "456"}], complete=True, truncated=False)
    store.begin_sync("100", 2, "failed")
    assert store.fail_sync("100", 2, "failed", "timeout")
    assert store.list_groups("100", membership="joined")["items"][0]["group_id"] == "456"
    assert store.sync_status("100")["reason"] == "timeout"


def test_trusted_event_after_sync_start_is_not_overwritten(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.begin_sync("100", 1, "first")
    store.confirm_membership("100", "123", True)
    assert store.finish_sync("100", 1, "first", [], complete=True, truncated=False)
    assert store.list_groups("100", membership="joined")["total"] == 1


def test_pagination_and_counts_use_full_identity_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.begin_sync("100", 1, "first")
    store.finish_sync("100", 1, "first", [
        {"group_id": str(index), "group_name": f"群{index}"} for index in range(1, 55)
    ], complete=True, truncated=False)
    store.patch_group("100", "1", "policy", {"enabled": {"mode": "value", "value": True}}, expected_revision=0)
    second = store.list_groups("100", page=2, page_size=25)
    assert second["total"] == 54
    assert len(second["items"]) == 25
    assert second["counts"]["joined"] == 54
    assert second["counts"]["response_enabled"] == 1
    assert store.list_groups("100", query="群5", membership="all")["total"] == 6


def test_approval_inheritance_counts_only_joined_groups(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.begin_sync("100", 1, "first")
    store.finish_sync("100", 1, "first", [
        {"group_id": "123"}, {"group_id": "456"}, {"group_id": "789"},
    ], complete=True, truncated=False)
    store.patch_group("100", "123", "approval", {
        "kick_group_member": {"mode": "value", "value": "auto_execute"},
    }, expected_revision=0)
    store.patch_group("100", "456", "approval", {
        "kick_group_member": {"mode": "inherit"},
    }, expected_revision=0)
    store.confirm_membership("100", "789", False)
    counts = store.approval_inheritance_counts("100")
    assert counts["kick_group_member"] == 1
    assert counts["set_group_name"] == 2


def test_scoped_session_summary_keeps_group_and_account_boundaries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.scoped_session_summary("bot", "100", "123")["known_scoped_count"] == 0
    route = ConversationRoute("42", "bot", "simple", "edictum", "group_member", "100", "123", 1)
    other = ConversationRoute("42", "bot", "simple", "edictum", "group_member", "200", "123", 1)
    with sqlite3.connect(tmp_path / "platform.db") as connection:
        connection.execute("CREATE TABLE context_sessions (context_key TEXT, platform TEXT, session_id TEXT)")
        connection.execute("CREATE TABLE session_configs (session_id TEXT, session_config TEXT)")
        connection.executemany("INSERT INTO context_sessions VALUES (?, ?, ?)", [
            (route.key, "bot", "session-a"), (other.key, "bot", "session-b"),
            ("legacy:user:42", "bot", "session-c"),
        ])
        connection.executemany("INSERT INTO session_configs VALUES (?, ?)", [
            ("session-a", json.dumps({"model_name": "other", "system_prompt": "", "plugins": []})),
            ("session-b", json.dumps({"model_name": "hidden"})),
        ])
    summary = store.scoped_session_summary("bot", "100", "123", 1)
    assert summary["known_scoped_count"] == 1
    assert summary["current_route_count"] == 1
    assert summary["session_ids"] == ["session-a"]
    assert summary["override_counts"] == {"model": 1, "prompt": 1, "plugins": 1}
    assert store.scoped_session_summary("bot", "100", "123", 2)["current_route_count"] == 0
