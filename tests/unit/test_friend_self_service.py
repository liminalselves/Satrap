"""好友本人服务的身份隔离, 稳定声明和主动申请契约"""
from unittest.mock import AsyncMock
from dataclasses import replace

import pytest

from satrap.core.call_context import bind_call_origin
from satrap.core.utils.TCBuilder import AsyncToolsManager
from .test_group_admin_plugin import _setup_adapter, _origin
from .test_request_inbox import incoming, tool, restore_manager


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["GroupMessage", "FriendMessage"])
async def test_ordinary_caller_only_lists_and_handles_own_request(kind):
    adapter = _setup_adapter()
    await incoming(adapter)
    adapter.client_self_id = adapter.bot_self_id
    await adapter.request_flags.register("friend", "another", self_id="10000", user_id="999", comment="他人的隐私")
    all_items = (await adapter.friend_requests("10000", 20, None))["items"]
    foreign = next(row for row in all_items if row["user_id"] == "999")
    with bind_call_origin(_origin(chat_type=kind, actor="321")):
        query = tool("friend_manager_list_requests", request_managers="")
        result = await query.execute(limit=1)
        assert result["ok"] and result["data"]["scope"] == "self"
        assert result["data"]["total"] == 1 and not result["data"]["has_more"]
        own = result["data"]["items"][0]
        assert own["user_id"] == "321"
        handle = tool("friend_manager_handle_request", request_managers="", write_callers="")
        denied = await handle.execute(request_id=foreign["request_id"], approve=True)
        assert denied["error"]["code"] == "permission_denied"
        adapter._bot.set_friend_add_request.assert_not_awaited()
        checked = await tool("friend_manager_recheck_request", request_managers="").execute(request_id=own["request_id"])
        assert checked["ok"] and checked["data"]["scope"] == "self"
        accepted = await handle.execute(request_id=own["request_id"], approve=True)
        assert accepted["ok"] and accepted["data"]["state"] == "succeeded"
    adapter._bot.set_friend_add_request.assert_awaited_once_with(flag="private-flag", approve=True, remark="")


@pytest.mark.asyncio
async def test_stable_definitions_do_not_grant_directory_access_to_ordinary_caller():
    adapter = _setup_adapter()
    await incoming(adapter)
    adapter._bot.get_friend_list.return_value = []
    manager = AsyncToolsManager()
    manager.register_tool(tool("friend_manager_list_friends"))
    manager.register_tool(tool("friend_manager_list_requests"))
    with bind_call_origin(_origin(chat_type="FriendMessage")):
        administrator = manager.get_tools_definitions()
        assert (await manager.execute_tool("friend_manager_list_friends", {}))["ok"]
    with bind_call_origin(_origin(chat_type="FriendMessage", actor="999")):
        assert manager.get_tools_definitions() == administrator
        assert "error" in await manager.execute_tool("friend_manager_list_friends", {})
        result = await manager.execute_tool("friend_manager_list_requests", {})
        assert result["ok"] and result["data"]["scope"] == "self" and not result["data"]["items"]
    manager.disable_tool("friend_manager_list_friends")
    assert len(manager.get_tools_definitions()) == 1


@pytest.mark.asyncio
async def test_missing_request_counts_and_cursors_are_scoped_before_pagination():
    adapter = _setup_adapter()
    await incoming(adapter)
    await adapter.request_flags.ledger.register("ob", "10000", "friend", "foreign-missing", user_id="999")
    await adapter.request_flags.register("friend", "second-own", self_id="10000", user_id="321")
    with bind_call_origin(_origin(chat_type="FriendMessage")):
        admin_page = await tool("friend_manager_list_requests").execute(limit=1)
        assert admin_page["data"]["unavailable_count"] == 1
    with bind_call_origin(_origin(chat_type="FriendMessage", actor="321")):
        query = tool("friend_manager_list_requests", request_managers="")
        page = await query.execute(limit=1)
        assert page["data"]["total"] == 2 and page["data"]["unavailable_count"] == 0
        next_page = await query.execute(limit=1, cursor=page["data"]["next_cursor"])
        assert len(next_page["data"]["items"]) == 1
        denied = await query.execute(limit=1, cursor=admin_page["data"]["next_cursor"])
        assert not denied["ok"] and denied["error"]["code"] == "cursor_expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["GroupMessage", "FriendMessage"])
async def test_ordinary_caller_can_request_own_deletion_but_needs_approval(kind):
    adapter = _setup_adapter()
    await incoming(adapter)
    adapter.client_self_id = adapter.bot_self_id
    delete = tool("friend_manager_delete_friend", request_managers="", write_callers="", delete_friend_enabled=True)
    adapter._bot.get_friend_list.return_value = [{"user_id": 321, "nickname": "本人", "remark": ""}]
    with bind_call_origin(_origin(chat_type=kind, actor="321")):
        denied = await delete.execute(user_id="999")
        assert denied["error"]["code"] == "permission_denied"
        result = await delete.execute()
        assert result["ok"] and result["data"]["state"] == "pending"
        adapter._bot.delete_friend.assert_not_awaited()
    approved = await adapter.friend_host.decide("10000", result["data"]["action_id"], True)
    assert approved["state"] == "succeeded"
    adapter._bot.delete_friend.assert_awaited_once_with(user_id=321)


@pytest.mark.asyncio
async def test_own_deletion_cannot_bypass_protection_or_revoked_switch():
    adapter = _setup_adapter()
    await incoming(adapter)
    delete = tool("friend_manager_delete_friend", request_managers="", write_callers="", delete_friend_enabled=True)
    adapter._bot.get_friend_list.return_value = [{"user_id": 321, "nickname": "本人", "remark": ""}]
    with bind_call_origin(_origin(chat_type="FriendMessage", actor="321")):
        result = await delete.execute()
        delete.config["delete_friend_enabled"] = False
    approved = await adapter.friend_host.decide("10000", result["data"]["action_id"], True)
    assert approved["state"] == "failed"
    delete.config.update(delete_friend_enabled=True, protected_friend_ids="321")
    with bind_call_origin(_origin(chat_type="FriendMessage", actor="321")):
        assert not (await delete.execute())["ok"]
    adapter._bot.delete_friend.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_request_supports_self_only_and_deduplicates_success_and_unknown():
    adapter = _setup_adapter()
    await incoming(adapter)
    adapter._bot.get_friend_list.return_value = []
    send = tool("friend_manager_send_request", request_managers="", write_callers="", send_request_enabled=True)
    caps = adapter.friend_capabilities
    adapter.friend_capabilities = lambda: {**caps(), "send_request": {"state": "supported"}}
    adapter.friend_send_request = AsyncMock(return_value={"status": "submitted"})
    with bind_call_origin(_origin(chat_type="FriendMessage", actor="321")):
        assert (await send.execute(user_id="999"))["error"]["code"] == "permission_denied"
        result = await send.execute(message="添加本人")
        assert result["ok"] and result["data"]["result"]["status"] == "submitted"
        repeated = await send.execute(message="添加本人")
        assert repeated["error"]["code"] == "unresolved_action"
    adapter.friend_send_request.assert_awaited_once_with("10000", "321", "添加本人")
    adapter.friend_send_request.side_effect = TimeoutError()
    with bind_call_origin(_origin(chat_type="FriendMessage", actor="999")):
        unknown = await send.execute()
        assert unknown["data"]["state"] == "unknown"
        assert (await send.execute())["error"]["code"] == "unresolved_action"
    assert adapter.friend_send_request.await_count == 2


@pytest.mark.asyncio
async def test_send_request_reports_unsupported_and_does_not_fake_inbound_acceptance():
    adapter = _setup_adapter()
    await incoming(adapter)
    send = tool("friend_manager_send_request", request_managers="", write_callers="", send_request_enabled=True)
    with bind_call_origin(_origin(chat_type="FriendMessage", actor="321")):
        result = await send.execute()
        assert result["error"]["code"] == "unsupported"
    adapter._bot.set_friend_add_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_request_admin_target_existing_friend_and_connection_change():
    adapter = _setup_adapter()
    await incoming(adapter)
    adapter.client_self_id = adapter.bot_self_id
    adapter._bot.get_friend_list.return_value = [{"user_id": 999, "nickname": "好友"}]
    caps = adapter.friend_capabilities
    adapter.friend_capabilities = lambda: {**caps(), "send_request": {"state": "supported"}}
    adapter.friend_send_request = AsyncMock(return_value={"status": "submitted"})
    send = tool("friend_manager_send_request", send_request_enabled=True)
    with bind_call_origin(_origin(chat_type="GroupMessage")):
        result = await send.execute(user_id="999")
        assert result["ok"] and result["data"]["result"]["status"] == "already_friends"
        adapter.friend_send_request.assert_not_awaited()
        adapter._bot.get_friend_list.return_value = []
        async def disconnect(*args):
            adapter._connection_generation += 1
            return {"status": "submitted"}
        adapter.friend_send_request.side_effect = disconnect
        result = await send.execute(user_id="777", message="管理员代发")
        assert result["ok"] and result["data"]["state"] == "unknown"
        assert result["data"]["result"]["code"] == "stale_account"
    adapter.friend_send_request.assert_awaited_once_with("10000", "777", "管理员代发")
    assert send.recovery_policy == "manual"


@pytest.mark.asyncio
async def test_missing_actor_cannot_use_self_service():
    adapter = _setup_adapter()
    await incoming(adapter)
    for name in ("friend_manager_list_requests", "friend_manager_handle_request", "friend_manager_delete_friend"):
        handle = tool(name, request_managers="", write_callers="", delete_friend_enabled=True)
        with bind_call_origin(replace(_origin(), actor_id="")):
            params = {"request_id": "any", "approve": True} if name.endswith("handle_request") else {}
            result = await handle.execute(**params)
            assert not result["ok"] and result["error"]["code"] == "permission_denied"
    adapter._bot.delete_friend.assert_not_awaited()
    adapter._bot.set_friend_add_request.assert_not_awaited()
