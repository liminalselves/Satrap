"""平台可疑申请与普通申请共用工具, 存储和权限"""
from pathlib import Path
import json

import pytest

from satrap.core.call_context import bind_call_origin
from satrap.core.platform import set_current_adapter_manager
from satrap.core.platform.onebot.request_registry import RequestApprovalLedger
from satrap.core.platform.onebot.admin import UnsupportedAdminAction
from satrap.core.friends import FriendError
from .test_group_admin_plugin import _setup_adapter, _origin
from .test_request_inbox import attach_friend_host, incoming, tool
from satrap.core.friends.service import FriendService
from satrap.core.platform.onebot import request_registry


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
