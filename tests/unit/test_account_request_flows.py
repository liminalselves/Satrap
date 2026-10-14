"""业务链路场景与集中边界契约"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
import asyncio
import ast
import json
import sqlite3
import time
import pytest
from satrap.expend.plugins.account_requests.tools import get_tools, DEFINITIONS, AsyncAccountRequestTool
from satrap.expend.plugins.group_admin.tools import _DEFINITIONS as group_definitions, get_tools as group_tools
from satrap.core.platform.onebot.request_registry import RequestApprovalLedger
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.platform import PlatformAdapterManager, PlatformConfig, current_adapter_manager
from satrap.core.platform import set_current_adapter_manager
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.config.group_store import GroupConfigConflict
from satrap.core.config.group_action_origin import ModelActionAuthorization, bind_model_action_authorization
from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.edictum.account_request_migration import migrate_account_request_specs
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_spec import parse_plugin_specs
from satrap.edictum import AsyncSimpleSession
from satrap.core.framework.Base import Session
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.providers import EdictumProvider
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.config import EdictumConfigManager
from satrap.core.type import SessionConfig
from satrap.core.utils.TCBuilder import AsyncToolsManager
from support.administration import _setup_adapter, _origin
from support.requests import incoming, tool, restore_manager
from types import SimpleNamespace
from satrap.core.friends import FriendError
from satrap.core.friends.service import FriendService
from satrap.core.friends.store import FriendStore

@pytest.fixture()
def restore_adapter_manager():
    previous = current_adapter_manager()
    yield
    set_current_adapter_manager(previous)

@pytest.fixture
def host(tmp_path: Path):
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[{"id": "bot", "type": "onebot"}]))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot"))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    manager = PlatformAdapterManager()
    manager._adapters["bot"] = adapter
    backend._adapter_mgr = manager
    set_current_adapter_manager(manager)
    return backend, adapter

def invitation_tool(name: str = "account_requests_list_group_invites", **config: Any):
    class FakeAsync(AsyncSimpleSession):
        def __init__(self):
            pass
    return next(tool for tool in get_tools(FakeAsync(), config) if tool.tool_name == name)

@pytest.fixture
async def invitation_runtime(host, tmp_path):
    backend, adapter = host
    backend.config.platforms[0].update(session_provider="edictum", session_type="assistant")
    backend._adapter_mgr.administrator_service = backend.administrator_service
    registry = create_default_edictum_type_registry()
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_manager.create("assistant", {"edictum_type": "async_simple", "model_name": "base", "plugins": [
        {"name": "account_requests", "enabled": True, "config": {"request_managers": "123", "allowed_callers": "123",
         "approval_required": True, "administrator_invites_auto_approve": True}}]})
    provider = EdictumProvider(config_manager, registry, default_checkpoint_db=str(backend.platform_db_path("bot")))
    manager = SessionManager(db_path=backend.platform_db_path("bot"), platform_id="bot")
    manager.register_provider(provider)
    cfg = SessionConfig(session_id="invite-session", session_type_name="assistant", provider_name="edictum")
    manager.store.upsert(cfg)
    session = provider.create_session(cfg, llm=cast(Any, object()))
    assert isinstance(session, AsyncSimpleSession)
    session.plugin_environment = PluginEnvironment("platform", "onebot")
    manager.pool.put("invite-session", session, "assistant")
    backend._platform_runtimes["bot"] = cast(Any, (manager, None))
    try:
        await provider.prepare_session_async(session)
        assert session._wf is not None
        tool = session._wf.tools_manager.tools["account_requests_handle_group_invite"]
        yield backend, adapter, tool, config_manager
    finally:
        await provider.release_session_async(session)

def origin(**changes: Any):
    return replace(CallOrigin("bot", "100", "FriendMessage", "123", "123", "message", "call"), **changes)

async def register(adapter: OneBotAdapter, flag: str = "invite-secret", group: str = "999", sub_type: str = "invite"):
    await adapter.request_flags.register("group", flag, self_id="100", group_id=group, sub_type=sub_type, user_id="321")
    return (await adapter.request_flags.list_requests("group", self_id="100", group_id=group, sub_type=sub_type))["items"][0]

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.parametrize("enabled", [False, True])
def test_migration_preserves_old_permissions_switches_and_is_once(tmp_path: Path, enabled: bool):
    catalog = PluginCatalog(user_dir=tmp_path / "plugins")
    manager = PluginConfigManager(tmp_path / "config")
    old = catalog.get("group_admin")
    assert old is not None
    manager.save_global("group_admin", old.config_schema, {"request_managers": "123\n456\n789", "allowed_read_callers": "123\n456",
        "allowed_callers": "456\n789", "write_tools_enabled": True, "allowed_groups": "999"})
    source = [{"name": "group_admin", "enabled": enabled,
               "capabilities": {"tools": {"group_admin_list_group_requests": False, "group_admin_handle_group_request": True}}}]
    original = deepcopy(source)
    result = migrate_account_request_specs(source, catalog, manager)
    assert source == original
    account = result[-1]
    assert account["enabled"] is enabled
    assert account["config"] == {"request_managers": "123\n456", "allowed_callers": "456", "allowed_invitation_groups": "999", "approval_required": True}
    assert not account["capabilities"]["tools"]["account_requests_list_group_invites"]
    assert account["capabilities"]["tools"]["account_requests_handle_group_invite"]
    assert migrate_account_request_specs(result, catalog, manager) == result
    assert migrate_account_request_specs(result[:-1], catalog, manager) == result[:-1]

@pytest.mark.usefixtures('restore_adapter_manager')
def test_migration_explicit_new_plugin_wins_and_plain_admin_stays_independent(tmp_path: Path):
    catalog = PluginCatalog(user_dir=tmp_path / "plugins")
    manager = PluginConfigManager(tmp_path / "config")
    explicit = {"name": "account_requests", "enabled": False, "config": {"request_managers": "321", "approval_required": False}}
    result = migrate_account_request_specs([{"name": "group_admin", "config": {"request_managers": "123"}}, explicit], catalog, manager)
    assert result[-1] == explicit
    plain = migrate_account_request_specs(["group_admin"], catalog, manager)
    assert len(plain) == 1 and plain[0]["migration_state"]["account_group_invites"] == 1
    specs = parse_plugin_specs(result, catalog)
    canonical = [spec.to_config() for spec in specs]
    assert [spec.to_config() for spec in parse_plugin_specs(canonical, catalog)] == canonical

@pytest.mark.usefixtures('restore_adapter_manager')
def test_plugins_have_no_cross_imports_and_only_two_account_tools(tmp_path: Path):
    assert not set(group_definitions) & {
        "group_admin_list_groups", "group_admin_get_group_info", "group_admin_list_members",
        "group_admin_get_member", "group_admin_get_message", "group_admin_set_card",
    }
    assert "group_admin_set_group_nickname" in group_definitions
    root = Path(__file__).resolve().parents[2] / "satrap/expend/plugins"
    plugins = {"account_requests", "friend_manager", "group_admin", "group_chat"}
    for plugin in plugins:
        for path in (root / plugin).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [item.name for item in node.names] if isinstance(node, ast.Import) else []
                assert all(not name.startswith(f"satrap.expend.plugins.{other}") for name in names for other in plugins - {plugin})
    # 旧群管理好友工具名只允许出现在迁移模块
    for path in (Path(__file__).resolve().parents[2] / "satrap").rglob("*.py"):
        if path.name == "plugin_migrations.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert "group_admin_list_friend_requests" not in source and "group_admin_handle_friend_request" not in source
    catalog = PluginCatalog(user_dir=tmp_path)
    entry = catalog.get("account_requests")
    assert entry is not None and set(entry.capabilities["tools"]) == set(DEFINITIONS)
    assert entry.config_schema["administrator_invites_auto_approve"].default is False
    assert set(entry.capabilities["skills"]) == {"account_requests"}
    sync = get_tools(cast(Session, object()))
    assert len(sync) == 2 and not any(isinstance(tool, AsyncAccountRequestTool) for tool in sync)
    assert sync[1].recovery_policy == "manual"
    assert sync[1].get_tool_defined()["function"]["parameters"]["additionalProperties"] is False

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
async def test_account_scope_excludes_members_filters_before_paging_and_counts_missing(host):
    _, adapter = host
    member = await register(adapter, "member", "999", "add")
    outside = await register(adapter, "other-invite", "888")
    await register(adapter, "first-invite")
    await register(adapter, "second-invite")
    tool = invitation_tool(request_managers="123", allowed_invitation_groups="999")
    with bind_call_origin(origin()):
        first = await tool.execute(limit=1)
        assert first["ok"] and first["data"]["total"] == 2
        assert first["data"]["has_more"]
        second = await tool.execute(limit=1, cursor=first["data"]["next_cursor"])
        assert len(second["data"]["items"]) == 1 and not second["data"]["has_more"]
        assert first["data"]["items"][0]["request_id"] != second["data"]["items"][0]["request_id"]
        assert "secret" not in json.dumps(first)
        denied = await tool.execute(group_id="888")
        assert not denied["ok"]
        for cursor in (member["request_id"], outside["request_id"]):
            wrong_page = await tool.execute(cursor=cursor)
            assert not wrong_page["ok"] and wrong_page["error_type"] == "invalid_arguments"
    with sqlite3.connect(adapter.request_flags.ledger.inbox_path) as connection:
        connection.execute("DELETE FROM requests")
    with bind_call_origin(origin()):
        missing = await tool.execute()
    assert missing["data"]["unavailable_count"] == 2

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
async def test_declarations_stay_stable_but_execution_denies_unauthorized_callers(host):
    _, adapter = host
    await register(adapter)
    tool = invitation_tool(request_managers="123")
    with bind_call_origin(origin(actor_id="456")):
        assert tool.is_available_for_call()
        denied = await tool.execute()
    assert not denied["ok"] and denied["error_type"] == "permission_denied"
    with bind_call_origin(origin()):
        assert (await tool.execute())["ok"]

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
async def test_sync_account_query_bridges_to_platform_loop(host):
    _, adapter = host
    item = await register(adapter)
    adapter._loop = asyncio.get_running_loop()
    tool = get_tools(cast(Session, object()), {"request_managers": "123"})[0]
    with bind_call_origin(origin()):
        result = await asyncio.to_thread(tool.execute)
    assert result["ok"] and result["data"]["items"][0]["request_id"] == item["request_id"]

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["account", "connection", "permission", "scope"])
async def test_read_rechecks_identity_and_permission_after_wait(host, monkeypatch, change):
    _, adapter = host
    await register(adapter)
    tool = invitation_tool(request_managers="123")
    original = adapter.request_flags.list_requests

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        if change == "account":
            adapter.bot_self_id = "200"
        elif change == "connection":
            adapter._connection_generation += 1
        elif change == "permission":
            tool.config["request_managers"] = "456"
        else:
            tool.config["allowed_invitation_groups"] = "888"
        return result

    monkeypatch.setattr(adapter.request_flags, "list_requests", changed)
    with bind_call_origin(origin()):
        result = await tool.execute()
    assert not result["ok"] and result["error_type"] == "permission_denied"

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
async def test_panel_invite_works_without_group_directory_and_is_idempotent(host):
    backend, adapter = host
    item = await register(adapter)
    params = {"request_id": item["request_id"], "expected_revision": item["revision"], "approve": False, "reason": "不加入"}
    result = await backend.submit_account_group_invite("bot", "100", params, actor_kind="panel", action_id="panel-invite")
    assert result["state"] == "succeeded"
    assert await backend.submit_account_group_invite("bot", "100", params, actor_kind="panel", action_id="panel-invite") == result
    with pytest.raises(GroupConfigConflict):
        await backend.submit_account_group_invite("bot", "100", {**params, "approve": True}, actor_kind="panel", action_id="panel-invite")
    adapter._bot.set_group_add_request.assert_awaited_once_with(flag="invite-secret", sub_type="invite", approve=False, reason="不加入")
    assert (await backend.account_invite_actions("bot", "100", state="all"))["total"] == 1
    assert (await backend.group_actions("bot", "100", "999", state="all"))["total"] == 0

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
async def test_panel_archived_invite_requires_current_revision(host, monkeypatch):
    backend, adapter = host
    clock = [1000.0]
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: clock[0])
    await register(adapter)
    clock[0] = 1700.0
    item = (await backend.account_group_invites("bot", "100", view="archived"))["items"][0]
    assert item["can_handle"] and item["requires_confirmation"]
    with pytest.raises(LookupError):
        await backend.submit_account_group_invite("bot", "100", {"request_id": item["request_id"], "approve": True}, actor_kind="panel")
    with pytest.raises(ValueError):
        await backend.submit_account_group_invite("bot", "100", {"request_id": item["request_id"], "approve": True, "expected_revision": 1}, actor_kind="panel")
    monkeypatch.setattr(time, "time", lambda: clock[0])
    result = await backend.submit_account_group_invite("bot", "100", {"request_id": item["request_id"], "approve": True, "expected_revision": item["revision"]}, actor_kind="panel")
    assert result["state"] == "succeeded"

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
async def test_http_rejects_wrong_kind_and_account_and_group_invitation_entry(host):
    backend, adapter = host
    server = BackendHTTPServer(backend)
    base = "/api/platforms/bot/account-requests/group-invites"
    add = await register(adapter, "add-secret", "999", "add")
    status, _ = await server._route("POST", base, json.dumps({"expected_self_id": "100", "action_id": "wrong-kind", "request_id": add["request_id"], "approve": True}).encode())
    assert status == 403
    status, _ = await server._route("GET", base + "?account=200", b"")
    assert status == 403
    for query in ("", "?account=100&limit=0", "?account=100&account=100", "?account=100&flag=x"):
        assert (await server._route("GET", base + query, b""))[0] == 400
    item = await register(adapter)
    payload = {"expected_self_id": "100", "action_id": "http-invite", "request_id": item["request_id"], "expected_revision": item["revision"], "approve": True}
    assert (await server._route("POST", base, json.dumps(payload).encode()))[0] == 200
    assert (await server._route("POST", base, json.dumps(payload).encode()))[0] == 200
    assert (await server._route("POST", base, json.dumps({**payload, "approve": False}).encode()))[0] == 409
    group_base = "/api/platforms/bot/groups/999/actions"
    assert (await server._route("GET", group_base + "/http-invite?account=100", b""))[0] == 400
    assert (await server._route("POST", group_base + "/http-invite/decision", b'{"expected_self_id":"100","approve":true}'))[0] == 400
    assert (await server._route("POST", group_base, b'{"expected_self_id":"100","action_id":"old","action_type":"handle_group_request","params":{"sub_type":"invite","flag":"x","approve":true}}'))[0] == 400
    adapter._bot.set_group_add_request.assert_awaited_once()

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
async def test_group_plugin_does_not_list_or_handle_robot_invitations(host):
    _, adapter = host
    adapter.config.settings["group_id"] = ["999"]
    adapter.config.settings["allowed_groups"] = ["999"]
    item = await register(adapter)
    tools = group_tools(cast(Session, object()), {"request_managers": "123", "allowed_callers": "123", "write_tools_enabled": True})
    adapter._loop = asyncio.get_running_loop()
    with bind_call_origin(origin(chat_type="GroupMessage", chat_id="999")):
        listing = await asyncio.to_thread(next(tool for tool in tools if tool.tool_name == "group_admin_list_group_requests").execute)
        handled = await asyncio.to_thread(next(tool for tool in tools if tool.tool_name == "group_admin_handle_group_request").execute, request_id=item["request_id"], approve=True)
    assert listing["ok"] and not listing["data"]["items"]
    assert not handled["ok"] and handled["error_type"] == "permission_denied"
    adapter._bot.set_group_add_request.assert_not_awaited()

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("archived", [False, True])
async def test_model_auto_execute_still_requires_approval_for_archived_invites(host, monkeypatch, archived):
    backend, adapter = host
    now = time.time()
    clock = [now]
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: clock[0])
    await register(adapter)
    clock[0] += 700 if archived else 0
    item = (await backend.account_group_invites("bot", "100", view="all"))["items"][0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    authorization = ModelActionAuthorization({"adapter_id": "bot", "self_id": "100", "tool_name": "account_requests_handle_group_invite",
        "chat_type": "FriendMessage", "chat_id": "123", "actor_id": "123", "session_id": "test-session"}, lambda _: None)
    with bind_model_action_authorization(authorization):
        result = await backend.submit_account_group_invite("bot", "100", {"request_id": item["request_id"], "expected_revision": item["revision"], "approve": True}, actor_kind="model", action_id="model-auto")
    assert result["state"] == ("pending" if archived else "succeeded")
    if archived:
        adapter._bot.set_group_add_request.assert_not_awaited()
        assert (await backend.decide_account_invite("bot", "100", "model-auto", approve=True))["state"] == "succeeded"
    adapter._bot.set_group_add_request.assert_awaited_once()

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,administrator,approve,archived,expected", [
    (True, "321", True, False, "succeeded"),
    (False, "321", True, False, "pending"),
    (True, "123", True, False, "pending"),
    (True, "321", False, False, "pending"),
    (True, "321", True, True, "pending"),
])
async def test_only_recent_administrator_invitation_acceptance_bypasses_approval(
    invitation_runtime, monkeypatch, enabled, administrator, approve, archived, expected,
):
    backend, adapter, tool, config_manager = invitation_runtime
    backend.administrator_service.apply([{"id": "admins", "name": "管理员", "members": [
        {"platform_id": "bot", "user_id": administrator}], "plugin_scope": {"mode": "selected", "included": ["account_requests"]}}])
    backend._adapter_mgr.administrator_service = backend.administrator_service
    clock = [time.time()]
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: clock[0])
    await register(adapter)
    clock[0] += 700 if archived else 0
    monkeypatch.setattr(time, "time", lambda: clock[0])
    item = (await backend.account_group_invites("bot", "100", view="all"))["items"][0]
    config = getattr(tool, "config")
    config["administrator_invites_auto_approve"] = enabled
    config_manager.update("assistant", {"plugins": [{"name": "account_requests", "enabled": True, "config": config}]})
    adapter.account_request_handler = lambda params: backend.submit_account_group_invite(
        "bot", "100", params, actor_kind="model", action_id="administrator-invitation",
    )
    with bind_call_origin(origin()):
        result = await tool.execute(request_id=item["request_id"], expected_revision=item["revision"], approve=approve)
    assert result["ok"] and result["data"]["state"] == expected
    if expected == "succeeded":
        adapter._bot.set_group_add_request.assert_awaited_once_with(flag="invite-secret", sub_type="invite", approve=True, reason="")
    else:
        adapter._bot.set_group_add_request.assert_not_awaited()

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["administrator", "excluded_plugin", "denied_override", "config", "archived"])
async def test_administrator_invitation_exemption_is_rechecked_before_platform_call(invitation_runtime, monkeypatch, revocation):
    backend, adapter, tool, config_manager = invitation_runtime
    group = {"id": "admins", "name": "管理员", "members": [{"platform_id": "bot", "user_id": "321"}],
             "plugin_scope": {"mode": "selected", "included": ["account_requests"]}}
    backend.administrator_service.apply([group])
    item = await register(adapter)
    adapter.account_request_handler = lambda params: backend.submit_account_group_invite("bot", "100", params, actor_kind="model")
    original = adapter.admin._call

    async def revoke_before_send(action, **params):
        if revocation == "administrator":
            backend.administrator_service.apply([])
        elif revocation == "excluded_plugin":
            backend.administrator_service.apply([{**group, "plugin_scope": {"mode": "selected", "included": ["group_admin"]}}])
        elif revocation == "denied_override":
            backend.administrator_service.apply([group], [{"id": "deny", "platform_id": "bot", "user_id": "321",
                                                           "deny": ["account_requests"]}])
        elif revocation == "config":
            config_manager.update("assistant", {"plugins": [{"name": "account_requests", "enabled": True,
                "config": {**getattr(tool, "config"), "administrator_invites_auto_approve": False}}]})
        else:
            later = time.time() + 700
            monkeypatch.setattr(time, "time", lambda: later)
        return await original(action, **params)

    monkeypatch.setattr(adapter.admin, "_call", revoke_before_send)
    with bind_call_origin(origin()):
        result = await tool.execute(request_id=item["request_id"], approve=True)
    assert result["ok"] and result["data"]["state"] == "failed"
    assert result["data"]["result"]["reason"] == "model_permission_revoked"
    adapter._bot.set_group_add_request.assert_not_awaited()

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
async def test_timeout_becomes_unknown_and_duplicate_does_not_replay(host):
    backend, adapter = host
    item = await register(adapter)
    adapter._bot.set_group_add_request.side_effect = asyncio.TimeoutError
    params = {"request_id": item["request_id"], "approve": True}
    result = await backend.submit_account_group_invite("bot", "100", params, actor_kind="panel", action_id="timeout-invite")
    assert result["state"] == "unknown"
    assert await backend.submit_account_group_invite("bot", "100", params, actor_kind="panel", action_id="timeout-invite") == result
    with pytest.raises(LookupError):
        await backend.submit_account_group_invite("bot", "100", params, actor_kind="panel", action_id="new-invite")
    adapter._bot.set_group_add_request.assert_awaited_once()

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
async def test_concurrent_panel_submission_has_one_platform_call(host):
    backend, adapter = host
    item = await register(adapter)
    params = {"request_id": item["request_id"], "approve": True}
    results = await asyncio.gather(*(backend.submit_account_group_invite("bot", "100", params, actor_kind="panel", action_id=f"concurrent-{i}") for i in range(2)), return_exceptions=True)
    assert sum(isinstance(result, dict) and result["state"] == "succeeded" for result in results) == 1
    adapter._bot.set_group_add_request.assert_awaited_once()

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("sub_type", ["add", "invite"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_new_group_request_with_reused_credential_survives_restart(host, tmp_path, monkeypatch, sub_type, legacy):
    _, adapter = host
    clock = [1000.0]
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: clock[0])
    registry = adapter.request_flags
    await registry.register("group", "reused-group-secret", self_id="100", group_id="999", sub_type=sub_type, user_id="321", event_time=None if legacy else 990)
    old = (await registry.list_requests("group", self_id="100"))["items"][0]
    clock[0] = 1700
    assert (await registry.list_requests("group", self_id="100", view="archived"))["items"][0]["can_handle"]
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    registry = adapter.request_flags
    await registry.register("group", "reused-group-secret", self_id="100", group_id="999", sub_type=sub_type, user_id="321", event_time=1690)
    new = (await registry.list_requests("group", self_id="100"))["items"][0]
    assert new["request_id"] != old["request_id"] and new["received_at"] == 1700 and new["requested_at"] == 1690
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    registry = adapter.request_flags
    assert (await registry.list_requests("group", self_id="100"))["items"][0]["request_id"] == new["request_id"]
    history = (await registry.list_requests("group", self_id="100", view="archived"))["items"][0]
    assert not history["can_handle"] and "更新申请" in history["handling_reason"]
    with pytest.raises(LookupError, match="已有更新记录"):
        await registry.resolve_request("group", old["request_id"], self_id="100", expected_revision=history["revision"], allow_archived=True)

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
async def test_group_adapter_uses_platform_time_and_does_not_renew_replays(host, monkeypatch):
    _, adapter = host
    clock = [1000.0]
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: clock[0])
    event = {"self_id": 100, "request_type": "group", "sub_type": "invite", "flag": "reused-group-secret", "group_id": 999, "user_id": 321, "time": 990}
    await adapter._register_request_flag(event)
    old = (await adapter.request_flags.list_requests("group", self_id="100"))["items"][0]
    clock[0] = 1100
    await adapter._register_request_flag({**event, "time": 1090})
    new = (await adapter.request_flags.list_requests("group", self_id="100"))["items"][0]
    assert new["request_id"] != old["request_id"] and new["requested_at"] == 1090
    clock[0] = 1200
    for timestamp in (990, 1090, None, True):
        await adapter._register_request_flag({**event, "time": timestamp})
    current = (await adapter.request_flags.list_requests("group", self_id="100"))["items"][0]
    assert current["request_id"] == new["request_id"] and current["received_at"] == 1100 and current["expires_at"] == 1700

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
async def test_old_model_approval_cannot_handle_reissued_invite_but_new_approval_can(host, monkeypatch):
    backend, adapter = host
    clock = [time.time()]
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: clock[0])
    await adapter.request_flags.register("group", "reused-group-secret", self_id="100", group_id="999", sub_type="invite", user_id="321", event_time=int(clock[0]) - 10)
    old = (await backend.account_group_invites("bot", "100"))["items"][0]
    authorization = ModelActionAuthorization({"adapter_id": "bot", "self_id": "100", "tool_name": "account_requests_handle_group_invite",
        "chat_type": "FriendMessage", "chat_id": "123", "actor_id": "123", "session_id": "test-session"}, lambda _: None, approval_required=True)
    with bind_model_action_authorization(authorization):
        pending = await backend.submit_account_group_invite("bot", "100", {"request_id": old["request_id"], "expected_revision": old["revision"], "approve": True}, actor_kind="model", action_id="old-model-invite")
    assert pending["state"] == "pending"
    clock[0] += 100
    await adapter.request_flags.register("group", "reused-group-secret", self_id="100", group_id="999", sub_type="invite", user_id="321", event_time=int(clock[0]) - 10)
    new = (await backend.account_group_invites("bot", "100"))["items"][0]
    assert new["request_id"] != old["request_id"]
    stale = await backend.decide_account_invite("bot", "100", "old-model-invite", approve=True)
    assert stale["state"] == "failed"
    adapter._bot.set_group_add_request.assert_not_awaited()
    with bind_model_action_authorization(authorization):
        fresh = await backend.submit_account_group_invite("bot", "100", {"request_id": new["request_id"], "expected_revision": new["revision"], "approve": True}, actor_kind="model", action_id="new-model-invite")
    assert fresh["state"] == "pending"
    assert (await backend.decide_account_invite("bot", "100", "new-model-invite", approve=True))["state"] == "succeeded"
    adapter._bot.set_group_add_request.assert_awaited_once_with(flag="reused-group-secret", sub_type="invite", approve=True, reason="")
    history = (await backend.account_group_invites("bot", "100", view="all"))["items"]
    assert next(item for item in history if item["request_id"] == new["request_id"])["decision"] == "accepted"
    assert next(item for item in history if item["request_id"] == old["request_id"])["decision"] is None

@pytest.mark.usefixtures('restore_adapter_manager')
@pytest.mark.asyncio
async def test_new_invite_during_protocol_wait_blocks_old_call_and_preserves_new_request(host, monkeypatch):
    backend, adapter = host
    clock = [time.time()]
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: clock[0])
    await adapter.request_flags.register("group", "reused-group-secret", self_id="100", group_id="999", sub_type="invite", user_id="321", event_time=int(clock[0]) - 10)
    old = (await backend.account_group_invites("bot", "100"))["items"][0]
    original = adapter.admin._call

    async def reissued(action, **params):
        clock[0] += 100
        await adapter.request_flags.register("group", "reused-group-secret", self_id="100", group_id="999", sub_type="invite", user_id="321", event_time=int(clock[0]) - 10)
        return await original(action, **params)

    monkeypatch.setattr(adapter.admin, "_call", reissued)
    result = await backend.submit_account_group_invite("bot", "100", {"request_id": old["request_id"], "approve": True}, actor_kind="panel")
    assert result["state"] == "failed" and result["result"]["reason"] == "PermissionError"
    adapter._bot.set_group_add_request.assert_not_awaited()
    new = (await backend.account_group_invites("bot", "100"))["items"][0]
    assert new["request_id"] != old["request_id"] and new["can_handle"]
    row = await adapter.request_flags.resolve_request("group", new["request_id"], self_id="100")
    assert adapter.request_flags.ledger.lookup("bot", "100", "group", "reused-group-secret", identity_digest=row["digest"])["state"] == "available"

@pytest.mark.usefixtures('restore_manager')
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

@pytest.mark.usefixtures('restore_manager')
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

@pytest.mark.usefixtures('restore_manager')
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

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_self_deletion_request_needs_approval_and_rechecks_protection():
    adapter = _setup_adapter()
    await incoming(adapter)
    adapter.client_self_id = adapter.bot_self_id
    delete = tool("friend_manager_delete_friend", request_managers="", write_callers="", delete_friend_enabled=True)
    adapter._bot.get_friend_list.return_value = [{"user_id": 321, "nickname": "本人", "remark": ""}]
    with bind_call_origin(_origin(chat_type="FriendMessage", actor="321")):
        denied = await delete.execute(user_id="999")
        assert denied["error"]["code"] == "permission_denied"
        result = await delete.execute()
        assert result["ok"] and result["data"]["state"] == "pending"
        adapter._bot.delete_friend.assert_not_awaited()
    # 审批前工具被停用 → 不能执行
    delete.disable()
    blocked = await cast(FriendService, adapter.friend_host).decide("10000", result["data"]["action_id"], True)
    assert blocked["state"] == "failed"
    delete.enable()
    # 保护名单阻止再次提交
    delete.config.update(protected_friend_ids="321")
    with bind_call_origin(_origin(chat_type="FriendMessage", actor="321")):
        assert not (await delete.execute())["ok"]
    adapter._bot.delete_friend.assert_not_awaited()
    # 解除保护后可正常提交并获批执行
    delete.config.update(protected_friend_ids="")
    with bind_call_origin(_origin(chat_type="FriendMessage", actor="321")):
        fresh = await delete.execute()
    approved = await cast(FriendService, adapter.friend_host).decide("10000", fresh["data"]["action_id"], True)
    assert approved["state"] == "succeeded"
    adapter._bot.delete_friend.assert_awaited_once_with(user_id=321)

@pytest.mark.usefixtures('restore_manager')
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

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_send_request_reports_unsupported_and_does_not_fake_inbound_acceptance():
    adapter = _setup_adapter()
    await incoming(adapter)
    send = tool("friend_manager_send_request", request_managers="", write_callers="", send_request_enabled=True)
    with bind_call_origin(_origin(chat_type="FriendMessage", actor="321")):
        result = await send.execute()
        assert result["error"]["code"] == "unsupported"
    adapter._bot.set_friend_add_request.assert_not_awaited()

@pytest.mark.usefixtures('restore_manager')
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

@pytest.mark.usefixtures('restore_manager')
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

class GenericFriends:
    """使用非数字账号的通用适配器替身"""

    def __init__(self):
        self.config = SimpleNamespace(enable=True, settings={})
        self.account = "bot@example.test"
        self.generation = 1
        self.items = [{"user_id": "alice", "nickname": "同名", "remark": "甲"},
                      {"user_id": "bob", "nickname": "同名", "remark": "乙"},
                      {"user_id": "manager", "nickname": "管理者", "remark": ""}]
        self.complete = True
        self.delete_calls = 0
        self.handle_calls = []
        self.friend_requests = AsyncMock(return_value={"items": [], "has_more": False, "next_cursor": None})

    def friend_account(self):
        return self.account

    def friend_generation(self):
        return self.generation

    def friend_capabilities(self):
        return {key: {"state": "supported"} for key in ("list_friends", "list_requests", "handle_request", "delete_friend")}

    async def friend_list(self, account):
        return {"items": deepcopy(self.items), "complete": self.complete}

    async def friend_delete(self, account, user_id):
        self.delete_calls += 1
        self.items = [row for row in self.items if row["user_id"] != user_id]

    async def friend_handle(self, account, request_id, approve, remark):
        self.handle_calls.append((request_id, approve, remark))

def setup(tmp_path):
    adapter: Any = GenericFriends()
    active = [adapter]
    permission = {"enabled": True, "version": "initial"}
    def check(source, target):
        source.verify(target)
        if not permission["enabled"]:
            raise PermissionError("已撤权")
        return permission["version"]
    host = FriendService("generic", tmp_path / "platform.db", lambda: active[0], check, lambda: ["manager"])
    source = ModelActionAuthorization({"adapter_id": "generic", "self_id": adapter.account}, lambda target: None)
    return host, adapter, source, active, permission

@pytest.mark.asyncio
async def test_search_ambiguity_pagination_and_scope(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    first = await host.list_friends(adapter.account, actor="manager", query="同名", limit=1)
    assert first["ambiguous"] and first["has_more"] and first["coverage"]["complete"]
    assert first["items"][0]["matched_by"] == ["nickname"]
    second = await host.list_friends(adapter.account, actor="manager", query="同名", limit=1, cursor=first["next_cursor"])
    assert second["items"][0]["user_id"] == "bob" and not second["has_more"]
    for actor, query in [("other", "同名"), ("manager", "乙")]:
        with pytest.raises(FriendError, match="当前账号或查询"):
            await host.list_friends(adapter.account, actor=actor, query=query, cursor=first["next_cursor"])
    adapter.generation += 1
    with pytest.raises(FriendError) as caught:
        await host.list_friends(adapter.account, actor="manager", query="同名", cursor=first["next_cursor"])
    assert caught.value.code == "cursor_expired"

@pytest.mark.asyncio
async def test_incomplete_directory_cannot_prove_absence(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    adapter.complete = False
    found = await host.list_friends(adapter.account, actor="manager", query="alice")
    assert not found["coverage"]["complete"] and found["items"][0]["exact"]
    with pytest.raises(FriendError) as caught:
        await host.submit(adapter.account, "delete-absent", "delete_friend", {"user_id": "missing"}, actor="panel")
    assert caught.value.code == "incomplete_directory" and adapter.delete_calls == 0

@pytest.mark.asyncio
async def test_human_deletion_no_plugin_approval_idempotency_and_history_kept(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    store = await host.store()
    with store.connection() as conn:
        conn.execute("INSERT INTO session_config_overrides(session_id,namespace,config_json,updated_at) VALUES(?,?,?,?)",
                     ("history", "history", '{}', 1))
    record = await host.submit(adapter.account, "human-delete", "delete_friend", {"user_id": "alice"}, actor="panel")
    assert record["state"] == "succeeded" and record["result"]["verification"] == "confirmed"
    assert record["target"]["nickname"] == "同名"
    same = await host.submit(adapter.account, "human-delete", "delete_friend", {"user_id": "alice"}, actor="panel")
    assert same == record and adapter.delete_calls == 1
    with pytest.raises(FriendError) as caught:
        await host.submit(adapter.account, "human-delete", "delete_friend", {"user_id": "bob"}, actor="panel")
    assert caught.value.code == "action_conflict"
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_config_overrides").fetchone()[0] == 1

@pytest.mark.asyncio
async def test_model_deletion_requires_approval_and_concurrent_decision_executes_once(tmp_path):
    host, adapter, source, _, _ = setup(tmp_path)
    record = await host.submit(adapter.account, "model-delete", "delete_friend", {"user_id": "alice"}, actor="model", source=source)
    assert record["state"] == "pending" and adapter.delete_calls == 0
    results = await asyncio.gather(host.decide(adapter.account, "model-delete", True), host.decide(adapter.account, "model-delete", True))
    assert adapter.delete_calls == 1 and any(row["state"] == "succeeded" for row in results)
    assert (await host.decide(adapter.account, "model-delete", True))["state"] == "succeeded"

@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["permission", "account", "protection"])
async def test_pending_model_action_rechecks_every_boundary(tmp_path, revocation):
    host, adapter, source, active, permission = setup(tmp_path)
    await host.submit(adapter.account, "pending", "delete_friend", {"user_id": "alice"}, actor="model", source=source)
    if revocation == "permission":
        permission["enabled"] = False
    elif revocation == "fingerprint":
        permission["version"] = "changed"
    elif revocation == "account":
        adapter.account = "new-bot"
    elif revocation == "connection":
        adapter.generation += 1
    elif revocation == "replacement":
        active[0] = GenericFriends()
    else:
        await host.policy(adapter.account, ["alice"])
    if revocation == "account":
        with pytest.raises(FriendError):
            await host.decide("bot@example.test", "pending", True)
    else:
        assert (await host.decide(adapter.account, "pending", True))["state"] == "failed"
    assert adapter.delete_calls == 0

@pytest.mark.asyncio
async def test_manual_protection_rejection_and_pending_rejection(tmp_path):
    host, adapter, source, _, _ = setup(tmp_path)
    for uid in ["manager", cast(str, adapter.account)]:
        with pytest.raises(FriendError) as caught:
            await host.submit(adapter.account, uid, "delete_friend", {"user_id": uid}, actor="panel")
        assert caught.value.code == "protected_friend"
    await host.policy(adapter.account, ["alice"])
    with pytest.raises(FriendError):
        await host.submit(adapter.account, "protected", "delete_friend", {"user_id": "alice"}, actor="panel")
    await host.submit(adapter.account, "reject", "delete_friend", {"user_id": "bob"}, actor="model", source=source)
    assert (await host.decide(adapter.account, "reject", False))["state"] == "rejected" and adapter.delete_calls == 0

@pytest.mark.asyncio
async def test_unknown_blocks_replay_even_with_new_id_and_restart_recovers(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    adapter.friend_delete = AsyncMock(side_effect=FriendError("unconfirmed", "超时"))
    record = await host.submit(adapter.account, "unknown", "delete_friend", {"user_id": "alice"}, actor="panel")
    assert record["state"] == "unknown"
    assert (await host.submit(adapter.account, "unknown", "delete_friend", {"user_id": "alice"}, actor="panel"))["state"] == "unknown"
    with pytest.raises(FriendError) as caught:
        await host.submit(adapter.account, "other-id", "delete_friend", {"user_id": "alice"}, actor="panel")
    assert caught.value.code == "unresolved_action"
    adapter.friend_delete.assert_awaited_once()
    store = await host.store()
    store.register(adapter.account, "interrupted", "handle_request", {"request_id": "request-1", "approve": True}, "panel")
    store.transition(adapter.account, "interrupted", "ready", "executing")
    recovered = FriendStore(tmp_path / "platform.db")
    assert recovered.get(adapter.account, "interrupted")["state"] == "unknown"

@pytest.mark.asyncio
async def test_cancellation_settles_unknown_and_no_replay(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    entered = asyncio.Event()
    async def wait(*args):
        entered.set()
        await asyncio.Event().wait()
    adapter.friend_delete = wait
    task = asyncio.create_task(host.submit(adapter.account, "cancel", "delete_friend", {"user_id": "alice"}, actor="panel"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await host.actions(adapter.account))["items"][0]["state"] == "unknown"

@pytest.mark.asyncio
async def test_request_handling_human_direct_and_strict_parameters(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    accepted = await host.submit(adapter.account, "accept", "handle_request", {"request_id": "request-1", "approve": True, "remark": "备注"}, actor="panel")
    assert accepted["state"] == "succeeded" and adapter.handle_calls == [("request-1", True, "备注")]
    for params in [{"request_id": "request-1", "approve": "false"}, {"request_id": "request-1", "approve": False, "remark": "无效"},
                   {"request_id": "request-1", "approve": True, "flag": "secret"}, {"request_id": "request-1", "approve": True, "remark": "x" * 61}]:
        with pytest.raises(FriendError):
            await host.submit(adapter.account, "invalid", "handle_request", params, actor="panel")
    assert len(adapter.handle_calls) == 1

@pytest.mark.asyncio
async def test_request_cursor_bound_to_actor_and_connection(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    adapter.friend_requests.return_value = {"items": [], "has_more": True, "next_cursor": "native-position"}
    result = await host.requests(adapter.account, actor="manager")
    assert result["next_cursor"] != "native-position"
    with pytest.raises(FriendError):
        await host.requests(adapter.account, cursor=result["next_cursor"], actor="other")
    await host.requests(adapter.account, cursor=result["next_cursor"], actor="manager")
    assert adapter.friend_requests.await_args.args[2] == "native-position"
    adapter.generation += 1
    with pytest.raises(FriendError):
        await host.requests(adapter.account, cursor=result["next_cursor"], actor="manager")

@pytest.mark.parametrize("old_enabled,write,callers", [(True, True, "manager"), (False, True, "manager"), (True, False, "manager"), (True, True, "other")])
def test_migration_conservative_idempotent_and_removes_old_entries(tmp_path, monkeypatch, old_enabled, write, callers):
    from satrap.edictum.plugin_config import PluginConfigManager
    monkeypatch.setattr(PluginConfigManager, "load_global_explicit", lambda *args: {})
    catalog = PluginCatalog(user_dir=tmp_path / "plugins")
    value = [{"name": "group_admin", "enabled": old_enabled, "config": {"request_managers": "manager", "allowed_callers": callers,
              "write_tools_enabled": write}, "capabilities": {"tools": {"group_admin_list_friend_requests": True, "group_admin_handle_friend_request": False}}}]
    before = deepcopy(value)
    specs = {spec.name: spec for spec in parse_plugin_specs(value, catalog)}
    assert value == before
    friend = specs["friend_manager"]
    assert friend.enabled == old_enabled
    assert friend.capabilities["tools"]["friend_manager_list_requests"] == old_enabled
    assert not friend.capabilities["tools"]["friend_manager_handle_request"]
    assert not friend.capabilities["tools"]["friend_manager_list_friends"]
    assert not friend.capabilities["tools"]["friend_manager_find_friends"]
    assert not friend.capabilities["tools"]["friend_manager_delete_friend"]
    assert "request_handling_enabled" not in friend.config
    canonical = [spec.to_config() for spec in specs.values()]
    assert [spec.to_config() for spec in parse_plugin_specs(canonical, catalog)] == canonical

def test_migration_defaults_preserve_empty_manager_and_implicit_globals(tmp_path, monkeypatch):
    from satrap.edictum.plugin_config import PluginConfigManager

    monkeypatch.setattr(PluginConfigManager, "load_global_explicit", lambda *args: {})
    catalog = PluginCatalog(user_dir=tmp_path / "plugins")
    source = [{"name": "group_admin", "config": {"request_managers": "manager", "write_tools_enabled": True},
               "capabilities": {"tools": {"group_admin_list_friend_requests": True}}},
              {"name": "friend_manager", "config": {"managers": "", "write_callers": "manager", "request_handling_enabled": True},
               "capabilities": {"tools": {"friend_manager_delete_friend": False}}}]
    specs = {spec.name: spec for spec in parse_plugin_specs(source, catalog)}
    assert specs["friend_manager"].config["managers"] == ""
    assert not specs["friend_manager"].capabilities["tools"]["friend_manager_handle_request"]
    assert not specs["friend_manager"].capabilities["tools"]["friend_manager_delete_friend"]

    # 隐式旧默认: 从 group_admin 全局显式配置迁移到 friend_manager
    monkeypatch.setattr(PluginConfigManager, "load_global_explicit", lambda self, name, schema:
                        {"request_managers": "manager", "write_tools_enabled": True} if name == "group_admin" else {})
    implicit = {spec.name: spec for spec in parse_plugin_specs(["group_admin"], catalog)}
    assert implicit["friend_manager"].config["managers"] == "manager"
    assert implicit["friend_manager"].config["write_callers"] == "manager"
    assert implicit["friend_manager"].capabilities["tools"]["friend_manager_handle_request"]
    assert not implicit["friend_manager"].capabilities["tools"]["friend_manager_delete_friend"]

def test_v7_schema_migration_preserves_history_and_checks_missing_friend_tables(tmp_path):
    from satrap.core.config.platform_schema import ensure_platform_tables, PLATFORM_SCHEMA_VERSION
    database = tmp_path / "platform.db"
    with sqlite3.connect(database) as conn:
        ensure_platform_tables(conn)
        conn.execute("INSERT INTO session_config_overrides(session_id, namespace, config_json, updated_at) VALUES('s','prompt','{}',1)")
        conn.execute("DROP TABLE friend_actions")
        conn.execute("DROP TABLE friend_policies")
        for table in ("memory_refs", "memory_proposals", "memory_operations", "memory_audit", "memories",
                      "group_chat_reminder_attempts", "group_chat_reminders", "reminder_operations"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("PRAGMA user_version=7")
        ensure_platform_tables(conn)
        assert conn.execute("SELECT config_json FROM session_config_overrides WHERE session_id='s'").fetchone()[0] == '{}'
        assert conn.execute("PRAGMA user_version").fetchone()[0] == PLATFORM_SCHEMA_VERSION
        conn.execute("DROP TABLE friend_actions")
        with pytest.raises(RuntimeError, match="friend_actions"):
            ensure_platform_tables(conn)

@pytest.mark.asyncio
async def test_management_http_works_without_plugin_and_refuses_account_mismatch(tmp_path):
    from satrap.core.backend.http_api import BackendHTTPServer
    host, adapter, _, _, _ = setup(tmp_path)
    server = object.__new__(BackendHTTPServer)
    server.backend = SimpleNamespace(friend_service=lambda adapter_id: host)
    status, info = await server._route_friends("GET", "/api/platforms/generic/friends/info", b"")
    assert status == 200 and info["current_account"] == adapter.account
    status, page = await server._route_friends("GET", f"/api/platforms/generic/friends?account={adapter.account}&q=同名", b"")
    assert status == 200 and len(page["items"]) == 2
    status, _ = await server._route_friends("POST", "/api/platforms/generic/friends/actions", json.dumps({
        "expected_self_id": "old-account", "action_id": "id", "action_type": "delete_friend", "params": {"user_id": "alice"}}).encode())
    assert status == 409 and adapter.delete_calls == 0
    status, result = await server._route_friends("POST", "/api/platforms/generic/friends/actions", json.dumps({
        "expected_self_id": adapter.account, "action_id": "id", "action_type": "delete_friend", "params": {"user_id": "alice"}}).encode())
    assert status == 200 and result["state"] == "succeeded" and adapter.delete_calls == 1
