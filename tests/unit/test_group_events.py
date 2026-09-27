"""验证近期群事件脱敏、身份隔离和诊断群过滤"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from satrap.core.config.group_events import GroupEventBuffer, event_values
from satrap.core.config.group_store import GroupConfigStore
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.config.group_actions import GroupActionStore
from satrap.core.pipeline.request_diagnostics import RequestDiagnostic, RequestDiagnosticLog
from satrap.core.platform import PlatformEvent
from satrap.core.platform.notices import NoticePayload
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform import PlatformConfig


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
