"""验证逐群审批的持久占用、幂等与请求目标边界"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
import sqlite3

import pytest

from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.config.group_actions import GroupActionStore
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.config.group_store import GroupConfigConflict
from satrap.core.platform import PlatformAdapterManager, PlatformConfig
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.onebot.admin import AdminActionUnconfirmed
from satrap.core.platform.onebot.group_action_types import normalize_action_params
from satrap.core.platform.receipt import SendReceipt


def test_pending_decision_is_atomic_and_cannot_replay(tmp_path: Path) -> None:
    store = GroupActionStore(tmp_path / "platform.db")
    params = {"user_id": "42", "reject_add_request": False}
    record, created = store.submit(
        "action-0001", "10000", "456", "kick_group_member", params,
        "panel", 7, approval_required=True,
    )
    assert created and record["state"] == "pending"
    duplicate, created = store.submit(
        "action-0001", "10000", "456", "kick_group_member", params,
        "panel", 7, approval_required=True,
    )
    assert not created and duplicate == record
    with pytest.raises(GroupConfigConflict):
        store.submit("action-0001", "10000", "456", "kick_group_member",
                     {"user_id": "43"}, "panel", 7, approval_required=True)
    occupied = store.decide("10000", "456", "action-0001", approve=True, policy_revision=7)
    assert occupied["state"] == "executing"
    with pytest.raises(GroupConfigConflict):
        store.decide("10000", "456", "action-0001", approve=True, policy_revision=7)
    settled = store.settle("10000", "456", "action-0001", "succeeded", "platform_confirmed")
    assert settled["state"] == "succeeded"
    with pytest.raises(GroupConfigConflict):
        store.settle("10000", "456", "action-0001", "succeeded", "platform_confirmed")


def test_expiration_and_policy_change_persist_after_conflict(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    store = GroupActionStore(database)
    for action_id in ("action-expire", "action-policy"):
        store.submit(action_id, "10000", "456", "set_group_whole_ban",
                     {"enable": True}, "panel", 3, approval_required=True)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE group_actions SET expires_at=0 WHERE action_id='action-expire'")
    with pytest.raises(GroupConfigConflict):
        store.decide("10000", "456", "action-expire", approve=True, policy_revision=3)
    expired = store.get("10000", "456", "action-expire")
    assert expired is not None and expired["state"] == "expired"
    with pytest.raises(GroupConfigConflict):
        store.decide("10000", "456", "action-policy", approve=True, policy_revision=4)
    policy = store.get("10000", "456", "action-policy")
    assert policy is not None
    assert policy["state"] == "expired"
    assert policy["result"]["reason"] == "policy_changed"


def test_executing_recovers_unknown_without_retry(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    store = GroupActionStore(database)
    store.submit("action-0002", "10000", "456", "leave_group",
                 {"dismiss": False}, "model", 2, approval_required=False)
    recovered = GroupActionStore(database, recover=True)
    record = recovered.get("10000", "456", "action-0002")
    assert record is not None
    assert record["state"] == "unknown"
    assert record["result"]["reason"] == "interrupted_restart"
    with pytest.raises(GroupConfigConflict):
        recovered.settle("10000", "456", "action-0002", "succeeded", "platform_confirmed")


def test_group_request_flag_never_enters_persistent_parameters(tmp_path: Path) -> None:
    params, flag = normalize_action_params(
        "handle_group_request", {"flag": "secret-flag", "sub_type": "invite", "approve": False}, "10000",
    )
    assert flag == "secret-flag"
    assert "flag" not in params and "flag_digest" in params
    database = tmp_path / "platform.db"
    store = GroupActionStore(database)
    store.submit("action-0003", "10000", "456", "handle_group_request",
                 params, "panel", 1, approval_required=True)
    assert b"secret-flag" not in database.read_bytes()
    recovered = GroupActionStore(database, recover=True)
    record = recovered.get("10000", "456", "action-0003")
    assert record is not None and record["state"] == "expired"
    assert record["result"]["reason"] == "request_flag_lost_on_restart"


@pytest.mark.asyncio
async def test_runtime_approval_and_auto_mode_execute_exactly_once(tmp_path: Path) -> None:
    """关闭响应的已加入群仍可管理, 人工批准前没有平台副作用"""
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(
        data_root=str(tmp_path), platforms=[{"id": "bot", "type": "onebot", "settings": settings}],
    ))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.confirm_membership("100", "456", True)
    directory.patch_group("100", "456", "approval", {
        "set_group_name": {"mode": "value", "value": "approval_required"},
    }, expected_revision=0)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    assert not adapter.allows_group("456")
    assert adapter.allows_management_target("456")
    adapter.admin.set_group_name = AsyncMock()
    params: dict[str, object] = {"name": "新群名"}
    pending = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-1", "set_group_name", params, actor_kind="panel",
    )
    assert pending["state"] == "pending"
    adapter.admin.set_group_name.assert_not_awaited()
    approved = await backend.decide_group_action("bot", "100", "456", "action-runtime-1", approve=True)
    assert approved["state"] == "succeeded"
    adapter.admin.set_group_name.assert_awaited_once_with("456", name="新群名")
    duplicate = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-1", "set_group_name", params, actor_kind="panel",
    )
    assert duplicate == approved
    adapter.admin.set_group_name.assert_awaited_once()
    with pytest.raises(GroupConfigConflict):
        await backend.decide_group_action("bot", "100", "456", "action-runtime-1", approve=True)
    directory.patch_group("100", "456", "approval", {
        "set_group_name": {"mode": "value", "value": "auto_execute"},
    }, expected_revision=1)
    executed = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-2", "set_group_name", {"name": "自动群名"}, actor_kind="panel",
    )
    assert executed["state"] == "succeeded"
    assert adapter.admin.set_group_name.await_count == 2
    adapter.admin.set_group_name = AsyncMock(side_effect=AdminActionUnconfirmed("网络超时"))
    unknown = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-3", "set_group_name", {"name": "未知群名"}, actor_kind="panel",
    )
    assert unknown["state"] == "unknown"
    repeated = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-3", "set_group_name", {"name": "未知群名"}, actor_kind="panel",
    )
    assert repeated == unknown
    adapter.admin.set_group_name.assert_awaited_once()
    adapter._running = True
    adapter._capability_states["set_group_name"] = (adapter.connection_generation(), "unsupported")
    metadata = await backend.group_action_types("bot", "100", "456")
    name_action = next(item for item in metadata["items"] if item["action_type"] == "set_group_name")
    assert name_action["capability"] == "unsupported" and name_action["available"] is False
    adapter.send_management_message = AsyncMock(return_value=SendReceipt("unknown", reason="network_timeout"))
    manual = await backend.send_group_message("bot", "100", "456", "action-send-1", "直接发送")
    assert manual["state"] == "unknown"
    repeated_send = await backend.send_group_message("bot", "100", "456", "action-send-1", "直接发送")
    assert repeated_send == manual
    adapter.send_management_message.assert_awaited_once_with("456", "直接发送")
    with pytest.raises(GroupConfigConflict):
        await backend.send_group_message("bot", "100", "456", "action-send-1", "另一条消息")
