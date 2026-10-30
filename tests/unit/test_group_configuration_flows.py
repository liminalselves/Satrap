"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
import sqlite3
from threading import Event
import pytest
from satrap.core.config.document import upsert_platform
from satrap.core.config.group_store import GroupConfigConflict, GroupConfigStore, GroupLegacyConflict
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform import PlatformConfig
from satrap.core.conversation import ConversationRoute
from unittest.mock import AsyncMock
import json
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.config.group_events import GroupEventBuffer, event_values
from satrap.core.config.group_actions import GroupActionStore
from satrap.core.pipeline.request_diagnostics import RequestDiagnostic, RequestDiagnosticLog
from satrap.core.platform import PlatformEvent
from satrap.core.platform.notices import NoticePayload
import asyncio
import importlib
import threading
from types import SimpleNamespace
from typing import Any, cast
from satrap.core.config.group_session import resolve_group_session, session_values
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.providers import EdictumProvider, SessionProviderRegistry
from satrap.core.type import LLMConfig, SessionConfig, UserCall
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.simple_session import AsyncSimpleSession
from typing import Mapping
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.call_context import CallOrigin
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.config.document import save_config_document
from satrap.core.platform import PlatformAdapterManager

def test_adopt_legacy_modes_and_isolation(tmp_path: Path) -> None:
    """adopt_legacy 按旧白名单解析模式并迁移群策略; 群与账号维度相互隔离"""
    # 新版空平台: 默认 selected 且无群记录
    platform = upsert_platform([], {"id": "bot", "type": "onebot", "settings": {}})[0]
    assert platform["settings"]["group_management_version"] == 1
    store = GroupConfigStore(tmp_path / "new.db")
    assert store.adopt_legacy("100", platform["settings"])["mode"] == "selected"
    assert store.read_group("100", "123")["revision"] == 0

    # 旧空白名单: 全开并迁移群策略, revision 前进; 旧来源冲突被拒
    store = GroupConfigStore(tmp_path / "all.db")
    old = {"group_whitelist": [], "wake_group_overrides": {"123": {"wake_cooldown": 3}}}
    assert store.adopt_legacy("100", old)["mode"] == "all"
    assert store.read_group("100", "123")["explicit"]["policy"] == {
        "wake_cooldown": {"mode": "value", "value": 3},
    }
    assert store.adopt_legacy("100", old)["revision"] == 1
    with pytest.raises(GroupLegacyConflict):
        store.check_legacy_source("100", {"group_whitelist": ["123"]})

    # 旧白名单与账号覆盖隔离: 账号维度独立
    store = GroupConfigStore(tmp_path / "iso.db")
    whitelisted = {"group_whitelist": ["123"], "wake_group_overrides": {"456": {"wake_mode": "frequency"}}}
    assert store.adopt_legacy("100", whitelisted)["mode"] == "selected"
    assert store.read_group("100", "123")["explicit"]["policy"]["enabled"] == {
        "mode": "value", "value": True,
    }
    assert "enabled" not in store.read_group("100", "456")["explicit"]["policy"]
    assert store.read_account("200") is None
    assert store.read_group("200", "123")["revision"] == 0
    assert store.adopt_legacy("200", whitelisted)["mode"] == "selected"
    assert store.read_group("200", "123")["revision"] == 0

def test_area_patch_keeps_other_sections_and_changes_route_only_for_binding(tmp_path: Path) -> None:
    store = GroupConfigStore(tmp_path / "platform.db")
    store.adopt_legacy("100", {"group_management_version": 1})
    first = store.patch_group("100", "123", "policy", {"enabled": {"mode": "value", "value": True}}, expected_revision=0)
    assert first["route_generation"] == 0
    binding = {"mode": "value", "value": {"provider": "edictum", "config_name": "assistant"}}
    second = store.patch_group("100", "123", "session", {"binding": binding}, expected_revision=1)
    assert second["route_generation"] == 1
    assert second["explicit"]["policy"] == first["explicit"]["policy"]
    third = store.patch_group("100", "123", "session", {"binding": binding, "prompt": {"mode": "value", "value": ""}}, expected_revision=2)
    assert third["route_generation"] == 1
    with pytest.raises(GroupConfigConflict):
        store.patch_group("100", "123", "events", {}, expected_revision=2)

def test_schema_migration_rejects_declared_missing_table(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 1")
    with pytest.raises(RuntimeError, match="session_config_overrides"):
        GroupConfigStore(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='group_accounts'").fetchone() is None

def test_policy_values_reject_unknown_and_ambiguous_values(tmp_path: Path) -> None:
    store = GroupConfigStore(tmp_path / "platform.db")
    store.adopt_legacy("100", {"group_management_version": 1})
    with pytest.raises(ValueError, match="不支持字段"):
        store.patch_group("100", "123", "policy", {"enable_group": {"mode": "value", "value": True}}, expected_revision=0)
    with pytest.raises(ValueError, match="布尔值"):
        store.patch_group("100", "123", "policy", {"enabled": {"mode": "value", "value": "true"}}, expected_revision=0)
    with pytest.raises(ValueError, match="wake_cooldown"):
        store.patch_group("100", "123", "policy", {"wake_cooldown": {"mode": "value", "value": -1}}, expected_revision=0)
    assert store.read_group("100", "123")["revision"] == 0

def test_route_generation_preserves_old_key_and_separates_new_history() -> None:
    old = ConversationRoute(user_id="200", platform="bot", session_type="assistant",
                            scope="group_member", self_id="100", group_id="123")
    new = ConversationRoute(user_id="200", platform="bot", session_type="assistant",
                            scope="group_member", self_id="100", group_id="123", generation=1)
    assert old.key != new.key
    assert old.context_value != new.context_value
    legacy = ConversationRoute(user_id="200", platform="bot", session_type="assistant")
    changed = ConversationRoute(user_id="200", platform="bot", session_type="assistant",
                                self_id="100", group_id="123", generation=1)
    assert legacy.key is None and changed.key is not None

@pytest.mark.asyncio
async def test_access_requires_explicit_enable_and_legacy_conflict_fails_closed(tmp_path: Path) -> None:
    """群准入需显式启用并绑定账号; 旧白名单来源冲突时 fail-closed 全拒"""
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"group_management_version": 1}))
    store = GroupConfigStore(tmp_path / "platform.db")
    adapter.set_group_access_store(store)
    await adapter._handle_meta({"self_id": "100", "meta_event_type": "lifecycle", "sub_type": "connect"})
    assert not adapter.allows_group("123")
    store.patch_group("100", "123", "policy", {"enabled": {"mode": "value", "value": True}}, expected_revision=0)
    await adapter.refresh_group_access("100")
    assert adapter.allows_group("123")
    assert not adapter.allows_group("456")
    await adapter._handle_meta({"self_id": "200", "meta_event_type": "lifecycle", "sub_type": "connect"})
    assert adapter.bot_self_id == "100"
    assert store.read_account("200") is None
    assert adapter.allows_group("123")

    # 旧格式白名单: 迁移前按旧源放行, 来源冲突时全部拒绝
    legacy = OneBotAdapter(PlatformConfig(id="bot2", type="onebot", settings={"group_whitelist": ["123"]}))
    legacy_store = GroupConfigStore(tmp_path / "legacy.db")
    legacy.set_group_access_store(legacy_store)
    await legacy._handle_meta({"self_id": "100", "meta_event_type": "lifecycle", "sub_type": "connect"})
    assert legacy.allows_group("123")
    assert not legacy.allows_group("456")
    legacy.config.settings["group_whitelist"] = ["456"]
    with pytest.raises(GroupLegacyConflict):
        await legacy._handle_meta({"self_id": "100", "meta_event_type": "lifecycle", "sub_type": "connect"})
    assert not legacy.allows_group("123")
    assert not legacy.allows_group("456")

@pytest.mark.asyncio
async def test_refresh_and_pause_keep_other_groups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """刷新未完成或失败时保留旧快照; 暂停目标群不影响其它群, 恢复后重新放行"""
    import asyncio

    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"group_management_version": 1}))
    store = GroupConfigStore(tmp_path / "platform.db")
    adapter.set_group_access_store(store)
    await adapter._handle_meta({"self_id": "100", "meta_event_type": "lifecycle", "sub_type": "connect"})
    for group_id in ("123", "789"):
        store.patch_group("100", group_id, "policy", {"enabled": {"mode": "value", "value": True}}, expected_revision=0)
    await adapter.refresh_group_access("100")
    original = store.runtime_snapshot
    entered = Event()
    release = Event()

    def delayed(self_id: str):
        result = original(self_id)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(store, "runtime_snapshot", delayed)
    task = asyncio.create_task(adapter.refresh_group_access("100"))
    assert await asyncio.to_thread(entered.wait, 5)
    assert adapter.allows_group("789")
    assert adapter.group_route("789")[1] == 0
    release.set()
    await task

    def failed(self_id: str):
        raise RuntimeError("read failed")

    monkeypatch.setattr(store, "runtime_snapshot", failed)
    with pytest.raises(RuntimeError, match="read failed"):
        await adapter.refresh_group_access("100")
    assert adapter.allows_group("789")
    assert adapter.group_route("789")[1] == 0

    # 暂停目标群只影响该群
    adapter.pause_group_access("123")
    assert not adapter.allows_group("123")
    assert adapter.group_route("123")[1] == -1
    assert adapter.allows_group("789")
    assert adapter.group_route("789")[1] == 0
    adapter.resume_group_access("123")
    assert adapter.allows_group("123")

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

def test_pagination_and_counts_use_full_identity_snapshot(tmp_path: Path, monkeypatch) -> None:
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
    store.patch_group("100", "1", "session", {"binding": {"mode": "value", "value": {"provider": "edictum", "config_name": "group-agent"}}}, expected_revision=1)
    GroupActionStore(store.database).submit("page-action-123", "100", "1", "kick_group_member", {"user_id": "42"}, "panel", 1, approval_required=True)
    from satrap.core.config.platform_messages import MessageScope

    scope = MessageScope("bot", "100", "group", "1")
    with sqlite3.connect(store.database) as connection:
        connection.execute("INSERT INTO platform_message_chats(scope_key,adapter_id,self_id,conversation_kind,chat_id,captured_from,captured_to) VALUES(?,?,?,?,?,1,9)",
                           (scope.key, "bot", "100", "group", "1"))
    queries = []
    connect = store._connect

    def traced():
        connection = connect()
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(store, "_connect", traced)
    listed = store.list_groups("100", platform_snapshot={"id": "bot", "session_provider": "edictum", "session_type": "platform-agent"})
    group = next(item for item in listed["items"] if item["group_id"] == "1")
    assert group["binding"] == {"agent": "group-agent", "provider": "edictum", "source": "group"}
    assert group["pending_actions"] == 1 and group["last_activity_at"] == 9
    assert not group["activity_coverage"]["complete"]
    assert all(item["binding"]["agent"] == "platform-agent" for item in listed["items"] if item["group_id"] != "1")
    assert len([query for query in queries if query.lstrip().upper().startswith("SELECT")]) <= 7

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

def test_event_buffer_never_exposes_flag_or_other_group() -> None:
    buffer = GroupEventBuffer()
    for adapter, account, group, flag in (
        ("ob", "100", "456", "secret-a"),
        ("ob", "101", "456", "secret-b"),
        ("other", "100", "456", "secret-c"),
        ("ob", "100", "789", "secret-d"),
    ):
        payload = NoticePayload(
            category="request", kind="group", self_id=account, group_id=group,
            user_id="42", flag=flag, comment="private message", time=100,
        )
        buffer.append(PlatformEvent(adapter, "onebot", "request.group", group_id=group,
                                    extras={"payload": payload, "raw_event": {"flag": flag}}))
    result = buffer.list("ob", "100", "456", visibility={}, limit=50)
    assert len(result["items"]) == 1
    encoded = json.dumps(result, ensure_ascii=False)
    assert "secret" not in encoded and "private message" not in encoded
    assert buffer.list("ob", "100", "456", visibility={"group_request": False}, limit=50)["items"] == []

def test_event_settings_reject_invalid_values() -> None:
    assert event_values({"group_ban": {"mode": "value", "value": False}}) == {"group_ban": False}
    for value in ({"group_ban": {"mode": "value", "value": 0}},
                  {"friend_request": {"mode": "value", "value": False}}):
        try:
            event_values(value)
        except ValueError:
            pass
        else:
            raise AssertionError("无效群事件设置应被拒绝")

def test_group_diagnostics_filter_before_limit() -> None:
    log = RequestDiagnosticLog()
    for index, (account, group) in enumerate((("100", "456"), ("100", "789"), ("101", "456"))):
        log.record(RequestDiagnostic(
            adapter_id="ob", session_id=f"group%{group}", actor_id="42",
            stage="model", decision="accepted", reason="", recorded_at=100 + index,
            request_id=f"req-{index}", self_id=account,
        ))
    records = log.list_requests("ob", self_id="100", group_id="456", limit=1)
    assert [item["request_id"] for item in records] == ["req-0"]

@pytest.mark.asyncio
async def test_event_subscription_independent_from_group_response(tmp_path: Path) -> None:
    adapter = OneBotAdapter(PlatformConfig(
        id="ob", type="onebot", settings={"group_management_version": 1},
    ))
    store = GroupConfigStore(tmp_path / "platform.db")
    adapter.set_group_access_store(store)
    adapter.bot_self_id = "100"
    await adapter._ensure_group_access("100")
    assert not adapter.allows_group("456")
    adapter.emit_event = AsyncMock()
    notice = {"self_id": 100, "post_type": "notice", "notice_type": "group_ban",
              "group_id": 456, "user_id": 42, "operator_id": 43, "sub_type": "ban", "duration": 60}
    await adapter._emit_notice(notice)
    assert adapter.emit_event.await_count == 1
    store.patch_group("100", "456", "events", {
        "group_ban": {"mode": "value", "value": False},
    }, expected_revision=0)
    await adapter.refresh_group_access("100")
    await adapter._emit_notice(notice)
    assert adapter.emit_event.await_count == 1

def test_self_leave_expires_pending_approval(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    directory = GroupDirectoryStore(database)
    directory.adopt_legacy("100", {"group_management_version": 1})
    actions = GroupActionStore(database)
    record, _ = actions.submit(
        "action-12345", "100", "456", "kick_group_member", {"user_id": "42"},
        "panel", 1, approval_required=True,
    )
    assert record["state"] == "pending"
    directory.confirm_membership("100", "456", False)
    expired = actions.get("100", "456", "action-12345")
    assert expired is not None and expired["state"] == "expired"
    assert expired["result"] == {"reason": "membership_left"}

def test_account_switch_expires_only_old_account_pending_actions(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    actions = GroupActionStore(database)
    for account in ("100", "101"):
        actions.submit(
            f"action-{account}-123", account, "456", "kick_group_member", {"user_id": "42"},
            "panel", 1, approval_required=True,
        )
    assert actions.expire_account("100", "account_changed") == 1
    old = actions.get("100", "456", "action-100-123")
    current = actions.get("101", "456", "action-101-123")
    assert old is not None and old["state"] == "expired"
    assert current is not None and current["state"] == "pending"

def test_group_session_prompt_empty_and_plugin_merge() -> None:
    explicit = {
        "prompt": {"mode": "value", "value": ""},
        "plugins": {"mode": "value", "value": [
            {"name": "search", "mode": "disabled"},
            {"name": "extra", "mode": "enabled", "config": {"limit": 2}},
        ]},
    }
    effective, sources = resolve_group_session(
        {"session_provider": "edictum", "session_type": "simple"}, explicit,
        {"model": "base", "prompt": "原提示词", "plugins": [
            {"name": "search", "enabled": True, "config": {"limit": 1}},
        ]},
    )
    assert effective["prompt"] == ""
    assert sources["prompt"] == "group"
    assert effective["plugins"] == [
        {"name": "search", "enabled": False, "config": {"limit": 1}},
        {"name": "extra", "enabled": True, "config": {"limit": 2}},
    ]
    assert effective["model"] == "base"
    assert sources["model"] == "named_config"

def test_group_session_rejects_duplicate_or_untyped_plugin_override() -> None:
    with pytest.raises(ValueError, match="重复"):
        session_values({"plugins": {"mode": "value", "value": [
            {"name": "x", "mode": "enabled"}, {"name": "x", "mode": "disabled"},
        ]}})
    with pytest.raises(ValueError, match="插件覆盖项"):
        session_values({"plugins": {"mode": "value", "value": ["x"]}})

def test_instance_plugins_win_over_group_plugins() -> None:
    manager = object.__new__(SessionManager)
    cfg = SessionConfig(session_id="sid", session_type_name="simple", provider_name="edictum",
                        session_config={"plugins": [{"name": "instance", "enabled": True}]})
    call = UserCall(group_session_overrides={"plugins": [{"name": "group", "mode": "disabled"}]})
    assert manager._group_plugin_target(cfg, call) == [{"name": "instance", "enabled": True}]

@pytest.mark.asyncio
async def test_group_prompt_overrides_and_instance_prompt_priority() -> None:
    """群提示词覆盖生效且保留历史, 无覆盖时清空不泄漏, 实例显式提示词优先"""
    class Context:
        def __init__(self) -> None:
            self.messages = ["旧消息"]
            self.prompt = "基础"

        async def reset_system_prompt(self, value: str) -> None:
            self.prompt = value

    class SessionStub:
        def __init__(self) -> None:
            self.context = Context()

        def _all_contexts(self) -> dict[str, Context]:
            return {"main": self.context}

    provider = SimpleNamespace()
    definition = SimpleNamespace(params={"model_name": "base"})
    manager = object.__new__(SessionManager)
    manager.provider_registry = cast(Any, SimpleNamespace(resolve_definition=lambda *_: (provider, definition)))
    manager._model_cfg_mgr = None
    session = SessionStub()
    cfg = SessionConfig(session_id="sid", session_type_name="simple", provider_name="edictum")

    await manager._apply_group_session_overrides(
        cfg, cast(Any, session), UserCall(group_session_overrides={"prompt": "群 A 的内容"}), None,
    )
    assert session.context.prompt == "群 A 的内容"
    assert session.context.messages == ["旧消息"]

    await manager._apply_group_session_overrides(cfg, cast(Any, session), UserCall(), None)
    assert session.context.prompt == ""
    assert session.context.messages == ["旧消息"]

    cfg.session_config["system_prompt"] = "实例优先"
    await manager._apply_group_session_overrides(
        cfg, cast(Any, session), UserCall(group_session_overrides={"prompt": "群内容"}), None,
    )
    assert session.context.prompt == "实例优先"
    assert session.context.messages == ["旧消息"]

@pytest.mark.asyncio
async def test_model_reload_reapplies_group_override_and_same_name_config(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = {"base": 1, "group-model": 1}
    module = importlib.import_module("satrap.core.framework.SessionManager")
    monkeypatch.setattr(module, "build_llm_from_config", lambda config, **_: (config.name, versions[config.name]))
    manager = object.__new__(SessionManager)
    cfg = SessionConfig(session_id="sid", session_type_name="named", provider_name="edictum")
    definition = SimpleNamespace(params={"model_name": "base"}, model_key="model_name")
    manager.provider_registry = cast(Any, SimpleNamespace(resolve_definition=lambda *_: (SimpleNamespace(), definition)))
    manager._model_cfg_mgr = cast(Any, SimpleNamespace(
        get_llm_config=lambda name: LLMConfig(name=name, api_key="test-key"),
    ))
    cast(Any, manager)._apply_session_context_config = lambda *_: None
    session = SimpleNamespace(llm=("base", 1), _all_contexts=lambda: {})
    session.reload_llm = lambda llm: setattr(session, "llm", llm)
    entry = SimpleNamespace(session=session, session_type="named", async_operation_lock=asyncio.Lock(),
                            sync_operation_lock=threading.RLock())
    manager.pool = cast(Any, SimpleNamespace(list_entries=lambda: {"sid": entry}))
    manager.store = cast(Any, SimpleNamespace(get=lambda _: cfg))

    group_call = UserCall(group_session_overrides={"model": "group-model"})
    await manager._apply_group_session_overrides(cfg, cast(Any, session), group_call, None)
    assert session.llm == ("group-model", 1)
    versions["group-model"] = 2
    await manager.reload_model_configs_async()
    assert session.llm == ("base", 1)
    await manager._apply_group_session_overrides(cfg, cast(Any, session), group_call, None)
    assert session.llm == ("group-model", 2)

    base_call = UserCall(group_session_overrides={"model": "base"})
    await manager._apply_group_session_overrides(cfg, cast(Any, session), base_call, None)
    versions["base"] = 2
    await manager.reload_model_configs_async()
    await manager._apply_group_session_overrides(cfg, cast(Any, session), base_call, None)
    assert session.llm == ("base", 2)

@pytest.mark.asyncio
async def test_retry_group_session_apply_reports_failure_and_recovery_without_turn() -> None:
    manager = object.__new__(SessionManager)
    cfg = SessionConfig(session_id="sid", session_type_name="named", provider_name="edictum")
    entry = SimpleNamespace(session=SimpleNamespace(), async_operation_lock=asyncio.Lock(), instance_generation="gen")
    manager.pool = cast(Any, SimpleNamespace(list_entries=lambda: {"sid": entry}))
    manager.store = cast(Any, SimpleNamespace(get=lambda _: cfg))
    manager._group_plugin_target = cast(Any, lambda *_: [])
    manager._prepare_session_async = cast(Any, AsyncMock())
    apply = AsyncMock(side_effect=[RuntimeError("群插件配置应用失败"), None])
    manager._apply_group_session_overrides = cast(Any, apply)
    reports: list[tuple[str, str | None]] = []
    manager.group_apply_reporter = lambda call, session_id, generation, error: reports.append((session_id, error))
    call = UserCall(group_config_revision=2, group_session_overrides={"plugins": []})

    assert not await manager.retry_group_session_apply_async("sid", call, "gen", lambda: True)
    assert await manager.retry_group_session_apply_async("sid", call, "gen", lambda: True)
    assert reports == [("sid", "群插件配置应用失败"), ("sid", None)]
    assert apply.await_count == 2

@pytest.mark.asyncio
async def test_retry_discards_instance_replaced_while_waiting_for_lock() -> None:
    manager = object.__new__(SessionManager)
    cfg = SessionConfig(session_id="sid", session_type_name="named", provider_name="edictum")
    lock = asyncio.Lock()
    entry = SimpleNamespace(session=SimpleNamespace(), async_operation_lock=lock, instance_generation="first")
    manager.pool = cast(Any, SimpleNamespace(list_entries=lambda: {"sid": entry}))
    manager.store = cast(Any, SimpleNamespace(get=lambda _: cfg))
    manager._group_plugin_target = cast(Any, lambda *_: [])
    manager._prepare_session_async = cast(Any, AsyncMock())
    apply_mock = AsyncMock()
    manager._apply_group_session_overrides = cast(Any, apply_mock)
    manager.group_apply_reporter = lambda *_: None
    await lock.acquire()
    retry = asyncio.create_task(manager.retry_group_session_apply_async(
        "sid", UserCall(group_config_revision=2), "first", lambda: True,
    ))
    await asyncio.sleep(0)
    entry.instance_generation = "second"
    lock.release()
    assert not await retry
    apply_mock.assert_not_awaited()

@pytest.mark.asyncio
async def test_real_edictum_session_applies_prompt_and_plugin_overlay(tmp_path: Path) -> None:
    """真实 Edictum Provider 在已有工作流上应用覆盖并保留普通历史"""
    registry = create_default_edictum_type_registry()
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_manager.create("assistant", {
        "edictum_type": "async_simple", "model_name": "base", "params": {"system_prompt": "基础提示"},
        "plugins": ["session_commands"],
    })
    provider = EdictumProvider(config_manager, registry, default_checkpoint_db=str(tmp_path / "platform.db"))
    cfg = SessionConfig(session_id="group-runtime", session_type_name="assistant", provider_name="edictum")
    session = provider.create_session(cfg, llm=cast(Any, object()))
    assert isinstance(session, AsyncSimpleSession)
    session.plugin_environment = PluginEnvironment("platform", "onebot")
    manager = object.__new__(SessionManager)
    manager.provider_registry = SessionProviderRegistry()
    manager.provider_registry.register(provider)
    manager._model_cfg_mgr = None
    try:
        await provider.prepare_session_async(session)
        assert session._wf is not None
        context = session._wf.ctx
        await context.add_user_message("已有历史")
        call = UserCall(group_session_overrides={
            "prompt": "", "plugins": [{"name": "session_commands", "mode": "disabled"}],
        })
        target = manager._group_plugin_target(cfg, call)
        await manager._apply_group_session_overrides(cfg, session, call, target)
        assert context._messages[0] == {"role": "system", "content": ""}
        assert any("已有历史" in str(item.get("content")) for item in context._messages)
        assert session.list_plugins() == []
        setattr(session, "_satrap_group_plugins", None)
        await provider.prepare_session_async(session)
        await manager._apply_group_session_overrides(cfg, session, UserCall(), None)
        assert context._messages[0] == {"role": "system", "content": "基础提示"}
        assert any("已有历史" in str(item.get("content")) for item in context._messages)
        assert len(session.list_plugins()) == 1
    finally:
        await provider.release_session_async(session)

def _runtime(tmp_path: Path) -> tuple[BackendManager, OneBotAdapter]:
    backend = BackendManager(BackendConfig(
        data_root=str(tmp_path),
        platforms=[{"id": "bot", "type": "onebot", "settings": {"group_management_version": 1}}],
    ))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"group_management_version": 1}))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    adapter.set_group_access_store(GroupDirectoryStore(backend.platform_db_path("bot")))
    return backend, adapter

@pytest.mark.asyncio
async def test_sync_deduplicates_and_publishes_complete_snapshot(tmp_path: Path) -> None:
    backend, adapter = _runtime(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch() -> dict[str, object]:
        entered.set()
        await release.wait()
        return {"items": [{"group_id": "123", "group_name": "群"}],
                "complete": True, "truncated": False, "reason": None}

    adapter.admin.fetch_group_directory = AsyncMock(side_effect=fetch)
    first = await backend.trigger_group_sync("bot", "100")
    await entered.wait()
    second = await backend.trigger_group_sync("bot", "100")
    assert second["sync_id"] == first["sync_id"] and second["reused"] is True
    release.set()
    await backend._group_sync_tasks[("bot", "100")][1]
    assert adapter.admin.fetch_group_directory.await_count == 1
    assert (await backend.group_sync_status("bot", "100", first["sync_id"]))["complete"] is True
    assert (await backend.list_groups("bot", "100"))["items"][0]["group_id"] == "123"

@pytest.mark.asyncio
async def test_slow_legacy_group_session_turn_reports_pending_then_failed_then_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, adapter = _runtime(tmp_path)
    store = GroupDirectoryStore(backend.platform_db_path("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    store.confirm_membership("100", "456", True)
    store.patch_group("100", "456", "session", {
        "prompt": {"mode": "value", "value": "新提示词"},
    }, expected_revision=0)
    await adapter.refresh_group_access("100")
    generation = (await backend.group_config("bot", "100", "456"))["route_generation"]
    manager = backend._create_session_manager("bot")
    backend._platform_runtimes["bot"] = cast(Any, (manager, None))
    cfg = SessionConfig(session_id="sid", session_type_name="named", provider_name="edictum")
    entry = SimpleNamespace(session=SimpleNamespace(), async_operation_lock=asyncio.Lock(), instance_generation="first")
    monkeypatch.setattr(manager, "_resolve_or_create_session_config", lambda _: cfg)
    monkeypatch.setattr(manager, "_acquire_or_create_entry_async", AsyncMock(return_value=entry))
    monkeypatch.setattr(manager.pool, "list_entries", lambda: {"sid": entry})
    monkeypatch.setattr(manager.pool, "release", lambda _: None)
    monkeypatch.setattr(manager.store, "get", lambda _: cfg)
    monkeypatch.setattr(manager, "_group_plugin_target", lambda *_: None)
    monkeypatch.setattr(manager, "_prepare_session_async", AsyncMock())
    monkeypatch.setattr(manager, "cleanup_idle_sessions_async", AsyncMock())
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail_after_wait(*_):
        entered.set()
        await release.wait()
        raise RuntimeError("群插件配置应用失败")

    monkeypatch.setattr(manager, "_apply_group_session_overrides", fail_after_wait)
    call = UserCall(session_id="sid", group_config_revision=1, group_route_generation=generation,
                    group_session_overrides={"prompt": "新提示词"},
                    origin=CallOrigin("bot", "100", "GroupMessage", "456", "123", "1", "request"))
    turn = asyncio.create_task(manager.handle_call_async(call))
    await entered.wait()
    pending = await backend.group_config("bot", "100", "456")
    assert pending["apply_status"] == "pending" and pending["active_instance_count"] == 1
    release.set()
    assert await turn == ""
    failed = await backend.group_config("bot", "100", "456")
    assert failed["apply_status"] == "failed" and failed["active_instance_count"] == 1
    assert failed["apply_error"] == "群插件配置应用失败"

    monkeypatch.setattr(manager, "_apply_group_session_overrides", AsyncMock())
    recovered = await backend.apply_group_config("bot", "100", "456", 1)
    assert recovered["apply_status"] == "applied" and recovered["active_instance_count"] == 1

@pytest.mark.asyncio
async def test_shared_legacy_instance_keeps_group_results_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, adapter = _runtime(tmp_path)
    store = GroupDirectoryStore(backend.platform_db_path("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    for group_id in ("456", "789"):
        store.confirm_membership("100", group_id, True)
        store.patch_group("100", group_id, "session", {
            "prompt": {"mode": "value", "value": f"群 {group_id}"},
        }, expected_revision=0)
    await adapter.refresh_group_access("100")
    manager = backend._create_session_manager("bot")
    backend._platform_runtimes["bot"] = cast(Any, (manager, None))
    cfg = SessionConfig(session_id="legacy-sid", session_type_name="named", provider_name="edictum")
    entry = SimpleNamespace(session=SimpleNamespace(), async_operation_lock=asyncio.Lock(), instance_generation="first")
    monkeypatch.setattr(manager, "_resolve_or_create_session_config", lambda _: cfg)
    monkeypatch.setattr(manager, "_acquire_or_create_entry_async", AsyncMock(return_value=entry))
    monkeypatch.setattr(manager.pool, "list_entries", lambda: {"legacy-sid": entry})
    monkeypatch.setattr(manager.pool, "release", lambda _: None)
    monkeypatch.setattr(manager.store, "get", lambda _: cfg)
    monkeypatch.setattr(manager, "_group_plugin_target", lambda *_: None)
    monkeypatch.setattr(manager, "_prepare_session_async", AsyncMock())
    monkeypatch.setattr(manager, "_invoke_sync_entry", lambda *_: "ok")
    monkeypatch.setattr(manager, "cleanup_idle_sessions_async", AsyncMock())

    async def apply(_, __, call: UserCall, ___) -> None:
        if call.origin is not None and call.origin.chat_id == "789":
            raise RuntimeError("群 B 应用失败")

    monkeypatch.setattr(manager, "_apply_group_session_overrides", apply)

    def call(group_id: str) -> UserCall:
        return UserCall(session_id="legacy-sid", group_config_revision=1, group_route_generation=0,
                        group_session_overrides={"prompt": f"群 {group_id}"},
                        origin=CallOrigin("bot", "100", "GroupMessage", group_id, "123", "1", "request"))

    assert await manager.handle_call_async(call("456")) == "ok"
    assert await manager.handle_call_async(call("789")) == ""
    group_a = await backend.group_config("bot", "100", "456")
    group_b = await backend.group_config("bot", "100", "789")
    assert group_a["apply_status"] == "applied" and group_a["active_instance_count"] == 1
    assert group_b["apply_status"] == "failed" and group_b["active_instance_count"] == 1
    monkeypatch.setattr(manager, "_apply_group_session_overrides", AsyncMock())
    recovered = await backend.apply_group_config("bot", "100", "789", 1)
    assert recovered["apply_status"] == "applied" and recovered["active_instance_count"] == 1
    assert (await backend.group_config("bot", "100", "456"))["apply_status"] == "applied"

    store.patch_group("100", "789", "session", {
        "prompt": {"mode": "value", "value": "群 B 新配置"},
    }, expected_revision=1)
    await adapter.refresh_group_access("100")
    reporter = manager.group_apply_reporter
    assert reporter is not None
    reporter(call("789"), "legacy-sid", "first", None)
    changed = await backend.group_config("bot", "100", "789")
    assert changed["apply_status"] == "pending" and changed["active_instance_count"] == 1
    assert (await backend.group_config("bot", "100", "456"))["apply_status"] == "applied"

    entry.instance_generation = "second"
    assert (await backend.group_config("bot", "100", "789"))["active_instance_count"] == 0
    reporter(call("789"), "legacy-sid", "first", "迟到失败")
    assert (await backend.group_config("bot", "100", "789"))["apply_status"] == "applied"

@pytest.mark.asyncio
async def test_group_save_pauses_only_target_until_snapshot_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, adapter = _runtime(tmp_path)
    store = GroupDirectoryStore(backend.platform_db_path("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    for group_id in ("456", "789"):
        store.confirm_membership("100", group_id, True)
        store.patch_group("100", group_id, "policy", {
            "enabled": {"mode": "value", "value": True},
        }, expected_revision=0)
    await adapter.refresh_group_access("100")
    current = await backend.group_config("bot", "100", "456")
    original = adapter.refresh_group_access
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused(self_id: str) -> None:
        entered.set()
        await release.wait()
        await original(self_id)

    monkeypatch.setattr(adapter, "refresh_group_access", paused)
    task = asyncio.create_task(backend.patch_group_config(
        "bot", "100", "456", expected_revision=1, base_revision=current["base_revision"],
        section="policy", values={"enabled": {"mode": "value", "value": False}},
    ))
    await entered.wait()
    assert not adapter.allows_group("456")
    assert adapter.group_route("456")[1] == -1
    assert adapter.allows_group("789")
    assert adapter.allows_management_target("789")
    release.set()
    saved = await task
    assert saved["apply_status"] == "applied"
    assert not adapter.allows_group("456")
    assert adapter.allows_group("789")

@pytest.mark.asyncio
async def test_account_mode_restriction_survives_conflict_and_uncertain_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """账号模式切换: 显式群保留, 失败后按已落盘修订收紧, 提交结果未知也不回退"""
    backend, adapter = _runtime(tmp_path)
    store = GroupDirectoryStore(backend.platform_db_path("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    store.patch_account("100", expected_revision=1, mode="all", approval_defaults={})
    for group_id in ("456", "789"):
        store.confirm_membership("100", group_id, True)
    store.patch_group("100", "456", "policy", {"enabled": {"mode": "value", "value": True}}, expected_revision=0)
    await adapter.refresh_group_access("100")
    assert adapter.allows_group("456") and adapter.allows_group("789")
    original = adapter.refresh_group_access

    async def failed(_: str) -> None:
        raise RuntimeError("read failed")

    monkeypatch.setattr(adapter, "refresh_group_access", failed)
    result = await backend.patch_group_settings("bot", "100", 2, "selected", {})
    assert result["apply_status"] == "failed" and result["active_revision"] == 2
    assert adapter.allows_group("456") and not adapter.allows_group("789")
    assert adapter.allows_management_target("789")
    with pytest.raises(Exception, match="已变化"):
        await backend.patch_group_settings("bot", "100", 2, "selected", {})
    assert not adapter.allows_group("789")
    monkeypatch.setattr(adapter, "refresh_group_access", original)
    recovered = await backend.apply_group_settings("bot", "100", 3)
    assert recovered["apply_status"] == "applied" and recovered["active_revision"] == 3
    account = store.read_account("100")
    assert account is not None and account["revision"] == 3
    assert adapter.allows_group("456") and not adapter.allows_group("789")
    assert adapter.allows_management_target("789")

    # 提交结果未知: 修订已落盘, 访问按新模式收紧
    backend2, adapter2 = _runtime(tmp_path / "uncertain")
    store2 = GroupDirectoryStore(backend2.platform_db_path("bot"))
    store2.adopt_legacy("100", {"group_management_version": 1})
    store2.patch_account("100", expected_revision=1, mode="all", approval_defaults={})
    store2.confirm_membership("100", "456", True)
    await adapter2.refresh_group_access("100")
    commit = GroupDirectoryStore.patch_account

    def commit_then_fail(self: GroupDirectoryStore, self_id: str, *, expected_revision: int,
                         mode: str, approval_defaults: Mapping[str, object]) -> dict[str, Any]:
        commit(self, self_id, expected_revision=expected_revision, mode=mode, approval_defaults=approval_defaults)
        raise RuntimeError("commit result unknown")

    monkeypatch.setattr(GroupDirectoryStore, "patch_account", commit_then_fail)
    with pytest.raises(RuntimeError, match="unknown"):
        await backend2.patch_group_settings("bot", "100", 2, "selected", {})
    account2 = store2.read_account("100")
    assert account2 is not None and account2["revision"] == 3
    assert not adapter2.allows_group("456")
    assert adapter2.allows_management_target("456")

@pytest.mark.asyncio
async def test_sync_discards_result_after_connection_generation_changes(tmp_path: Path) -> None:
    backend, adapter = _runtime(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch() -> dict[str, object]:
        entered.set()
        await release.wait()
        return {"items": [{"group_id": "123"}], "complete": True, "truncated": False, "reason": None}

    adapter.admin.fetch_group_directory = AsyncMock(side_effect=fetch)
    task = await backend.trigger_group_sync("bot", "100")
    await entered.wait()
    adapter._connection_generation += 1
    release.set()
    await backend._group_sync_tasks[("bot", "100")][1]
    assert (await backend.group_sync_status("bot", "100", task["sync_id"]))["reason"] == "stale_connection"
    assert (await backend.list_groups("bot", "100"))["total"] == 0

@pytest.mark.asyncio
async def test_http_directory_requires_account_and_sync_uses_fixed_identity(tmp_path: Path) -> None:
    backend, adapter = _runtime(tmp_path)
    store = GroupDirectoryStore(backend.platform_db_path("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    server = BackendHTTPServer(backend)
    status, result = await server._route("GET", "/api/platforms/bot/groups", b"")
    assert status == 400 and result["reason"] == "missing_account"
    status, result = await server._route("GET", "/api/platforms/bot/groups?account=100", b"")
    assert status == 200 and result["account"] == "100"
    status, result = await server._route(
        "POST", "/api/platforms/bot/groups/sync", json.dumps({"expected_self_id": "200"}).encode("utf-8"),
    )
    assert status == 409 and result["reason"] == "account_changed"
    adapter.admin.fetch_group_directory = AsyncMock(return_value={
        "items": [], "complete": True, "truncated": False, "reason": None,
    })
    status, result = await server._route(
        "POST", "/api/platforms/bot/groups/sync", json.dumps({"expected_self_id": "100"}).encode("utf-8"),
    )
    assert status == 202
    await backend._group_sync_tasks[("bot", "100")][1]
    status, detail = await server._route("GET", f"/api/platforms/bot/groups/sync/{result['sync_id']}?account=100", b"")
    assert status == 200 and detail["complete"] is True

@pytest.mark.asyncio
async def test_hidden_notice_keeps_internal_self_membership_update(tmp_path: Path) -> None:
    backend, adapter = _runtime(tmp_path)
    adapter.config.settings["notice_types"] = []
    store = GroupDirectoryStore(backend.platform_db_path("bot"))
    await adapter._handle_notice({
        "self_id": 100, "group_id": 123, "user_id": 100,
        "post_type": "notice", "notice_type": "group_increase", "time": 1,
    })
    assert store.list_groups("100")["total"] == 1
    await adapter._handle_notice({
        "self_id": 100, "group_id": 123, "user_id": 456,
        "post_type": "notice", "notice_type": "group_decrease", "time": 2,
    })
    assert store.list_groups("100")["total"] == 1
    await adapter._handle_notice({
        "self_id": 100, "group_id": 123, "user_id": 100,
        "post_type": "notice", "notice_type": "group_decrease", "time": 3,
    })
    assert store.list_groups("100")["total"] == 0
    assert store.list_groups("100", membership="left")["total"] == 1

@pytest.mark.asyncio
async def test_settings_route_updates_current_account_and_rejects_friend_policy(tmp_path: Path) -> None:
    backend, adapter = _runtime(tmp_path)
    store = GroupDirectoryStore(backend.platform_db_path("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    store.confirm_membership("100", "123", True)
    server = BackendHTTPServer(backend)
    status, accounts = await server._route("GET", "/api/platforms/bot/groups/accounts", b"")
    assert status == 200 and accounts["current_account"] == "100"
    status, settings = await server._route("GET", "/api/platforms/bot/groups/settings?account=100", b"")
    assert status == 200 and settings["mode"] == "selected"
    assert settings["approval_inheriting_counts"]["kick_group_member"] == 1
    assert any(item["action_type"] == "kick_group_member" and item["risk"] == "high"
               for item in settings["approval_actions"])
    invalid = {"expected_self_id": "100", "expected_revision": 1, "mode": "all",
               "approval_defaults": {"handle_friend_request": "auto_execute"}}
    status, body = await server._route(
        "PATCH", "/api/platforms/bot/groups/settings", json.dumps(invalid).encode("utf-8"),
    )
    assert status == 400 and body["reason"] == "invalid_group_query"
    invalid["approval_defaults"] = {"kick_group_member": "approval_required"}
    status, saved = await server._route(
        "PATCH", "/api/platforms/bot/groups/settings", json.dumps(invalid).encode("utf-8"),
    )
    assert status == 200 and saved["mode"] == "all" and saved["apply_status"] == "applied"
    assert saved["approval_inheriting_counts"]["kick_group_member"] == 1
    assert adapter.allows_group("123")
    status, body = await server._route(
        "PATCH", "/api/platforms/bot/groups/settings", json.dumps(invalid).encode("utf-8"),
    )
    assert status == 409 and body["reason"] == "group_config_conflict"

@pytest.mark.asyncio
async def test_group_policy_patch_updates_runtime_and_checks_base_revision(tmp_path: Path) -> None:
    backend, adapter = _runtime(tmp_path)
    store = GroupDirectoryStore(backend.platform_db_path("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    store.confirm_membership("100", "123", True)
    await adapter._ensure_group_access("100")
    server = BackendHTTPServer(backend)
    path = "/api/platforms/bot/groups/123/config"
    status, original = await server._route("GET", path + "?account=100", b"")
    assert status == 200 and original["effective"]["policy"]["enabled"] is False
    body = {"expected_self_id": "100", "expected_revision": 0,
            "base_revision": original["base_revision"], "section": "policy",
            "values": {"enabled": {"mode": "value", "value": True},
                       "wake_mode": {"mode": "value", "value": "frequency"}}}
    status, saved = await server._route("PATCH", path, json.dumps(body).encode("utf-8"))
    assert status == 200 and saved["apply_status"] == "applied"
    assert adapter.allows_group("123")
    assert adapter.resolve_policy_settings("123")["wake_mode"] == "frequency"
    assert not adapter.allows_group("456")
    body["base_revision"] = "stale"
    body["expected_revision"] = 1
    status, conflict = await server._route("PATCH", path, json.dumps(body).encode("utf-8"))
    assert status == 409 and conflict["reason"] == "group_config_conflict"

@pytest.mark.asyncio
async def test_control_route_reads_offline_snapshot_and_saves_cold_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from satrap.core.backend import control_server as control

    path = tmp_path / "config.json"
    save_config_document(path, {
        "data_root": str(tmp_path / "data"),
        "platforms": [{"id": "bot", "type": "onebot",
                       "settings": {"self_id": "100", "group_management_version": 1}}],
    })
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    monkeypatch.setattr(control, "_check_backend_health", lambda: {"running": False})
    store = GroupDirectoryStore(control._configured_storage_layout().platform_db("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    assert control._is_control_api_path("/api/platforms/bot/groups")
    context = control._RouteContext(
        "GET", "/api/platforms/bot/groups/accounts", "/api/platforms/bot/groups/accounts", asyncio.StreamReader(), b"",
    )
    result = await control._route_group_directory(context)
    assert result is not None
    status, accounts = result
    assert status == 200 and accounts["current_account"] == "100"
    query = "/api/platforms/bot/groups?account=100"
    context = control._RouteContext("GET", "/api/platforms/bot/groups", query, asyncio.StreamReader(), b"")
    result = await control._route_group_directory(context)
    assert result is not None
    status, listing = result
    assert status == 200 and listing["offline_snapshot"] is True
    payload = {"expected_self_id": "100", "expected_revision": 1,
               "mode": "all", "approval_defaults": {}}
    body = json.dumps(payload).encode("utf-8")
    reader = asyncio.StreamReader()
    reader.feed_data(body)
    reader.feed_eof()
    route = "/api/platforms/bot/groups/settings"
    raw = f"PATCH {route} HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode("utf-8")
    context = control._RouteContext("PATCH", route, route, reader, raw)
    result = await control._route_group_directory(context)
    assert result is not None
    status, saved = result
    assert status == 200 and saved["mode"] == "all" and saved["apply_status"] == "pending"
    assert isinstance(saved["approval_inheriting_counts"], dict)
    assert saved["approval_inheriting_counts"]["kick_group_member"] == 0
    save_config_document(path, {
        "data_root": str(tmp_path / "data"),
        "platforms": [{"id": "bot", "type": "onebot", "settings": {"group_management_version": 1}}],
    })
    context = control._RouteContext(
        "GET", "/api/platforms/bot/groups/accounts", "/api/platforms/bot/groups/accounts", asyncio.StreamReader(), b"",
    )
    result = await control._route_group_directory(context)
    assert result is not None
    status, accounts = result
    assert status == 200 and accounts["current_account"] == "" and accounts["waiting_for_account"] is True
    reader = asyncio.StreamReader()
    reader.feed_data(body)
    reader.feed_eof()
    context = control._RouteContext("PATCH", route, route, reader, raw)
    result = await control._route_group_directory(context)
    assert result is not None
    status, rejected = result
    assert status == 409 and rejected["reason"] == "account_changed"
