"""平台复用好友申请凭据时, 新事件与旧审批保持独立"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from satrap.core.call_context import bind_call_origin
from satrap.core.config.group_action_origin import bind_group_action_preflight, current_group_action_preflight
from satrap.core.platform.onebot.request_registry import RequestApprovalLedger, RequestFlagRegistry
from unit.test_group_admin_plugin import _origin, _setup_adapter
from unit.test_request_inbox import attach_friend_host, incoming, restore_manager, tool


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    value = [1000.0]
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: value[0])
    return value


async def register(registry: RequestFlagRegistry, event_time: int | None, comment: str = "首次申请") -> dict[str, Any]:
    await registry.register("friend", "reused-user-uid", self_id="10000", user_id="321", event_time=event_time, comment=comment)
    return (await registry.list_requests("friend", self_id="10000", view="all"))["items"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_new_request_after_archive_survives_restart_and_blocks_old_approval(tmp_path: Path, clock: list[float], legacy: bool):
    path = tmp_path / "ledger.json"
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    old = await register(registry, None if legacy else 990)
    clock[0] = 1700
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


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["completed", "unknown", "expired"])
async def test_duplicate_event_never_revives_but_later_request_is_independent(tmp_path: Path, clock: list[float], state: str):
    path = tmp_path / "ledger.json"
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    old = await register(registry, 990)
    row = await registry.resolve_request("friend", old["request_id"], self_id="10000")
    if state != "expired":
        await registry.occupy("friend", row["flag"], self_id="10000", identity_digest=row["digest"])
        await registry.settle("friend", row["flag"], self_id="10000", state=state, identity_digest=row["digest"])
    clock[0] = 1700
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


@pytest.mark.asyncio
async def test_duplicate_active_event_does_not_change_comment_or_expiry(tmp_path: Path, clock: list[float]):
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    initial = await register(registry, 990)
    clock[0] = 1100
    assert not await registry.register("friend", "reused-user-uid", self_id="10000", user_id="321", event_time=990, comment="重复")
    current = (await registry.list_requests("friend", self_id="10000"))["items"][0]
    assert current["request_id"] == initial["request_id"]
    assert current["comment"] == initial["comment"] and current["expires_at"] == initial["expires_at"]


@pytest.mark.asyncio
async def test_stale_or_undated_replay_cannot_supersede_latest_request(tmp_path: Path, clock: list[float], caplog: pytest.LogCaptureFixture):
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    latest = await register(registry, 990)
    clock[0] = 1200
    timestamps: list[int | None] = [980, None]
    for timestamp in timestamps:
        assert not await registry.register("friend", "reused-user-uid", self_id="10000", user_id="321", event_time=timestamp)
    page = await registry.list_requests("friend", self_id="10000")
    assert page["total"] == 1 and page["items"][0]["request_id"] == latest["request_id"]
    assert "stale_event" in caplog.text and "missing_event_identity" in caplog.text
    assert "reused-user-uid" not in caplog.text


@pytest.mark.asyncio
async def test_adapter_uses_original_timestamp_and_host_sends_only_raw_credential(tmp_path: Path, clock: list[float]):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    await incoming(adapter, flag="reused-user-uid", time=990)
    adapter._bot.set_friend_add_request.return_value = {}
    attach_friend_host(adapter)
    old = (await adapter.friend_requests("10000", 20, None))["items"][0]
    clock[0] = 1700
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


@pytest.mark.asyncio
async def test_queued_old_decision_is_rejected_and_settlement_does_not_clear_new_request(tmp_path: Path, clock: list[float], monkeypatch: pytest.MonkeyPatch):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    await incoming(adapter, flag="reused-user-uid", time=990)
    old = (await adapter.friend_requests("10000", 20, None))["items"][0]
    original_call = adapter.admin._call

    async def queued_call(action: str, **params: Any) -> Any:
        await incoming(adapter, flag="reused-user-uid", time=1090, comment="排队期间的新申请")
        return await original_call(action, **params)

    monkeypatch.setattr(adapter.admin, "_call", queued_call)
    clock[0] = 1100
    checked: list[bool] = []
    with bind_group_action_preflight(lambda: checked.append(True)):
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


@pytest.mark.asyncio
async def test_old_settlement_during_new_request_keeps_new_credential(tmp_path: Path, clock: list[float]):
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    old = await register(registry, 990)
    row = await registry.resolve_request("friend", old["request_id"], self_id="10000")
    await registry.occupy("friend", row["flag"], self_id="10000", identity_digest=row["digest"])
    clock[0] = 1100
    new = await register(registry, 1090)
    await registry.settle("friend", row["flag"], self_id="10000", state="completed", decision="accepted", identity_digest=row["digest"])
    current = await registry.resolve_request("friend", new["request_id"], self_id="10000")
    assert current["flag"] == row["flag"] and current["decision"] is None
    assert (await registry.list_requests("friend", self_id="10000"))["total"] == 1


@pytest.mark.asyncio
async def test_pending_archived_approval_cannot_handle_later_same_person_request(tmp_path: Path, clock: list[float]):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    await incoming(adapter, flag="reused-user-uid", time=990)
    clock[0] = 1700
    with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
        old = (await tool("friend_manager_list_requests").execute(view="archived"))["data"]["items"][0]
        pending = await tool("friend_manager_handle_request").execute(
            request_id=old["request_id"], expected_revision=old["revision"], approve=True,
        )
        assert pending["ok"] and pending["data"]["state"] == "pending"
        clock[0] = 1800
        await incoming(adapter, flag="reused-user-uid", time=1790, comment="不能由旧审批处理")
        host = adapter.friend_host
        assert host is not None
        result = await host.decide("10000", pending["data"]["action_id"], True)
    assert result["state"] == "failed"
    adapter._bot.set_friend_add_request.assert_not_awaited()
    current = await adapter.friend_requests("10000", 20, None)
    assert current["total"] == 1 and current["items"][0]["comment"] == "不能由旧审批处理"


@pytest.mark.asyncio
async def test_duplicate_event_repairs_missing_occurrence_details_without_renewal(tmp_path: Path, clock: list[float]):
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    old = await register(registry, 990)
    assert registry.inbox.path is not None
    with sqlite3.connect(registry.inbox.path) as connection:
        connection.execute("DELETE FROM requests")
    clock[0] = 1700
    assert not await registry.register("friend", "reused-user-uid", self_id="10000", user_id="321", event_time=990, comment="修复详情")
    page = await registry.list_requests("friend", self_id="10000", view="archived")
    assert page["total"] == 1 and page["unavailable_count"] == 0
    repaired = page["items"][0]
    assert repaired["received_at"] == old["received_at"] and repaired["expires_at"] == old["expires_at"]
    assert repaired["comment"] == "修复详情" and repaired["requires_confirmation"]


@pytest.mark.asyncio
async def test_reused_credential_cannot_move_to_another_applicant(tmp_path: Path, clock: list[float], caplog: pytest.LogCaptureFixture):
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    original = await register(registry, 990)
    assert not await registry.register("friend", "reused-user-uid", self_id="10000", user_id="999", event_time=1090)
    row = await registry.resolve_request("friend", original["request_id"], self_id="10000")
    with pytest.raises(LookupError, match="处理凭据不符"):
        await registry.occupy("friend", "another-credential", self_id="10000", identity_digest=row["digest"])
    assert (await registry.list_requests("friend", self_id="10000"))["total"] == 1
    assert "conflict" in caplog.text and "reused-user-uid" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("event_time", True), ("event_time", 0), ("credential_digest", None), ("credential_digest", "0" * 32)])
async def test_corrupt_persisted_occurrence_identity_disables_processing(tmp_path: Path, clock: list[float], field: str, value: object):
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


@pytest.mark.asyncio
@pytest.mark.parametrize("timestamp", [None, True, 0, -1, "990", 990.0])
async def test_invalid_original_timestamp_logs_and_never_creates_fresh_identity(tmp_path: Path, clock: list[float], caplog: pytest.LogCaptureFixture, timestamp: object):
    adapter = _setup_adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    await incoming(adapter, flag="reused-user-uid", time=timestamp)
    initial = (await adapter.friend_requests("10000", 20, None))["items"][0]
    clock[0] = 1700
    await incoming(adapter, flag="reused-user-uid", time=timestamp)
    assert not (await adapter.friend_requests("10000", 20, None))["items"]
    history = (await adapter.request_flags.list_requests("friend", self_id="10000", view="archived"))["items"][0]
    assert history["request_id"] == initial["request_id"] and history["received_at"] == 1000
    assert "缺少有效原始时间" in caplog.text and "reused-user-uid" not in caplog.text
