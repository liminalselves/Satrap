"""群目录运行时同步任务和 HTTP 路由的身份与去重反例"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace
from typing import Any, Mapping, cast
import asyncio
import json
import sqlite3

import pytest

from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.call_context import CallOrigin
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.config.document import save_config_document
from satrap.core.conversation import ConversationRoute
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform import PlatformAdapterManager, PlatformConfig
from satrap.core.type import SessionConfig, UserCall


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
async def test_group_session_apply_status_and_retry_reaches_failed_instance(tmp_path: Path) -> None:
    backend, adapter = _runtime(tmp_path)
    store = GroupDirectoryStore(backend.platform_db_path("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    store.confirm_membership("100", "456", True)
    store.patch_group("100", "456", "policy", {"enabled": {"mode": "value", "value": True}}, expected_revision=0)
    await adapter.refresh_group_access("100")
    store.patch_group("100", "456", "session", {
        "prompt": {"mode": "value", "value": "新提示词"},
    }, expected_revision=1)
    await adapter.refresh_group_access("100")
    pending = await backend.group_config("bot", "100", "456")
    assert pending["apply_status"] == "applied"
    assert pending["active_revision"] == 2
    generation = pending["route_generation"]
    route = ConversationRoute("1", "bot", "simple", "edictum", "group_member", "100", "456", generation)
    with sqlite3.connect(backend.platform_db_path("bot")) as connection:
        connection.execute("CREATE TABLE context_sessions (context_key TEXT, platform TEXT, session_id TEXT)")
        connection.execute("INSERT INTO context_sessions VALUES (?, ?, ?)", (route.key, "bot", "sid"))
    entry = SimpleNamespace(instance_generation="first")
    entries = {"sid": entry}

    async def retry(session_id, call, instance_generation, is_current):
        assert session_id == "sid" and instance_generation == "first" and is_current()
        assert call.group_session_overrides == {"prompt": "新提示词"}
        adapter.report_group_session_apply("100", "456", 2, generation, session_id, instance_generation, None)
        return True

    runtime = SimpleNamespace(pool=SimpleNamespace(list_entries=lambda: entries),
                              retry_group_session_apply_async=retry)
    backend._platform_runtimes["bot"] = cast(Any, (runtime, None))
    assert (await backend.group_config("bot", "100", "456"))["apply_status"] == "pending"
    adapter.report_group_session_apply("100", "456", 2, generation, "sid", "first", "群插件配置应用失败")
    failed = await backend.group_config("bot", "100", "456")
    assert failed["apply_status"] == "failed"
    assert failed["apply_error"] == "群插件配置应用失败"
    recovered = await backend.apply_group_config("bot", "100", "456", 2)
    assert recovered["apply_status"] == "applied"
    assert recovered["active_revision"] == 2
    adapter.report_group_session_apply("100", "456", 2, generation, "sid", "first", "旧实例失败")
    entry.instance_generation = "second"
    assert (await backend.group_config("bot", "100", "456"))["apply_status"] == "pending"
    adapter.report_group_session_apply("100", "456", 2, generation, "sid", "second", None)
    assert (await backend.group_config("bot", "100", "456"))["apply_status"] == "applied"
    entries.clear()
    assert (await backend.group_config("bot", "100", "456"))["apply_status"] == "applied"


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
async def test_account_mode_restriction_preserves_explicit_groups_and_survives_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
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


@pytest.mark.asyncio
async def test_account_mode_restriction_survives_uncertain_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, adapter = _runtime(tmp_path)
    store = GroupDirectoryStore(backend.platform_db_path("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    store.patch_account("100", expected_revision=1, mode="all", approval_defaults={})
    store.confirm_membership("100", "456", True)
    await adapter.refresh_group_access("100")
    original = GroupDirectoryStore.patch_account

    def commit_then_fail(
        self: GroupDirectoryStore, self_id: str, *, expected_revision: int,
        mode: str, approval_defaults: Mapping[str, object],
    ) -> dict[str, Any]:
        original(self, self_id, expected_revision=expected_revision, mode=mode,
                 approval_defaults=approval_defaults)
        raise RuntimeError("commit result unknown")

    monkeypatch.setattr(GroupDirectoryStore, "patch_account", commit_then_fail)
    with pytest.raises(RuntimeError, match="unknown"):
        await backend.patch_group_settings("bot", "100", 2, "selected", {})
    account = store.read_account("100")
    assert account is not None and account["revision"] == 3
    assert not adapter.allows_group("456")
    assert adapter.allows_management_target("456")


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
    assert control._is_control_api_path("/platforms/bot/groups")
    context = control._RouteContext(
        "GET", "/platforms/bot/groups/accounts", "/platforms/bot/groups/accounts", asyncio.StreamReader(), b"",
    )
    result = await control._route_group_directory(context)
    assert result is not None
    status, accounts = result
    assert status == 200 and accounts["current_account"] == "100"
    query = "/platforms/bot/groups?account=100"
    context = control._RouteContext("GET", "/platforms/bot/groups", query, asyncio.StreamReader(), b"")
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
    route = "/platforms/bot/groups/settings"
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
        "GET", "/platforms/bot/groups/accounts", "/platforms/bot/groups/accounts", asyncio.StreamReader(), b"",
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
