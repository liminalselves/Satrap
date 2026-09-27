"""群目录运行时同步任务和 HTTP 路由的身份与去重反例"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
import asyncio
import json

import pytest

from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.config.document import save_config_document
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform import PlatformAdapterManager, PlatformConfig


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
    server = BackendHTTPServer(backend)
    status, accounts = await server._route("GET", "/api/platforms/bot/groups/accounts", b"")
    assert status == 200 and accounts["current_account"] == "100"
    status, settings = await server._route("GET", "/api/platforms/bot/groups/settings?account=100", b"")
    assert status == 200 and settings["mode"] == "selected"
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
