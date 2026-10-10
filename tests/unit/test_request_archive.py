"""申请本地归档, 持久执行资格和人工确认契约"""
from __future__ import annotations

import asyncio
import json
import time
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from satrap.core.call_context import bind_call_origin
from satrap.core.platform.onebot.request_registry import RequestApprovalLedger, RequestFlagRegistry
from satrap.core.platform.request_inbox import flag_digest
from satrap.core.backend.BackendManager import BackendManager, BackendConfig
from satrap.core.platform import PlatformAdapterManager, PlatformConfig
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.config.group_directory import GroupDirectoryStore
from unittest.mock import AsyncMock
from types import SimpleNamespace
from satrap.core.backend.http_api import BackendHTTPServer
from unit.test_group_admin_plugin import _origin, _setup_adapter
from unit.test_request_inbox import attach_friend_host, incoming, tool, restore_manager


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: value[0])
    return value


async def archived_registry(tmp_path, clock, kind="friend"):
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    await registry.register(kind, "secret", self_id="10000", group_id="456" if kind == "group" else "", sub_type="add" if kind == "group" else "", user_id="321")
    initial = (await registry.list_requests(kind, self_id="10000"))["items"][0]
    clock[0] = 1700.0
    assert not (await registry.list_requests(kind, self_id="10000"))["items"]
    archived = (await registry.list_requests(kind, self_id="10000", view="archived"))["items"][0]
    assert archived["request_id"] == initial["request_id"]
    assert archived["revision"] > initial["revision"]
    return registry, archived


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["friend", "group"])
async def test_repeated_expired_event_restores_missing_details_without_resetting_ledger(tmp_path, clock, kind):
    registry, initial = await archived_registry(tmp_path, clock, kind)
    registry.inbox.delete("ob", "10000", kind, initial["request_id"], initial["revision"], clock[0])
    assert not (await registry.list_requests(kind, self_id="10000", view="all"))["items"]
    assert not await registry.register(kind, "secret", self_id="10000", user_id="321",
                                       group_id="456" if kind == "group" else "",
                                       sub_type="add" if kind == "group" else "", comment="重新上报")
    page = await registry.list_requests(kind, self_id="10000", view="archived")
    assert page["unavailable_count"] == 0
    item = page["items"][0]
    assert item["received_at"] == 1000 and item["archived"] and item["requires_confirmation"]
    assert item["comment"] == "重新上报"
    entry = registry.ledger.lookup("ob", "10000", kind, "secret")
    assert entry is not None and entry["state"] == "expired"


@pytest.mark.asyncio
async def test_repeated_recent_event_restores_missing_details_in_same_process(tmp_path, clock):
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    await registry.register("friend", "secret", self_id="10000", user_id="321")
    inbox_path = registry.ledger.inbox_path
    assert inbox_path is not None
    with sqlite3.connect(inbox_path) as connection:
        connection.execute("DELETE FROM requests")
    clock[0] = 1100
    assert not await registry.register("friend", "secret", self_id="10000", user_id="321", comment="补回详情")
    page = await registry.list_requests("friend", self_id="10000")
    assert page["unavailable_count"] == 0 and page["items"][0]["comment"] == "补回详情"
    assert page["items"][0]["received_at"] == 1000 and not page["items"][0]["archived"]
    assert await registry.register("friend", "new-flag", self_id="10000", user_id="321")
    assert (await registry.list_requests("friend", self_id="10000"))["total"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["friend", "group"])
async def test_archive_survives_restart_and_requires_explicit_confirmation(tmp_path, clock, kind):
    registry, item = await archived_registry(tmp_path, clock, kind)
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    assert item["can_handle"] and item["requires_confirmation"]
    assert "secret" not in json.dumps(item)
    with pytest.raises(LookupError):
        await registry.resolve_request(kind, item["request_id"], self_id="10000")
    with pytest.raises(ValueError):
        await registry.resolve_request(kind, item["request_id"], self_id="10000", allow_archived=True, expected_revision=1)
    result = await registry.resolve_request(kind, item["request_id"], self_id="10000", allow_archived=True, expected_revision=item["revision"])
    assert result["flag"] == "secret"
    check = await registry.recheck_request(kind, item["request_id"], self_id="10000")
    assert check["verification"] == "local_only" and not check["platform_query_supported"]
    assert check["platform_state"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("state,decision", [("completed", "accepted"), ("completed", "rejected"), ("unknown", None)])
async def test_archived_request_single_winner_and_terminal_history_cannot_replay(tmp_path, clock, state, decision):
    registry, item = await archived_registry(tmp_path, clock)
    calls = await asyncio.gather(*(registry.occupy("friend", "secret", self_id="10000", allow_archived=True) for _ in range(2)), return_exceptions=True)
    assert sum(not isinstance(result, Exception) for result in calls) == 1
    await registry.settle("friend", "secret", self_id="10000", state=state, decision=decision)
    history = (await registry.list_requests("friend", self_id="10000", view="archived"))["items"][0]
    assert not history["can_handle"] and history["decision"] == decision
    assert history["execution_state"] == ("unknown" if state == "unknown" else "succeeded")
    assert registry.inbox.resolve("ob", "10000", "friend", item["request_id"], clock[0])["flag"] == ""
    await registry.delete_request("friend", history["request_id"], self_id="10000", expected_revision=history["revision"])
    await registry.register("friend", "secret", self_id="10000", user_id="321")
    assert not (await registry.list_requests("friend", self_id="10000", view="all"))["items"]
    with pytest.raises(LookupError):
        await registry.occupy("friend", "secret", self_id="10000", allow_archived=True)


@pytest.mark.asyncio
async def test_retention_clears_credential_then_history_without_resetting_ledger(tmp_path, clock):
    registry, item = await archived_registry(tmp_path, clock)
    registry.inbox.policy("ob", "10000", {"credential_days": 1, "history_days": 2})
    assert registry.inbox.policy("other", "10000") == {"credential_days": 30, "history_days": 90}
    clock[0] = 1000 + 86400 + 1
    history = (await registry.list_requests("friend", self_id="10000", view="archived"))["items"][0]
    assert not history["can_handle"]
    registry.inbox.policy("ob", "10000", {"credential_days": 2, "history_days": 2})
    assert registry.inbox.resolve("ob", "10000", "friend", item["request_id"], clock[0])["flag"] == ""
    clock[0] = 1000 + 2 * 86400 + 1
    assert not (await registry.list_requests("friend", self_id="10000", view="all"))["items"]
    await registry.register("friend", "secret", self_id="10000", user_id="321")
    assert not (await registry.list_requests("friend", self_id="10000", view="all"))["items"]


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_panel_can_process_archived_friend_request_with_current_revision(tmp_path, clock, approve):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    attach_friend_host(adapter)
    await incoming(adapter)
    clock[0] = 1700
    item = (await adapter.friend_requests("10000", 20, None, view="archived"))["items"][0]
    result = await adapter.friend_host.submit("10000", "archive-panel", "handle_request", {"request_id": item["request_id"], "expected_revision": item["revision"], "approve": approve}, actor="panel")
    assert result["state"] == "succeeded"
    adapter._bot.set_friend_add_request.assert_awaited_once_with(flag="private-flag", approve=approve, remark="")


@pytest.mark.asyncio
async def test_model_archived_request_waits_for_approval_then_executes_once(tmp_path, clock):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    await incoming(adapter)
    clock[0] = 1700
    with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
        item = (await tool("friend_manager_list_requests").execute(view="archived"))["data"]["items"][0]
        handle = tool("friend_manager_handle_request")
        result = await handle.execute(request_id=item["request_id"], expected_revision=item["revision"], approve=True)
        assert result["ok"] and result["data"]["state"] == "pending"
        adapter._bot.set_friend_add_request.assert_not_awaited()
        approved = await adapter.friend_host.decide("10000", result["data"]["action_id"], True)
        assert approved["state"] == "succeeded"
    adapter._bot.set_friend_add_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_unknown_history_can_be_deleted_before_local_ttl(tmp_path, clock):
    path = tmp_path / "ledger.json"
    first = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    await first.register("friend", "secret", self_id="10000", user_id="321")
    await first.occupy("friend", "secret", self_id="10000")
    second = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    record = (await second.list_requests("friend", self_id="10000", view="archived"))["items"][0]
    assert record["archived_at"] is not None and record["execution_state"] == "unknown"
    await second.delete_request("friend", record["request_id"], self_id="10000", expected_revision=record["revision"])
    with pytest.raises(LookupError):
        await second.occupy("friend", "secret", self_id="10000", allow_archived=True)


@pytest.mark.asyncio
async def test_archived_group_request_uses_revision_and_existing_backend_approval(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: clock[0])
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[{"id": "bot", "type": "onebot", "settings": settings}]))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    backend._attach_request_ledger(adapter)
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.confirm_membership("100", "456", True)
    directory.patch_account("100", expected_revision=1, mode="all", approval_defaults={"handle_group_request": "approval_required"})
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    await adapter.request_flags.register("group", "group-secret", self_id="100", group_id="456", sub_type="add", user_id="123")
    clock[0] = 1700
    item = (await adapter.request_flags.list_requests("group", self_id="100", view="archived"))["items"][0]
    params = {"flag": "group-secret", "sub_type": "add", "approve": True, "request_id": item["request_id"], "expected_revision": item["revision"]}
    record = await backend.submit_group_action("bot", "100", "456", "archived-group", "handle_group_request", params, actor_kind="panel")
    assert record["state"] == "pending" and "group-secret" not in json.dumps(record)
    adapter._bot.set_group_add_request.assert_not_awaited()
    completed = await backend.decide_group_action("bot", "100", "456", "archived-group", approve=True)
    assert completed["state"] == "succeeded"
    adapter._bot.set_group_add_request.assert_awaited_once_with(flag="group-secret", sub_type="add", approve=True, reason="")


@pytest.mark.asyncio
async def test_archive_http_routes_enforce_account_revision_and_keep_no_replay_record(tmp_path, clock):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    attach_friend_host(adapter)
    await incoming(adapter)
    clock[0] = 1700
    server = object.__new__(BackendHTTPServer)
    server.backend = cast(BackendManager, SimpleNamespace(friend_service=lambda adapter_id: adapter.friend_host))
    async def route(method, path, body):
        result = await server._route_friends(method, path, body)
        assert result is not None
        return result
    base = "/api/platforms/ob/friends"
    status, page = await route("GET", base + "/requests?account=10000&view=archived", b"")
    assert status == 200 and page["items"][0]["can_handle"]
    item = page["items"][0]
    status, policy = await route("GET", base + "/request-policy?account=10000", b"")
    assert status == 200 and policy == {"credential_days": 30, "history_days": 90}
    status, _ = await route("POST", base + f"/requests/{item['request_id']}/recheck", json.dumps({"expected_self_id": "other"}).encode())
    assert status == 409
    status, result = await route("POST", base + f"/requests/{item['request_id']}/recheck", json.dumps({"expected_self_id": "10000"}).encode())
    assert status == 200 and result["verification"] == "local_only"
    status, _ = await route("PATCH", base + "/request-policy", json.dumps({"expected_self_id": "10000", "credential_days": 30, "history_days": 1}).encode())
    assert status == 400
    status, result = await route("POST", base + "/actions", json.dumps({"expected_self_id": "10000", "action_id": "archive-http", "action_type": "handle_request", "params": {"request_id": item["request_id"], "approve": True, "expected_revision": item["revision"]}}).encode())
    assert status == 200 and result["state"] == "succeeded"
    status, page = await route("GET", base + "/requests?account=10000&view=archived", b"")
    history = page["items"][0]
    assert not history["can_handle"] and history["decision"] == "accepted"
    status, _ = await route("POST", base + f"/requests/{item['request_id']}/delete", json.dumps({"expected_self_id": "10000", "expected_revision": item["revision"]}).encode())
    assert status == 400
    status, _ = await route("POST", base + f"/requests/{item['request_id']}/delete", json.dumps({"expected_self_id": "10000", "expected_revision": history["revision"]}).encode())
    assert status == 200
    await incoming(adapter)
    assert not (await adapter.friend_requests("10000", 20, None, view="all"))["items"]
    adapter._bot.set_friend_add_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_inbox_schema_migrates_and_preserves_still_usable_credential(tmp_path, clock):
    ledger = RequestApprovalLedger(tmp_path / "ledger.json")
    await ledger.register("ob", "10000", "friend", "legacy-secret", user_id="321")
    path = ledger.inbox_path
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = "rq_" + "a" * 32
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE requests(request_id TEXT PRIMARY KEY,adapter_id TEXT NOT NULL,self_id TEXT NOT NULL,kind TEXT NOT NULL,digest TEXT NOT NULL,flag TEXT NOT NULL,group_id TEXT NOT NULL,sub_type TEXT NOT NULL,user_id TEXT NOT NULL,comment TEXT NOT NULL,received_at REAL NOT NULL,expires_at REAL NOT NULL,UNIQUE(adapter_id,self_id,kind,digest))")
        db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (identity, "ob", "10000", "friend", flag_digest("friend", "10000", "legacy-secret"), "legacy-secret", "", "", "321", "旧申请", 1000.0, 1600.0))
    clock[0] = 1700
    registry = RequestFlagRegistry("ob", ledger)
    item = (await registry.list_requests("friend", self_id="10000", view="archived"))["items"][0]
    assert item["request_id"] == identity and item["can_handle"] and item["comment"] == "旧申请"
    internal = await registry.resolve_request("friend", identity, self_id="10000", expected_revision=item["revision"], allow_archived=True)
    assert internal["flag"] == "legacy-secret" and internal["credential_expires_at"] == 1000.0 + 30 * 86400
