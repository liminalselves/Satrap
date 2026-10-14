"""业务链路场景与集中边界契约"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
import pytest
from satrap.core.call_context import bind_call_origin
from satrap.core.config.group_action_origin import bind_group_action_preflight, current_group_action_preflight
from satrap.core.platform.onebot.request_registry import RequestApprovalLedger, RequestFlagRegistry
from support.administration import _origin, _setup_adapter
from support.requests import attach_friend_host, incoming, restore_manager, tool
import asyncio
import time
from typing import cast
from satrap.core.platform.request_inbox import flag_digest
from satrap.core.backend.BackendManager import BackendManager, BackendConfig
from satrap.core.platform import PlatformAdapterManager, PlatformConfig
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.config.group_directory import GroupDirectoryStore
from unittest.mock import AsyncMock
from types import SimpleNamespace
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.platform import set_current_adapter_manager
from satrap.core.platform.onebot.admin import UnsupportedAdminAction
from satrap.core.friends import FriendError
from satrap.core.friends.service import FriendService
from satrap.core.platform.onebot import request_registry

@pytest.fixture
def _friend_request_occurrences_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    value = [1000.0]
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: value[0])
    return value

async def register(registry: RequestFlagRegistry, event_time: int | None, comment: str = "首次申请") -> dict[str, Any]:
    await registry.register("friend", "reused-user-uid", self_id="10000", user_id="321", event_time=event_time, comment=comment)
    return (await registry.list_requests("friend", self_id="10000", view="all"))["items"][0]

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_new_request_after_archive_survives_restart_and_blocks_old_approval(tmp_path: Path, _friend_request_occurrences_clock: list[float], legacy: bool):
    path = tmp_path / "ledger.json"
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    old = await register(registry, None if legacy else 990)
    _friend_request_occurrences_clock[0] = 1700
    archived = (await registry.list_requests("friend", self_id="10000", view="archived"))["items"][0]
    assert archived["can_handle"] and archived["requires_confirmation"]
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    new = await register(registry, 1690, "第二次申请")
    assert new["request_id"] != old["request_id"] and new["received_at"] == 1700
    assert new["comment"] == "第二次申请" and not new["archived"] and new["can_handle"]
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    active = await registry.list_requests("friend", self_id="10000", owner_user_id="321")
    assert active["total"] == 1 and active["unavailable_count"] == 0
    history = await registry.list_requests("friend", self_id="10000", view="archived")
    assert history["total"] == 1 and not history["items"][0]["can_handle"]
    assert "更新申请" in history["items"][0]["handling_reason"]
    with pytest.raises(LookupError, match="已有更新记录"):
        await registry.resolve_request("friend", old["request_id"], self_id="10000", allow_archived=True)
    assert (await registry.resolve_request("friend", new["request_id"], self_id="10000"))["flag"] == "reused-user-uid"
    assert "reused-user-uid" not in json.dumps(active)
    assert "reused-user-uid" not in path.read_text(encoding="utf-8")

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["completed", "unknown", "expired"])
async def test_duplicate_event_never_revives_but_later_request_is_independent(tmp_path: Path, _friend_request_occurrences_clock: list[float], state: str):
    path = tmp_path / "ledger.json"
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    old = await register(registry, 990)
    row = await registry.resolve_request("friend", old["request_id"], self_id="10000")
    if state != "expired":
        await registry.occupy("friend", row["flag"], self_id="10000", identity_digest=row["digest"])
        await registry.settle("friend", row["flag"], self_id="10000", state=state, identity_digest=row["digest"])
    _friend_request_occurrences_clock[0] = 1700
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    assert not await registry.register("friend", row["flag"], self_id="10000", user_id="321", event_time=990)
    assert (await registry.list_requests("friend", self_id="10000"))["total"] == 0
    repeated = (await registry.list_requests("friend", self_id="10000", view="archived"))["items"][0]
    assert repeated["request_id"] == old["request_id"] and repeated["received_at"] == 1000
    assert repeated["can_handle"] == (state == "expired")
    new = await register(registry, 1690)
    assert new["can_handle"] and new["request_id"] != old["request_id"]
    history = (await registry.list_requests("friend", self_id="10000", view="archived"))["items"][0]
    assert not history["can_handle"]

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_duplicate_active_event_does_not_change_comment_or_expiry(tmp_path: Path, _friend_request_occurrences_clock: list[float]):
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    initial = await register(registry, 990)
    _friend_request_occurrences_clock[0] = 1100
    assert not await registry.register("friend", "reused-user-uid", self_id="10000", user_id="321", event_time=990, comment="重复")
    current = (await registry.list_requests("friend", self_id="10000"))["items"][0]
    assert current["request_id"] == initial["request_id"]
    assert current["comment"] == initial["comment"] and current["expires_at"] == initial["expires_at"]

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_stale_or_undated_replay_cannot_supersede_latest_request(tmp_path: Path, _friend_request_occurrences_clock: list[float], caplog: pytest.LogCaptureFixture):
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    latest = await register(registry, 990)
    _friend_request_occurrences_clock[0] = 1200
    timestamps: list[int | None] = [980, None]
    for timestamp in timestamps:
        assert not await registry.register("friend", "reused-user-uid", self_id="10000", user_id="321", event_time=timestamp)
    page = await registry.list_requests("friend", self_id="10000")
    assert page["total"] == 1 and page["items"][0]["request_id"] == latest["request_id"]
    assert "stale_event" in caplog.text and "missing_event_identity" in caplog.text
    assert "reused-user-uid" not in caplog.text

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_adapter_uses_original_timestamp_and_host_sends_only_raw_credential(tmp_path: Path, _friend_request_occurrences_clock: list[float]):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    await incoming(adapter, flag="reused-user-uid", time=990)
    adapter._bot.set_friend_add_request.return_value = {}
    attach_friend_host(adapter)
    old = (await adapter.friend_requests("10000", 20, None))["items"][0]
    _friend_request_occurrences_clock[0] = 1700
    await incoming(adapter, flag="reused-user-uid", time=1690, comment="新申请")
    active = await adapter.friend_requests("10000", 20, None)
    assert active["total"] == 1
    new = active["items"][0]
    with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
        result = await tool("friend_manager_handle_request").execute(request_id=new["request_id"], approve=True, remark="验收")
    assert result["ok"] and result["data"]["state"] == "succeeded"
    adapter._bot.set_friend_add_request.assert_awaited_once_with(flag="reused-user-uid", approve=True, remark="验收")
    history = await adapter.request_flags.list_requests("friend", self_id="10000", view="archived")
    by_id = {item["request_id"]: item for item in history["items"]}
    assert by_id[new["request_id"]]["decision"] == "accepted" and by_id[old["request_id"]]["decision"] is None

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_queued_old_decision_is_rejected_and_settlement_does_not_clear_new_request(tmp_path: Path, _friend_request_occurrences_clock: list[float], monkeypatch: pytest.MonkeyPatch, asynchronous):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    await incoming(adapter, flag="reused-user-uid", time=990)
    old = (await adapter.friend_requests("10000", 20, None))["items"][0]
    original_call = adapter.admin._call

    async def queued_call(action: str, **params: Any) -> Any:
        await incoming(adapter, flag="reused-user-uid", time=1090, comment="排队期间的新申请")
        return await original_call(action, **params)

    monkeypatch.setattr(adapter.admin, "_call", queued_call)
    _friend_request_occurrences_clock[0] = 1100
    checked: list[bool] = []

    async def async_preflight():
        checked.append(True)

    with bind_group_action_preflight(async_preflight if asynchronous else lambda: checked.append(True)):
        with pytest.raises(PermissionError, match="已有更新记录"):
            await adapter.friend_handle("10000", old["request_id"], True, "")
    adapter._bot.set_friend_add_request.assert_not_awaited()
    assert checked == [True] and current_group_action_preflight() is None
    active = await adapter.friend_requests("10000", 20, None)
    assert active["total"] == 1 and active["items"][0]["comment"] == "排队期间的新申请"
    row = await adapter.request_flags.resolve_request("friend", active["items"][0]["request_id"], self_id="10000")
    assert row["flag"] == "reused-user-uid"
    history = (await adapter.request_flags.list_requests("friend", self_id="10000", view="archived"))["items"][0]
    assert history["decision"] is None and not history["can_handle"]

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_old_settlement_during_new_request_keeps_new_credential(tmp_path: Path, _friend_request_occurrences_clock: list[float]):
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    old = await register(registry, 990)
    row = await registry.resolve_request("friend", old["request_id"], self_id="10000")
    await registry.occupy("friend", row["flag"], self_id="10000", identity_digest=row["digest"])
    _friend_request_occurrences_clock[0] = 1100
    new = await register(registry, 1090)
    await registry.settle("friend", row["flag"], self_id="10000", state="completed", decision="accepted", identity_digest=row["digest"])
    current = await registry.resolve_request("friend", new["request_id"], self_id="10000")
    assert current["flag"] == row["flag"] and current["decision"] is None
    assert (await registry.list_requests("friend", self_id="10000"))["total"] == 1

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_pending_archived_approval_cannot_handle_later_same_person_request(tmp_path: Path, _friend_request_occurrences_clock: list[float]):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    await incoming(adapter, flag="reused-user-uid", time=990)
    _friend_request_occurrences_clock[0] = 1700
    with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
        old = (await tool("friend_manager_list_requests").execute(view="archived"))["data"]["items"][0]
        pending = await tool("friend_manager_handle_request").execute(
            request_id=old["request_id"], expected_revision=old["revision"], approve=True,
        )
        assert pending["ok"] and pending["data"]["state"] == "pending"
        _friend_request_occurrences_clock[0] = 1800
        await incoming(adapter, flag="reused-user-uid", time=1790, comment="不能由旧审批处理")
        host = adapter.friend_host
        assert host is not None
        result = await host.decide("10000", pending["data"]["action_id"], True)
    assert result["state"] == "failed"
    adapter._bot.set_friend_add_request.assert_not_awaited()
    current = await adapter.friend_requests("10000", 20, None)
    assert current["total"] == 1 and current["items"][0]["comment"] == "不能由旧审批处理"

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_duplicate_event_repairs_missing_occurrence_details_without_renewal(tmp_path: Path, _friend_request_occurrences_clock: list[float]):
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    old = await register(registry, 990)
    assert registry.inbox.path is not None
    with sqlite3.connect(registry.inbox.path) as connection:
        connection.execute("DELETE FROM requests")
    _friend_request_occurrences_clock[0] = 1700
    assert not await registry.register("friend", "reused-user-uid", self_id="10000", user_id="321", event_time=990, comment="修复详情")
    page = await registry.list_requests("friend", self_id="10000", view="archived")
    assert page["total"] == 1 and page["unavailable_count"] == 0
    repaired = page["items"][0]
    assert repaired["received_at"] == old["received_at"] and repaired["expires_at"] == old["expires_at"]
    assert repaired["comment"] == "修复详情" and repaired["requires_confirmation"]

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_reused_credential_cannot_move_to_another_applicant(tmp_path: Path, _friend_request_occurrences_clock: list[float], caplog: pytest.LogCaptureFixture):
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    original = await register(registry, 990)
    assert not await registry.register("friend", "reused-user-uid", self_id="10000", user_id="999", event_time=1090)
    row = await registry.resolve_request("friend", original["request_id"], self_id="10000")
    with pytest.raises(LookupError, match="处理凭据不符"):
        await registry.occupy("friend", "another-credential", self_id="10000", identity_digest=row["digest"])
    assert (await registry.list_requests("friend", self_id="10000"))["total"] == 1
    assert "conflict" in caplog.text and "reused-user-uid" not in caplog.text

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("event_time", True), ("event_time", 0), ("credential_digest", None), ("credential_digest", "0" * 32)])
async def test_corrupt_persisted_occurrence_identity_disables_processing(tmp_path: Path, _friend_request_occurrences_clock: list[float], field: str, value: object):
    path = tmp_path / "ledger.json"
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    item = await register(registry, 990)
    data = json.loads(path.read_text(encoding="utf-8"))
    next(iter(data["entries"].values()))[field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    restarted = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    assert restarted.ledger.degraded
    with pytest.raises(LookupError, match="账本不可用"):
        await restarted.resolve_request("friend", item["request_id"], self_id="10000")

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("timestamp", [None, True, 0, -1, "990", 990.0])
async def test_invalid_original_timestamp_logs_and_never_creates_fresh_identity(tmp_path: Path, _friend_request_occurrences_clock: list[float], caplog: pytest.LogCaptureFixture, timestamp: object):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    await incoming(adapter, flag="reused-user-uid", time=timestamp)
    initial = (await adapter.friend_requests("10000", 20, None))["items"][0]
    _friend_request_occurrences_clock[0] = 1700
    await incoming(adapter, flag="reused-user-uid", time=timestamp)
    assert not (await adapter.friend_requests("10000", 20, None))["items"]
    history = (await adapter.request_flags.list_requests("friend", self_id="10000", view="archived"))["items"][0]
    assert history["request_id"] == initial["request_id"] and history["received_at"] == 1000
    assert "缺少有效原始时间" in caplog.text and "reused-user-uid" not in caplog.text

@pytest.fixture
def _request_archive_clock(monkeypatch):
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

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["friend", "group"])
async def test_repeated_expired_event_restores_missing_details_without_resetting_ledger(tmp_path, _request_archive_clock, kind):
    registry, initial = await archived_registry(tmp_path, _request_archive_clock, kind)
    registry.inbox.delete("ob", "10000", kind, initial["request_id"], initial["revision"], _request_archive_clock[0])
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

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_repeated_recent_event_restores_missing_details_in_same_process(tmp_path, _request_archive_clock):
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    await registry.register("friend", "secret", self_id="10000", user_id="321")
    inbox_path = registry.ledger.inbox_path
    assert inbox_path is not None
    with sqlite3.connect(inbox_path) as connection:
        connection.execute("DELETE FROM requests")
    _request_archive_clock[0] = 1100
    assert not await registry.register("friend", "secret", self_id="10000", user_id="321", comment="补回详情")
    page = await registry.list_requests("friend", self_id="10000")
    assert page["unavailable_count"] == 0 and page["items"][0]["comment"] == "补回详情"
    assert page["items"][0]["received_at"] == 1000 and not page["items"][0]["archived"]
    assert await registry.register("friend", "new-flag", self_id="10000", user_id="321")
    assert (await registry.list_requests("friend", self_id="10000"))["total"] == 2

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["friend", "group"])
async def test_archive_survives_restart_and_requires_explicit_confirmation(tmp_path, _request_archive_clock, kind):
    registry, item = await archived_registry(tmp_path, _request_archive_clock, kind)
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

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("state,decision", [("completed", "accepted"), ("completed", "rejected"), ("unknown", None)])
async def test_archived_request_single_winner_and_terminal_history_cannot_replay(tmp_path, _request_archive_clock, state, decision):
    registry, item = await archived_registry(tmp_path, _request_archive_clock)
    calls = await asyncio.gather(*(registry.occupy("friend", "secret", self_id="10000", allow_archived=True) for _ in range(2)), return_exceptions=True)
    assert sum(not isinstance(result, Exception) for result in calls) == 1
    await registry.settle("friend", "secret", self_id="10000", state=state, decision=decision)
    history = (await registry.list_requests("friend", self_id="10000", view="archived"))["items"][0]
    assert not history["can_handle"] and history["decision"] == decision
    assert history["execution_state"] == ("unknown" if state == "unknown" else "succeeded")
    assert registry.inbox.resolve("ob", "10000", "friend", item["request_id"], _request_archive_clock[0])["flag"] == ""
    await registry.delete_request("friend", history["request_id"], self_id="10000", expected_revision=history["revision"])
    await registry.register("friend", "secret", self_id="10000", user_id="321")
    assert not (await registry.list_requests("friend", self_id="10000", view="all"))["items"]
    with pytest.raises(LookupError):
        await registry.occupy("friend", "secret", self_id="10000", allow_archived=True)

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_retention_clears_credential_then_history_without_resetting_ledger(tmp_path, _request_archive_clock):
    registry, item = await archived_registry(tmp_path, _request_archive_clock)
    registry.inbox.policy("ob", "10000", {"credential_days": 1, "history_days": 2})
    assert registry.inbox.policy("other", "10000") == {"credential_days": 30, "history_days": 90}
    _request_archive_clock[0] = 1000 + 86400 + 1
    history = (await registry.list_requests("friend", self_id="10000", view="archived"))["items"][0]
    assert not history["can_handle"]
    registry.inbox.policy("ob", "10000", {"credential_days": 2, "history_days": 2})
    assert registry.inbox.resolve("ob", "10000", "friend", item["request_id"], _request_archive_clock[0])["flag"] == ""
    _request_archive_clock[0] = 1000 + 2 * 86400 + 1
    assert not (await registry.list_requests("friend", self_id="10000", view="all"))["items"]
    await registry.register("friend", "secret", self_id="10000", user_id="321")
    assert not (await registry.list_requests("friend", self_id="10000", view="all"))["items"]

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_panel_can_process_archived_friend_request_with_current_revision(tmp_path, _request_archive_clock, approve):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    attach_friend_host(adapter)
    await incoming(adapter)
    _request_archive_clock[0] = 1700
    item = (await adapter.friend_requests("10000", 20, None, view="archived"))["items"][0]
    result = await cast(FriendService, adapter.friend_host).submit("10000", "archive-panel", "handle_request", {"request_id": item["request_id"], "expected_revision": item["revision"], "approve": approve}, actor="panel")
    assert result["state"] == "succeeded"
    adapter._bot.set_friend_add_request.assert_awaited_once_with(flag="private-flag", approve=approve, remark="")

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_model_archived_request_waits_for_approval_then_executes_once(tmp_path, _request_archive_clock):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    await incoming(adapter)
    _request_archive_clock[0] = 1700
    with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
        item = (await tool("friend_manager_list_requests").execute(view="archived"))["data"]["items"][0]
        handle = tool("friend_manager_handle_request")
        result = await handle.execute(request_id=item["request_id"], expected_revision=item["revision"], approve=True)
        assert result["ok"] and result["data"]["state"] == "pending"
        adapter._bot.set_friend_add_request.assert_not_awaited()
        approved = await cast(FriendService, adapter.friend_host).decide("10000", result["data"]["action_id"], True)
        assert approved["state"] == "succeeded"
    adapter._bot.set_friend_add_request.assert_awaited_once()

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_restart_unknown_history_can_be_deleted_before_local_ttl(tmp_path, _request_archive_clock):
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

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_archived_group_request_uses_revision_and_existing_backend_approval(tmp_path, _request_archive_clock, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: _request_archive_clock[0])
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
    _request_archive_clock[0] = 1700
    item = (await adapter.request_flags.list_requests("group", self_id="100", view="archived"))["items"][0]
    params = {"flag": "group-secret", "sub_type": "add", "approve": True, "request_id": item["request_id"], "expected_revision": item["revision"]}
    record = await backend.submit_group_action("bot", "100", "456", "archived-group", "handle_group_request", params, actor_kind="panel")
    assert record["state"] == "pending" and "group-secret" not in json.dumps(record)
    adapter._bot.set_group_add_request.assert_not_awaited()
    completed = await backend.decide_group_action("bot", "100", "456", "archived-group", approve=True)
    assert completed["state"] == "succeeded"
    adapter._bot.set_group_add_request.assert_awaited_once_with(flag="group-secret", sub_type="add", approve=True, reason="")

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_archive_http_routes_enforce_account_revision_and_keep_no_replay_record(tmp_path, _request_archive_clock):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    attach_friend_host(adapter)
    await incoming(adapter)
    _request_archive_clock[0] = 1700
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

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_legacy_inbox_schema_migrates_and_preserves_still_usable_credential(tmp_path, _request_archive_clock):
    ledger = RequestApprovalLedger(tmp_path / "ledger.json")
    await ledger.register("ob", "10000", "friend", "legacy-secret", user_id="321")
    path = ledger.inbox_path
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = "rq_" + "a" * 32
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE requests(request_id TEXT PRIMARY KEY,adapter_id TEXT NOT NULL,self_id TEXT NOT NULL,kind TEXT NOT NULL,digest TEXT NOT NULL,flag TEXT NOT NULL,group_id TEXT NOT NULL,sub_type TEXT NOT NULL,user_id TEXT NOT NULL,comment TEXT NOT NULL,received_at REAL NOT NULL,expires_at REAL NOT NULL,UNIQUE(adapter_id,self_id,kind,digest))")
        db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (identity, "ob", "10000", "friend", flag_digest("friend", "10000", "legacy-secret"), "legacy-secret", "", "", "321", "旧申请", 1000.0, 1600.0))
    _request_archive_clock[0] = 1700
    registry = RequestFlagRegistry("ob", ledger)
    item = (await registry.list_requests("friend", self_id="10000", view="archived"))["items"][0]
    assert item["request_id"] == identity and item["can_handle"] and item["comment"] == "旧申请"
    internal = await registry.resolve_request("friend", identity, self_id="10000", expected_revision=item["revision"], allow_archived=True)
    assert internal["flag"] == "legacy-secret" and internal["credential_expires_at"] == 1000.0 + 30 * 86400

@pytest.fixture
def adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(FriendService, "_test_path", tmp_path / "friends.db", raising=False)
    item = _setup_adapter()
    item.bot_self_id = "10000"
    item.client_self_id = "10000"
    item.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    attach_friend_host(item)
    item._bot.get_doubt_friends_add_request.return_value = []
    item._bot.set_doubt_friends_add_request.return_value = {}
    yield item
    set_current_adapter_manager(None)

def suspicious(user_id=321, stamp=1791638000, **extra):
    return {"uid": "private-doubt-credential", "user_id": user_id, "nick": "测试好友", "source": "账号查找",
            "reason": "平台风险提示", "msg": "我是测试好友", "reqTime": stamp, **extra}

@pytest.mark.asyncio
async def test_mixed_query_filters_metadata_and_no_credential_disclosure(adapter, caplog):
    await incoming(adapter, flag="ordinary", time=1791637900)
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious()]
    with bind_call_origin(_origin()):
        result = await tool("friend_manager_list_requests").execute()
        assert result["ok"]
        rows = result["data"]["items"]
        assert {row["request_category"] for row in rows} == {"normal", "suspicious"}
        row = next(row for row in rows if row["request_category"] == "suspicious")
        assert row["nickname"] == "测试好友" and row["suspicious_reason"] == "平台风险提示"
        assert row["requested_at"] == 1791638000 and row["request_source"] == "账号查找"
        only = await tool("friend_manager_list_requests").execute(request_category="suspicious")
        assert len(only["data"]["items"]) == 1
        normal = await tool("friend_manager_list_requests").execute(request_category="normal")
        assert len(normal["data"]["items"]) == 1
    assert "private-doubt-credential" not in json.dumps(result) + caplog.text
    assert result["data"]["coverage"]["suspicious"]["complete"] is False

@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_handle_chooses_platform_suspicious_action_and_prevents_replay(adapter, approve):
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious()]
    page = await adapter.friend_requests("10000", 20, None)
    row = page["items"][0]
    await adapter.friend_handle("10000", row["request_id"], approve, "", expected_revision=row["revision"])
    adapter._bot.set_doubt_friends_add_request.assert_awaited_once_with(flag="private-doubt-credential", approve=approve)
    adapter._bot.set_friend_add_request.assert_not_awaited()
    with pytest.raises(LookupError):
        await adapter.friend_handle("10000", row["request_id"], approve, "")

@pytest.mark.asyncio
async def test_repeat_fetch_keeps_deadline_revision_and_new_occurrence_separate(adapter):
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious()]
    first = (await adapter.friend_requests("10000", 20, None))["items"][0]
    repeat = (await adapter.friend_requests("10000", 20, None))["items"][0]
    assert (repeat["request_id"], repeat["expires_at"], repeat["revision"]) == (first["request_id"], first["expires_at"], first["revision"])
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious(stamp=1791638100)]
    latest = (await adapter.friend_requests("10000", 20, None))["items"][0]
    assert latest["request_id"] != first["request_id"]
    old = (await adapter.friend_requests("10000", 20, None, view="archived"))["items"][0]
    assert old["request_id"] == first["request_id"] and not old["can_handle"]

@pytest.mark.asyncio
async def test_remark_and_missing_platform_evidence_do_not_consume_request(adapter):
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious()]
    row = (await adapter.friend_requests("10000", 20, None))["items"][0]
    with pytest.raises(FriendError, match="不支持设置备注"):
        await adapter.friend_handle("10000", row["request_id"], True, "备注")
    adapter._bot.get_doubt_friends_add_request.return_value = []
    checked = await adapter.friend_recheck_request("10000", row["request_id"])
    assert checked["verification"] == "not_confirmed" and checked["platform_state"] != "processed"
    with pytest.raises(FriendError, match="未确认"):
        await adapter.friend_handle("10000", row["request_id"], True, "")
    adapter._bot.set_doubt_friends_add_request.assert_not_awaited()
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious()]
    await adapter.friend_handle("10000", row["request_id"], True, "")

@pytest.mark.asyncio
async def test_unsupported_query_preserves_normal_results_and_reports_coverage(adapter, monkeypatch):
    await incoming(adapter)
    original = adapter.admin._call

    async def unsupported(action, **params):
        if action == "get_doubt_friends_add_request":
            raise UnsupportedAdminAction("not implemented")
        return await original(action, **params)

    monkeypatch.setattr(adapter.admin, "_call", unsupported)
    page = await adapter.friend_requests("10000", 20, None)
    assert len(page["items"]) == 1 and page["coverage"]["suspicious"]["state"] == "unsupported"
    with pytest.raises(FriendError) as error:
        await adapter.friend_requests("10000", 20, None, request_category="suspicious")
    assert error.value.code == "unsupported"

@pytest.mark.asyncio
async def test_self_scope_and_unknown_applicant_not_granted_own_permissions(adapter):
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious(321), suspicious(999, uid="other-uid"), suspicious(0, uid="unknown-uid"), suspicious(uid="")]
    with bind_call_origin(_origin(actor="321", chat_type="GroupMessage")):
        result = await tool("friend_manager_list_requests").execute()
        assert result["ok"] and result["data"]["scope"] == "self"
        assert [row["user_id"] for row in result["data"]["items"]] == ["321"]
        assert "unavailable_count" not in result["data"]["coverage"]["suspicious"]
        assert "unavailable_reasons" not in result["data"]["coverage"]["suspicious"]
        own_id = result["data"]["items"][0]["request_id"]
        checked = await tool("friend_manager_recheck_request").execute(request_id=own_id)
        assert checked["ok"] and checked["data"]["verification"] == "platform_pending"
        assert "unavailable_count" not in checked["data"]["coverage"]
        assert "unavailable_reasons" not in checked["data"]["coverage"]
    with bind_call_origin(_origin()):
        all_rows = (await tool("friend_manager_list_requests").execute())["data"]["items"]
        other = next(row for row in all_rows if row["user_id"] == "999")
        checked = await tool("friend_manager_recheck_request").execute(request_id=own_id)
        assert checked["data"]["coverage"]["unavailable_reasons"][0]["code"] == "invalid_credential"
    with bind_call_origin(_origin(actor="321")):
        denied = await tool("friend_manager_handle_request").execute(request_id=other["request_id"], approve=True)
        assert not denied["ok"] and denied["error"]["code"] == "permission_denied"
    adapter._bot.set_doubt_friends_add_request.assert_not_awaited()

@pytest.mark.asyncio
async def test_category_change_invalidates_old_revision_before_write(adapter):
    await incoming(adapter, flag="private-doubt-credential", time=1791638000)
    old = (await adapter.friend_requests("10000", 20, None, request_category="normal"))["items"][0]
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious()]
    page = await adapter.friend_requests("10000", 20, None)
    changed = page["items"][0]
    assert changed["request_id"] == old["request_id"] and changed["revision"] > old["revision"]
    assert changed["request_category"] == "suspicious"
    with pytest.raises(ValueError, match="已变化"):
        await adapter.friend_handle("10000", old["request_id"], True, "", expected_revision=old["revision"])
    adapter._bot.set_friend_add_request.assert_not_awaited()
    adapter._bot.set_doubt_friends_add_request.assert_not_awaited()

@pytest.mark.asyncio
async def test_protocol_query_bounded_invalid_items_do_not_leak_or_crash(adapter):
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious(uid=""), None, suspicious()] * 70
    page = await adapter.friend_requests("10000", 20, None)
    proof = page["coverage"]["suspicious"]
    assert proof["truncated"] and proof["unavailable_count"] > 0
    assert proof["unavailable_count"] == sum(item["count"] for item in proof["unavailable_reasons"])
    assert {item["code"] for item in proof["unavailable_reasons"]} == {"invalid_credential", "invalid_item"}
    assert len(page["items"]) == 1
    adapter._bot.get_doubt_friends_add_request.assert_awaited_once_with(count=200)

@pytest.mark.asyncio
@pytest.mark.parametrize("raw, code, message", [
    (suspicious(uid=""), "invalid_credential", "缺少有效处理凭据"),
    (None, "invalid_item", "条目格式无效"),
    (suspicious(user_id=True), "invalid_applicant", "账号格式无效"),
    ({**suspicious(), "user_id": "bad-account"}, "invalid_applicant", "账号格式无效"),
    (suspicious(msg=["not-text"]), "invalid_details", "详情格式无效"),
])
async def test_item_failure_reason_is_separate_from_query_coverage(adapter, raw, code, message, caplog):
    adapter._bot.get_doubt_friends_add_request.return_value = [raw]
    with bind_call_origin(_origin()):
        result = await tool("friend_manager_list_requests").execute(request_category="suspicious")
    assert result["ok"] and result["data"]["items"] == []
    proof = result["data"]["coverage"]["suspicious"]
    assert proof["state"] == "queried" and proof["complete"] is False
    assert "查询覆盖范围" in proof["reason"]
    assert proof["unavailable_count"] == 1
    assert len(proof["unavailable_reasons"]) == 1
    reason = proof["unavailable_reasons"][0]
    assert reason["code"] == code and reason["count"] == 1 and message in reason["message"]
    assert "private-doubt-credential" not in json.dumps(result) + caplog.text
    adapter._bot.set_doubt_friends_add_request.assert_not_awaited()

@pytest.mark.asyncio
async def test_empty_query_does_not_imply_an_unreadable_request(adapter):
    page = await adapter.friend_requests("10000", 20, None)
    proof = page["coverage"]["suspicious"]
    assert proof["complete"] is False and page["items"] == []
    assert proof["unavailable_count"] == 0 and proof["unavailable_reasons"] == []

@pytest.mark.asyncio
async def test_registration_failure_reports_safe_reason(adapter, monkeypatch, caplog):
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious()]
    monkeypatch.setattr(adapter.request_flags.ledger, "lookup", lambda *args, **kwargs: None)
    page = await adapter.friend_requests("10000", 20, None)
    proof = page["coverage"]["suspicious"]
    assert proof["unavailable_count"] == 1
    assert proof["unavailable_reasons"][0]["code"] == "registration_unconfirmed"
    assert "private-doubt-credential" not in json.dumps(page) + caplog.text

@pytest.mark.asyncio
async def test_archived_suspicious_request_survives_restart_and_can_be_confirmed(adapter, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(request_registry, "time", lambda: clock[0])
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious(stamp=990)]
    first = (await adapter.friend_requests("10000", 20, None))["items"][0]
    path = adapter.request_flags.ledger._path
    clock[0] = 1700
    adapter.set_request_ledger(RequestApprovalLedger(path))
    row = (await adapter.friend_requests("10000", 20, None, view="archived"))["items"][0]
    assert row["request_id"] == first["request_id"] and row["received_at"] == 1000
    assert row["request_category"] == "suspicious" and row["requires_confirmation"]
    checked = await adapter.friend_recheck_request("10000", row["request_id"])
    assert checked["verification"] == "platform_pending"
    await adapter.friend_handle("10000", row["request_id"], True, "", expected_revision=checked["revision"], allow_archived=True)
    adapter._bot.set_doubt_friends_add_request.assert_awaited_once()

@pytest.mark.asyncio
async def test_ordinary_user_handles_own_suspicious_request_in_group(adapter):
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious()]
    with bind_call_origin(_origin(actor="321")):
        row = (await tool("friend_manager_list_requests").execute())["data"]["items"][0]
        result = await tool("friend_manager_handle_request").execute(request_id=row["request_id"], approve=False)
    assert result["ok"] and result["data"]["state"] == "succeeded"
    adapter._bot.set_doubt_friends_add_request.assert_awaited_once_with(flag="private-doubt-credential", approve=False)

@pytest.mark.asyncio
async def test_undated_cross_source_request_is_not_merged_by_account_or_flag_alone(adapter):
    await incoming(adapter, flag="private-doubt-credential")
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious(stamp=0)]
    page = await adapter.friend_requests("10000", 20, None)
    assert page["items"][0]["request_category"] == "normal"
    assert page["coverage"]["suspicious"]["unavailable_count"] == 1
    assert page["coverage"]["suspicious"]["unavailable_reasons"][0]["code"] == "ambiguous_identity"

@pytest.mark.asyncio
async def test_undated_suspicious_duplicate_does_not_refresh_identity(adapter):
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious(stamp=0)]
    first = (await adapter.friend_requests("10000", 20, None))["items"][0]
    second = (await adapter.friend_requests("10000", 20, None))["items"][0]
    assert first["requested_at"] is None
    assert (first["request_id"], first["revision"], first["expires_at"]) == (second["request_id"], second["revision"], second["expires_at"])

@pytest.mark.asyncio
async def test_request_cursor_is_bound_to_category_and_preserves_query_evidence(adapter):
    await incoming(adapter, flag="one")
    await incoming(adapter, flag="two")
    adapter._bot.get_doubt_friends_add_request.return_value = [suspicious(uid="")]
    host = adapter.friend_host
    page = await host.requests("10000", limit=1, actor="123")
    assert page["has_more"]
    assert page["coverage"]["suspicious"]["unavailable_reasons"][0]["code"] == "invalid_credential"
    next_page = await host.requests("10000", limit=1, actor="123", cursor=page["next_cursor"])
    assert next_page["coverage"] == page["coverage"]
    assert adapter._bot.get_doubt_friends_add_request.await_count == 1
    with pytest.raises(FriendError) as error:
        await host.requests("10000", limit=1, actor="123", cursor=page["next_cursor"], request_category="normal")
    assert error.value.code == "cursor_expired"

@pytest.mark.asyncio
async def test_route_revision_changes_while_queued_prevent_protocol_write(adapter, monkeypatch):
    await incoming(adapter, flag="private-doubt-credential", time=1791638000)
    row = (await adapter.friend_requests("10000", 20, None, request_category="normal"))["items"][0]
    original = adapter.admin._call

    async def queued(action, **params):
        if action == "set_friend_add_request":
            await adapter.request_flags.register("friend", "private-doubt-credential", self_id="10000", user_id="321", event_time=1791638000,
                                                details={"request_category": "suspicious"})
        return await original(action, **params)

    monkeypatch.setattr(adapter.admin, "_call", queued)
    with pytest.raises(PermissionError, match="发送前已变化"):
        await adapter.friend_handle("10000", row["request_id"], True, "")
    adapter._bot.set_friend_add_request.assert_not_awaited()
    adapter._bot.set_doubt_friends_add_request.assert_not_awaited()
