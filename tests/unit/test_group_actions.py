"""验证逐群审批的持久占用、幂等与请求目标边界"""
from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from satrap.core.config.group_actions import GroupActionStore
from satrap.core.config.group_store import GroupConfigConflict
from satrap.core.platform.onebot.group_action_types import normalize_action_params


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
