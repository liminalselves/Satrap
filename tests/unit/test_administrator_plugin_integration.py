from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from satrap.core.call_context import bind_call_origin
from satrap.core.config.administrator_groups import AdministratorService
from satrap.core.config.platform_identity import platform_instance_id
from satrap.core.platform import current_adapter_manager, set_current_adapter_manager
from satrap.edictum import AsyncSimpleSession
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.core.plugin_authorization import evaluate_plugin_permissions
from satrap.expend.plugins.group_chat.tools import get_tools as chat_tools
from .test_group_admin_plugin import _setup_adapter, _origin, _async_tools
from .test_group_tool_migration import runtime


def groups(platform, plugins, *, enabled=True, excluded=()):
    return [{"id": "main", "name": "主要管理员", "enabled": enabled,
             "members": [{"platform_id": platform["id"], "platform_instance_id": platform_instance_id(platform), "user_id": "123"}],
             "plugin_scope": {"mode": "selected", "included": list(plugins), "excluded": list(excluded)}}]


@pytest.fixture(autouse=True)
def restore_manager():
    previous = current_adapter_manager()
    yield
    set_current_adapter_manager(previous)


def test_all_seven_gates_use_admin_grants_and_preserve_local_empty_rules(tmp_path):
    platform = {"id": "ob", "instance_id": "original"}
    service = AdministratorService(lambda: [platform], groups(platform, ["group_admin", "group_chat", "friend_manager"]))
    catalog = PluginCatalog(user_dir=tmp_path / "plugins")
    cases = [("group_admin", "group_admin_get_honors", "allowed_read_callers", True),
             ("group_admin", "group_admin_kick", "allowed_callers", False),
             ("group_admin", "group_admin_list_group_requests", "request_managers", False),
             ("group_chat", "group_chat_list_groups", "cross_group_query_callers", False),
             ("group_chat", "group_chat_set_group_nickname", "nickname_allowed_callers", True),
             ("friend_manager", "friend_manager_list_friends", "managers", False),
             ("friend_manager", "friend_manager_delete_friend", "write_callers", False)]
    for name, tool, field, empty_allow in cases:
        entry = catalog.get(name)
        assert entry and entry.permissions.supports_administrators
        config = {field: ""}
        assert evaluate_plugin_permissions(name, entry.permissions, "tools", tool, config, _origin(), service).status == "allowed"
        if tool == "friend_manager_delete_friend":
            config["managers"] = "123"
        decision = evaluate_plugin_permissions(name, entry.permissions, "tools", tool, config, _origin())
        assert (decision.status == "allowed") == empty_allow
        assert config[field] == ""


@pytest.mark.asyncio
async def test_request_queries_pass_multiple_admin_gates_but_never_open_write_switch(tmp_path):
    adapter = _setup_adapter()
    adapter.bot_self_id = "10000"
    platform = {"id": "ob", "instance_id": "original"}
    manager = current_adapter_manager()
    manager.administrator_service = AdministratorService(lambda: [platform], groups(platform, ["group_admin"]))
    tools = _async_tools({})
    query = next(tool for tool in tools if tool.tool_name == "group_admin_list_group_requests")
    write = next(tool for tool in tools if tool.tool_name == "group_admin_handle_group_request")
    with bind_call_origin(_origin()):
        assert query.is_available_for_call()
        result = await query.execute()
        assert result["status"] == "ok"
        denied = await write.execute(request_id="request", approve=True)
        assert denied["status"] == "error" and "未在插件配置中开启" in denied["error"]
    with bind_call_origin(_origin(actor="999")):
        assert not query.is_available_for_call()
        assert (await query.execute())["status"] == "error"


@pytest.mark.asyncio
async def test_admin_private_group_list_rechecks_revocation_after_adapter_read(tmp_path, monkeypatch):
    from .test_group_chat_service import _setup
    _, adapter, origin = _setup(tmp_path)
    adapter._bot.get_group_list.return_value = [{"group_id": 456}]
    platform = {"id": "ob", "instance_id": "original"}
    service = AdministratorService(lambda: [platform], groups(platform, ["group_chat"]))
    current_adapter_manager().administrator_service = service
    session = AsyncSimpleSession("private", cast(Any, object()), enable_checkpoint=False, db_path=str(tmp_path / "context.db"))
    query = next(tool for tool in chat_tools(session, {}) if tool.tool_name == "group_chat_list_groups")
    with bind_call_origin(replace(origin, chat_type="FriendMessage", chat_id="123")):
        assert query.is_available_for_call() and (await query.execute())["ok"]
        read = adapter.group_chat_groups
        async def revoke(scope):
            result = await read(scope)
            service.apply([])
            return result
        monkeypatch.setattr(adapter, "group_chat_groups", revoke)
        result = await query.execute()
        assert not result["ok"] and result["error"]["code"] == "forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [True, False])
@pytest.mark.parametrize("change", ["none", "remove", "exclude", "disable", "unrelated", "local"])
async def test_real_pending_action_admin_grant_and_revocation(tmp_path: Path, asynchronous, change):
    import asyncio
    backend, adapter, origin, session, provider, configs = await runtime(tmp_path, asynchronous, "group_admin")
    try:
        manager = current_adapter_manager()
        manager.administrator_service = backend.administrator_service
        configured = groups(backend.config.platforms[0], ["group_admin"])
        backend.administrator_service.apply(configured)
        tool = session._wf.tools_manager.tools["group_admin_set_group_nickname"]
        config = {"write_tools_enabled": True, "allowed_callers": "123" if change == "local" else ""}
        tool.config.update(config)
        configs.update("assistant", {"plugins": [{"name": "group_admin", "config": config}]})
        with bind_call_origin(origin):
            assert tool.is_available_for_call()
            pending = await tool.execute(user_id="321", nickname="管理员验收") if asynchronous else await asyncio.to_thread(tool.execute, user_id="321", nickname="管理员验收")
        assert pending["data"]["state"] == "pending"
        adapter._bot.set_group_card.assert_not_awaited()
        if change in {"remove", "local"}:
            configured = []
        elif change == "exclude":
            configured[0]["plugin_scope"]["excluded"] = ["group_admin"]
        elif change == "disable":
            configured[0]["enabled"] = False
        elif change == "unrelated":
            configured[0]["name"] = "修改显示名称"
        backend.administrator_service.apply(configured)
        record = await backend.decide_group_action("ob", "10000", "456", pending["data"]["action_id"], approve=True)
        if change in {"remove", "exclude", "disable"}:
            assert record["state"] == "failed"
            adapter._bot.set_group_card.assert_not_awaited()
        else:
            assert record["state"] == "succeeded"
            adapter._bot.set_group_card.assert_awaited_once()
        assert "123" in backend._friend_protected_managers("ob") if configured and configured[0]["enabled"] else "123" not in backend._friend_protected_managers("ob")
    finally:
        await provider.release_session_async(session)


@pytest.mark.asyncio
async def test_friend_admin_empty_lists_preserve_pending_and_recheck_revocation(tmp_path):
    backend, adapter, origin, session, provider, configs = await runtime(
        tmp_path, True, "friend_manager", {"delete_friend_enabled": True},
    )
    try:
        current_adapter_manager().administrator_service = backend.administrator_service
        backend.administrator_service.apply(groups(backend.config.platforms[0], ["friend_manager"]))
        adapter._running = True
        adapter.friend_host = backend.friend_service("ob")
        adapter._bot.get_friend_list.return_value = [{"user_id": 123, "nickname": "管理员"}, {"user_id": 321, "nickname": "普通好友"}]
        private = replace(origin, chat_type="FriendMessage", chat_id="123")
        query = session._wf.tools_manager.tools["friend_manager_list_friends"]
        deletion = session._wf.tools_manager.tools["friend_manager_delete_friend"]
        with bind_call_origin(private):
            assert query.is_available_for_call()
            assert (await query.execute())["ok"]
            protected = await deletion.execute(user_id="123")
            assert not protected["ok"]
            pending = await deletion.execute(user_id="321")
            assert pending["ok"] and pending["data"]["state"] == "pending"
        adapter._bot.delete_friend.assert_not_awaited()
        backend.administrator_service.apply([])
        with bind_call_origin(private):
            assert not query.is_available_for_call() and not (await query.execute())["ok"]
        action = pending["data"]["action_id"]
        record = await adapter.friend_host.decide("10000", action, True)
        assert record["state"] == "failed"
        adapter._bot.delete_friend.assert_not_awaited()
    finally:
        await provider.release_session_async(session)
