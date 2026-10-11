"""QQ 本群管理员授权, 平台回源与审批后的撤权复核"""
from unittest.mock import AsyncMock
from dataclasses import replace
import asyncio
from typing import Any, cast
import pytest

from satrap.expend.plugins.group_admin.tools import get_tools
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.utils.TCBuilder.manager import ToolsManager
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.framework.providers import EdictumProvider
from satrap.core.config.group_store import GroupConfigConflict
from satrap.core.framework.Base import Session
from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.config import EdictumConfigManager
from satrap.core.platform import PlatformAdapterManager, PlatformConfig, current_adapter_manager, set_current_adapter_manager
from satrap.core.type import SessionConfig
from satrap.edictum import AsyncSimpleSession


@pytest.fixture
async def runtime(tmp_path):
    previous = current_adapter_manager()
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[{
        "id": "bot", "type": "onebot", "session_provider": "edictum", "session_type": "assistant", "settings": settings,
    }]))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    adapter._bot.get_group_member_info.return_value = {"group_id": 456, "user_id": 123, "role": "admin"}
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.patch_account("100", expected_revision=1, mode="all", approval_defaults={})
    for group in ("456", "789"):
        directory.confirm_membership("100", group, True)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    backend._attach_request_ledger(adapter)
    registry = create_default_edictum_type_registry()
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_manager.create("assistant", {"edictum_type": "async_simple", "model_name": "base", "plugins": [{
        "name": "group_admin", "config": {"group_admins_enabled": True, "write_tools_enabled": True, "high_risk_approval": True},
    }]})
    provider = EdictumProvider(config_manager, registry, default_checkpoint_db=str(backend.platform_db_path("bot")))
    manager = SessionManager(db_path=backend.platform_db_path("bot"), platform_id="bot")
    manager.register_provider(provider)
    cfg = SessionConfig(session_id="qq-admin-session", session_type_name="assistant", provider_name="edictum")
    manager.store.upsert(cfg)
    session = provider.create_session(cfg, llm=cast(Any, object()))
    assert isinstance(session, AsyncSimpleSession)
    session.plugin_environment = PluginEnvironment("platform", "onebot")
    manager.pool.put("qq-admin-session", session, "assistant")
    backend._platform_runtimes["bot"] = cast(Any, (manager, None))
    adapter.group_action_handler = lambda group, action, params: backend.submit_group_action(
        "bot", "100", group, "qq-admin-action", action, params, actor_kind="model",
    )
    set_current_adapter_manager(backend._adapter_mgr)
    try:
        await provider.prepare_session_async(session)
        assert session._wf is not None
        yield backend, adapter, session._wf.tools_manager, config_manager
    finally:
        await provider.release_session_async(session)
        set_current_adapter_manager(previous)


def origin(role="admin"):
    return CallOrigin("bot", "100", "GroupMessage", "456", "123", "message", "request", group_role=role)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "owner"])
async def test_qq_administrator_uses_normal_tool_dispatch_and_approval(runtime, role):
    backend, adapter, tools, _ = runtime
    adapter._bot.get_group_member_info.return_value["role"] = role
    with bind_call_origin(origin(role)):
        result = await tools.execute_tool("group_admin_ban", {"user_id": "42", "duration": 60})
    assert result["ok"] and result["data"]["state"] == "pending"
    adapter._bot.set_group_ban.assert_not_awaited()
    approved = await backend.decide_group_action("bot", "100", "456", "qq-admin-action", approve=True)
    assert approved["state"] == "succeeded"
    adapter._bot.set_group_ban.assert_awaited_once_with(group_id=456, user_id=42, duration=60)
    assert adapter._bot.get_group_member_info.await_count >= 2
    assert all(call.kwargs == {"group_id": 456, "user_id": 123, "no_cache": True}
               for call in adapter._bot.get_group_member_info.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["role", "switch", "group_scope", "account", "generation"])
async def test_pending_qq_administrator_action_cannot_execute_after_revocation(runtime, change):
    backend, adapter, tools, config_manager = runtime
    with bind_call_origin(origin()):
        result = await tools.execute_tool("group_admin_ban", {"user_id": "42"})
    assert result["data"]["state"] == "pending"
    if change == "role":
        adapter._bot.get_group_member_info.return_value["role"] = "member"
    elif change == "account":
        adapter.bot_self_id = "200"
    elif change == "generation":
        adapter._connection_generation += 1
    else:
        config_manager.update("assistant", {"plugins": [{"name": "group_admin", "config": {
            "group_admins_enabled": change != "switch", "write_tools_enabled": True, "high_risk_approval": True,
            "allowed_groups": "789" if change == "group_scope" else "",
        }}]})
    if change in {"account", "generation"}:
        with pytest.raises(ValueError if change == "account" else GroupConfigConflict):
            await backend.decide_group_action("bot", "100", "456", "qq-admin-action", approve=True)
    else:
        approved = await backend.decide_group_action("bot", "100", "456", "qq-admin-action", approve=True)
        assert approved["state"] == "failed"
    adapter._bot.set_group_ban.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["member", "private", "cross_group", "wrong_member", "wrong_group", "unavailable"])
async def test_qq_role_does_not_grant_other_members_or_other_groups(runtime, case):
    _, adapter, tools, _ = runtime
    source = origin("member" if case == "member" else "admin")
    params = {"user_id": "42"}
    if case == "private":
        source = replace(source, chat_type="FriendMessage", chat_id="123")
        params["group_id"] = "456"
    elif case == "cross_group":
        params["group_id"] = "789"
    elif case == "wrong_member":
        adapter._bot.get_group_member_info.return_value["user_id"] = 999
    elif case == "wrong_group":
        adapter._bot.get_group_member_info.return_value["group_id"] = 789
    elif case == "unavailable":
        adapter._bot.get_group_member_info.side_effect = RuntimeError("平台故障")
    with bind_call_origin(source):
        result = await tools.execute_tool("group_admin_ban", params)
    assert result["ok"] is False
    adapter._bot.set_group_ban.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_is_rechecked_after_action_waits_to_send(runtime, monkeypatch):
    backend, adapter, tools, _ = runtime
    with bind_call_origin(origin()):
        assert (await tools.execute_tool("group_admin_ban", {"user_id": "42"}))["data"]["state"] == "pending"
    entered, release = asyncio.Event(), asyncio.Event()
    original = adapter.admin.ban_group_member

    async def delayed(group_id, user_id, duration=1800):
        entered.set()
        await release.wait()
        await original(group_id, user_id, duration)

    monkeypatch.setattr(adapter.admin, "ban_group_member", delayed)
    task = asyncio.create_task(backend.decide_group_action("bot", "100", "456", "qq-admin-action", approve=True))
    await entered.wait()
    adapter._bot.get_group_member_info.return_value["role"] = "member"
    release.set()
    result = await task
    assert result["state"] == "failed" and result["result"]["reason"] == "model_permission_revoked"
    adapter._bot.set_group_ban.assert_not_awaited()


@pytest.mark.asyncio
async def test_qq_administrator_can_query_and_approve_only_member_applications(runtime):
    backend, adapter, tools, _ = runtime
    await adapter.request_flags.register("group", "member-request", self_id="100", group_id="456", sub_type="add", user_id="42")
    await adapter.request_flags.register("group", "bot-invitation", self_id="100", group_id="456", sub_type="invite", user_id="42")
    with bind_call_origin(origin()):
        listing = await tools.execute_tool("group_admin_list_group_requests", {})
        assert listing["ok"] and listing["data"]["total"] == 1
        result = await tools.execute_tool("group_admin_handle_group_request", {
            "request_id": listing["data"]["items"][0]["request_id"], "approve": True,
        })
    assert result["ok"] and result["data"]["state"] == "pending"
    approved = await backend.decide_group_action("bot", "100", "456", "qq-admin-action", approve=True)
    assert approved["state"] == "succeeded"
    adapter._bot.set_group_add_request.assert_awaited_once_with(flag="member-request", sub_type="add", approve=True, reason="")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,write_enabled,current_role,success", [
    (True, True, "admin", True), (False, True, "admin", False),
    (True, False, "admin", False), (True, True, "member", False),
])
async def test_synchronous_qq_administrator_calls_are_verified_on_platform_loop(runtime, enabled, write_enabled, current_role, success):
    _, adapter, _, _ = runtime
    adapter._loop = asyncio.get_running_loop()
    adapter.group_action_handler = None
    adapter._bot.get_group_member_info.return_value["role"] = current_role
    tool = next(tool for tool in get_tools(cast(Session, object()), {
        "group_admins_enabled": enabled, "write_tools_enabled": write_enabled,
    }) if tool.tool_name == "group_admin_ban")
    manager = ToolsManager()
    manager.register_tool(tool)
    with bind_call_origin(origin()):
        result = await asyncio.to_thread(manager.execute_tool, "group_admin_ban", {"user_id": "42", "duration": 60})
    assert result["ok"] is success
    if success:
        adapter._bot.set_group_ban.assert_awaited_once_with(group_id=456, user_id=42, duration=60)
        assert adapter._bot.get_group_member_info.await_count == 2
    else:
        adapter._bot.set_group_ban.assert_not_awaited()
