"""群工具职责迁移, 插件独立性与机器人自身群昵称授权"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
from aiocqhttp.exceptions import ActionFailed
import asyncio
import ast

import pytest

from satrap.core.call_context import bind_call_origin
from satrap.core.group_chat.types import GroupChatLimits
from satrap.core.platform import current_adapter_manager, set_current_adapter_manager
from satrap.edictum import SimpleSession, AsyncSimpleSession
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_spec import parse_plugin_specs
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.expend.plugins.group_chat.tools import get_tools, DEFINITIONS
from satrap.expend.plugins.group_admin.tools import _DEFINITIONS
from .test_group_chat_service import _setup


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def restore_manager():
    previous = current_adapter_manager()
    yield
    set_current_adapter_manager(previous)


def test_old_tools_only_exist_in_config_migration_and_plugins_do_not_import_each_other():
    assert not set(_DEFINITIONS) & {
        "group_admin_list_groups", "group_admin_get_group_info", "group_admin_list_members",
        "group_admin_get_member", "group_admin_get_message", "group_admin_set_card",
    }
    assert "group_admin_set_group_nickname" in _DEFINITIONS
    for plugin, other in (("group_chat", "group_admin"), ("group_admin", "group_chat")):
        for path in (ROOT / "satrap/expend/plugins" / plugin).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or "").startswith(f"satrap.expend.plugins.{other}")
                elif isinstance(node, ast.Import):
                    assert all(not item.name.startswith(f"satrap.expend.plugins.{other}") for item in node.names)


@pytest.mark.parametrize("enabled", [False, True])
def test_old_query_and_nickname_states_migrate_without_enabling_extra_capabilities(tmp_path, enabled):
    catalog = PluginCatalog(user_dir=tmp_path)
    source = [{"name": "group_admin", "enabled": enabled, "config": {"allowed_groups": "456"},
               "capabilities": {"tools": {"group_admin_set_card": False, "group_admin_get_group_info": True,
                                           "group_admin_get_member": False, "group_admin_list_groups": True}}}]
    original = deepcopy(source)
    specs = {spec.name: spec for spec in parse_plugin_specs(source, catalog)}
    assert source == original
    assert specs["group_admin"].capabilities["tools"]["group_admin_set_group_nickname"] is False
    chat = specs["group_chat"]
    assert chat.capabilities["tools"]["group_chat_get_group_info"] is enabled
    assert chat.capabilities["tools"]["group_chat_get_member"] is False
    assert not chat.capabilities["tools"]["group_chat_reply"]
    assert not any(chat.capabilities["skills"].values())
    assert chat.config["allowed_groups"] == "456"
    canonical = [spec.to_config() for spec in specs.values()]
    assert [spec.to_config() for spec in parse_plugin_specs(canonical, catalog)] == canonical


def test_migration_preserves_explicit_new_switch_and_rejects_permission_conflict(tmp_path):
    catalog = PluginCatalog(user_dir=tmp_path)
    source = [{"name": "group_admin", "config": {"allowed_groups": "456"}, "capabilities": {"tools": {
        "group_admin_set_card": True, "group_admin_set_group_nickname": False, "group_admin_get_member": False}}},
              {"name": "group_chat", "config": {"allowed_groups": "456\n789"}}]
    specs = {spec.name: spec for spec in parse_plugin_specs(source, catalog)}
    assert not specs["group_admin"].capabilities["tools"]["group_admin_set_group_nickname"]
    assert not specs["group_chat"].capabilities["tools"]["group_chat_get_member"]
    assert specs["group_chat"].config["allowed_groups"] == "456"
    source[1]["config"]["allowed_groups"] = "789"
    with pytest.raises(ValueError, match="无交集"):
        parse_plugin_specs(source, catalog)


@pytest.mark.asyncio
async def test_chat_queries_group_info_members_roles_and_pagination_without_admin_plugin(tmp_path):
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_group_info.return_value = {"group_id": 456, "group_name": "测试群", "member_count": 3, "secret": "hidden"}
    adapter._bot.get_group_member_list.return_value = [
        {"group_id": 456, "user_id": uid, "nickname": f"成员{uid}", "card": "同名", "role": role}
        for uid, role in ((123, "owner"), (321, "admin"), (789, "member"))
    ]
    with bind_call_origin(origin):
        info = await service.execute("group_chat_get_group_info", {})
        first = await service.execute("group_chat_list_members", {"limit": 1})
        second = await service.execute("group_chat_list_members", {"cursor": first["next_cursor"]})
        wrong_cursor = await service.execute("group_chat_find_members", {"query": "同名", "cursor": first["next_cursor"]})
        cross_group = await service.execute("group_chat_get_group_info", {"group_id": "999"})
        adapter._bot.get_group_info.return_value = {"group_id": 999}
        bad_info = await service.execute("group_chat_get_group_info", {})
    assert info["item"]["group_name"] == "测试群" and "secret" not in info["item"]
    assert first["items"][0]["role"] == "owner" and first["has_more"]
    assert [item["user_id"] for item in second["items"]] == ["321", "789"]
    assert not second["has_more"] and adapter._bot.get_group_member_list.await_count == 1
    assert not wrong_cursor["ok"] and not cross_group["ok"]
    assert bad_info["error"]["code"] == "unverified_target"


@pytest.mark.asyncio
async def test_group_list_is_private_authorized_scoped_and_revalidates_permissions(tmp_path, monkeypatch):
    _, adapter, origin = _setup(tmp_path)
    session = AsyncSimpleSession("group-list", cast(Any, object()), enable_checkpoint=False, db_path=str(tmp_path / "ctx.db"))
    config = {"cross_group_query_callers": "123", "allowed_groups": "456"}
    query = next(tool for tool in get_tools(session, config) if tool.tool_name == "group_chat_list_groups")
    adapter._bot.get_group_list.return_value = [{"group_id": 456, "group_name": "可见群"}, {"group_id": 999, "group_name": "其它群"}]
    with bind_call_origin(origin):
        assert not query.is_available_for_call()
        assert not (await query.execute())["ok"]
    private = replace(origin, chat_type="FriendMessage", chat_id="123")
    with bind_call_origin(replace(private, actor_id="999")):
        assert not query.is_available_for_call()
        assert not (await query.execute())["ok"]
    with bind_call_origin(private):
        assert query.is_available_for_call()
        result = await query.execute()
        assert [item["group_id"] for item in result["items"]] == ["456"]
        original = adapter.group_chat_groups
        async def revoke(scope):
            result = await original(scope)
            query.config["allowed_groups"] = "999"
            return result
        monkeypatch.setattr(adapter, "group_chat_groups", revoke)
        denied = await query.execute()
        assert not denied["ok"] and denied["error"]["code"] == "stale_call"


async def runtime(tmp_path: Path, asynchronous: bool, plugin_name: str, plugin_config: dict[str, Any] | None = None):
    from satrap.core.backend.BackendManager import BackendManager, BackendConfig
    from satrap.core.config.group_directory import GroupDirectoryStore
    from satrap.core.platform import PlatformAdapterManager
    from satrap.core.framework.SessionManager import SessionManager
    from satrap.core.framework.providers import EdictumProvider
    from satrap.core.type import SessionConfig
    from satrap.edictum.config import EdictumConfigManager
    from satrap.edictum.registry import create_default_edictum_type_registry

    _, adapter, origin = _setup(tmp_path)
    adapter._loop = asyncio.get_running_loop()
    settings = {"group_management_version": 1}
    adapter.config.settings = settings
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[{
        "id": "ob", "type": "onebot", "session_provider": "edictum", "session_type": "assistant", "settings": settings,
    }]))
    backend._adapter_mgr = cast(PlatformAdapterManager, current_adapter_manager())
    directory = GroupDirectoryStore(backend.platform_db_path("ob"))
    directory.adopt_legacy("10000", settings)
    directory.patch_account("10000", expected_revision=1, mode="all", approval_defaults={"set_group_card": "approval_required"})
    directory.confirm_membership("10000", "456", True)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("10000")
    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config = ({"self_nickname_enabled": True, "nickname_allowed_callers": "123"} if plugin_name == "group_chat"
              else {"write_tools_enabled": True, "allowed_callers": "123"})
    if plugin_config is not None:
        config = plugin_config
    configs.create("assistant", {"edictum_type": "async_simple" if asynchronous else "simple", "model_name": "base",
                                 "plugins": [{"name": plugin_name, "enabled": True, "config": config}]})
    provider = EdictumProvider(configs, registry, default_checkpoint_db=str(backend.platform_db_path("ob")))
    manager = SessionManager(db_path=backend.platform_db_path("ob"), platform_id="ob")
    manager.register_provider(provider)
    cfg = SessionConfig(session_id="nickname-agent", session_type_name="assistant", provider_name="edictum")
    manager.store.upsert(cfg)
    session = provider.create_session(cfg, llm=cast(Any, object()))
    session.plugin_environment = PluginEnvironment("platform", "onebot")
    manager.pool.put("nickname-agent", session, "assistant")
    backend._platform_runtimes["ob"] = cast(Any, (manager, None))
    await provider.prepare_session_async(session)
    ids = iter(f"nickname-action-{i}" for i in range(30))
    adapter.group_action_handler = lambda gid, action, params: backend.submit_group_action(
        "ob", "10000", gid, next(ids), action, params, actor_kind="model")
    return backend, adapter, origin, session, provider, configs


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("plugin_name", ["group_chat", "group_admin"])
@pytest.mark.parametrize("nickname", ["验收群昵称", ""])
async def test_each_plugin_independently_submits_nickname_for_approval_and_executes(tmp_path, asynchronous, plugin_name, nickname):
    backend, adapter, origin, session, provider, _ = await runtime(tmp_path, asynchronous, plugin_name)
    try:
        assert [plugin.name for plugin in session.list_plugins()] == [plugin_name]
        tool = session._wf.tools_manager.tools[f"{plugin_name}_set_group_nickname"]
        assert tool.recovery_policy == "manual"
        arguments = {"nickname": nickname}
        if plugin_name == "group_admin":
            arguments["user_id"] = "321"
        with bind_call_origin(origin):
            result = await tool.execute(**arguments) if asynchronous else await asyncio.to_thread(tool.execute, **arguments)
        assert result["data"]["state"] == "pending"
        adapter._bot.set_group_card.assert_not_awaited()
        action = result["data"]["action_id"]
        record = await backend.decide_group_action("ob", "10000", "456", action, approve=True)
        assert record["state"] == "succeeded"
        adapter._bot.set_group_card.assert_awaited_once_with(group_id=456, user_id=10000 if plugin_name == "group_chat" else 321, card=nickname)
    finally:
        await provider.release_session_async(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["off", "actor", "private", "handler", "unsupported", "bad_nickname"])
async def test_self_nickname_denied_paths_never_write(tmp_path, reason):
    _, adapter, origin, session, provider, _ = await runtime(tmp_path, True, "group_chat")
    try:
        tool = session._wf.tools_manager.tools["group_chat_set_group_nickname"]
        if reason == "off":
            tool.config.pop("self_nickname_enabled")
        elif reason == "actor":
            origin = replace(origin, actor_id="999")
        elif reason == "private":
            origin = replace(origin, chat_type="FriendMessage", chat_id="123")
        elif reason == "handler":
            adapter.group_action_handler = None
        elif reason == "unsupported":
            adapter._capability_states["set_group_card"] = (adapter.connection_generation(), "unsupported")
        with bind_call_origin(origin):
            if reason in {"off", "actor", "private", "unsupported"}:
                assert not tool.is_available_for_call()
            result = await tool.execute(nickname="x" * 61 if reason == "bad_nickname" else "试用")
        assert not result["ok"]
        adapter._bot.set_group_card.assert_not_awaited()
    finally:
        await provider.release_session_async(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, state", [(ActionFailed({"retcode": 100}), "failed"),
                                         (TimeoutError(), "unknown"), (RuntimeError("transport"), "unknown")])
async def test_self_nickname_platform_failure_is_not_success_or_retried(tmp_path, failure, state):
    from satrap.core.config.group_store import GroupConfigConflict

    backend, adapter, origin, session, provider, _ = await runtime(tmp_path, True, "group_chat")
    try:
        tool = session._wf.tools_manager.tools["group_chat_set_group_nickname"]
        with bind_call_origin(origin):
            pending = await tool.execute(nickname="试用")
        adapter._bot.set_group_card.side_effect = failure
        action_id = pending["data"]["action_id"]
        result = await backend.decide_group_action("ob", "10000", "456", action_id, approve=True)
        assert result["state"] == state and result["executed_at"] is not None
        with pytest.raises(GroupConfigConflict):
            await backend.decide_group_action("ob", "10000", "456", action_id, approve=True)
        adapter._bot.set_group_card.assert_awaited_once()
    finally:
        await provider.release_session_async(session)


@pytest.mark.asyncio
async def test_list_groups_filters_platform_range_and_discards_revoked_callers(tmp_path, monkeypatch):
    _, adapter, origin = _setup(tmp_path)
    adapter.config.settings["group_whitelist"] = ["456"]
    adapter._bot.get_group_list.return_value = [{"group_id": 456}, {"group_id": 789}]
    session = AsyncSimpleSession("group-list", cast(Any, object()), enable_checkpoint=False, db_path=str(tmp_path / "ctx.db"))
    tool = next(tool for tool in get_tools(session, {"cross_group_query_callers": "123"}) if tool.tool_name == "group_chat_list_groups")
    origin = replace(origin, chat_type="FriendMessage", chat_id="123")
    with bind_call_origin(origin):
        result = await tool.execute()
        assert [item["group_id"] for item in result["items"]] == ["456"]
        read = adapter.group_chat_groups
        async def revoke(scope):
            snapshot = await read(scope)
            tool.config["cross_group_query_callers"] = "999"
            return snapshot
        monkeypatch.setattr(adapter, "group_chat_groups", revoke)
        result = await tool.execute()
        assert not result["ok"] and result["error"]["code"] == "forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unsupported", "rejected", "timeout", "connection", "account"])
async def test_group_info_read_failure_and_late_results_are_rejected(tmp_path, mode):
    service, adapter, origin = _setup(tmp_path)
    if mode in {"unsupported", "rejected", "timeout"}:
        adapter._bot.get_group_info.side_effect = (TimeoutError() if mode == "timeout"
                                                  else ActionFailed({"retcode": 10002 if mode == "unsupported" else 100}))
    else:
        async def change(**kwargs):
            if mode == "connection":
                adapter._bot = AsyncMock()
            else:
                adapter.bot_self_id = "999"
            return {"group_id": 456, "group_name": "旧结果"}
        adapter._bot.get_group_info.side_effect = change
    with bind_call_origin(origin):
        result = await service.execute("group_chat_get_group_info", {})
    assert not result["ok"] and "item" not in result
    assert result["error"]["code"] == ("unsupported" if mode == "unsupported" else "unavailable" if mode in {"rejected", "timeout"} else "stale_call")


@pytest.mark.asyncio
async def test_member_list_character_budget_paginates_without_skipping(tmp_path):
    service, adapter, origin = _setup(tmp_path)
    adapter._bot.get_group_member_list.return_value = [{"user_id": uid, "nickname": "甲" * 30, "role": "member"} for uid in (123, 321, 789)]
    adapter._bot.get_group_member_info.return_value = {"group_id": 456, "user_id": 123, "role": "owner"}
    limits = GroupChatLimits(text_budget=200)
    items = []
    args = {"limit": 3}
    with bind_call_origin(origin):
        for _ in range(3):
            page = await service.execute("group_chat_list_members", args, limits=limits)
            assert len(page["items"]) == 1 and "ambiguous" not in page
            items.extend(item["user_id"] for item in page["items"])
            args = {"cursor": page["next_cursor"], "limit": 3}
        role = await service.execute("group_chat_get_member", {"user_id": "123"})
    assert items == ["123", "321", "789"] and not page["has_more"]
    assert role["item"]["role"] == "owner"


def test_migration_preserves_global_group_restrictions(tmp_path, monkeypatch):
    from satrap.edictum.plugin_config import PluginConfigManager

    catalog = PluginCatalog(user_dir=tmp_path)
    monkeypatch.setattr("satrap.edictum.plugin_config.CONFIG_DIR", tmp_path / "plugin-configs")
    manager = PluginConfigManager()
    manager.save_global("group_admin", catalog.get("group_admin").config_schema, {"allowed_groups": "456\n789"})
    manager.save_global("group_chat", catalog.get("group_chat").config_schema, {"allowed_groups": "456\n999"})
    specs = {spec.name: spec for spec in parse_plugin_specs([{"name": "group_admin", "capabilities": {
        "tools": {"group_admin_get_group_info": True}}}], catalog)}
    assert specs["group_chat"].config["allowed_groups"] == "456"


@pytest.mark.asyncio
async def test_new_queries_support_another_platform_and_private_route_revocation(tmp_path):
    from satrap.core.call_context import CallOrigin
    from satrap.core.config.platform_messages import MessageScope
    from satrap.core.group_chat.service import GroupChatService
    from satrap.core.group_chat.types import GroupRecord, GroupSnapshot, VerifiedGroup
    from satrap.core.platform import PlatformAdapterManager, PlatformAdapterRegistry, PlatformConfig
    from .test_group_chat_service import _FutureAdapter, NOW

    class QueryAdapter(_FutureAdapter):
        """平台相关的查询和允许群范围由适配器自己提供"""

        def group_chat_capabilities(self):
            result = super().group_chat_capabilities()
            for name in ("group_info", "group_list"):
                result[name] = {"state": "supported", "reason": "implemented"}
            return result

        def group_chat_group_visible(self, group_id):
            return group_id == "room/群"

        async def group_chat_group(self, scope):
            return VerifiedGroup(scope, GroupRecord(scope.chat_id, "另一个平台的群", 2), NOW)

        async def group_chat_groups(self, scope):
            return GroupSnapshot(scope, (GroupRecord("room/群"), GroupRecord("room/不可见")), NOW, True)

    registry = PlatformAdapterRegistry()
    registry.register("future", QueryAdapter)
    manager = PlatformAdapterManager(registry)
    adapter = manager.add_adapter(PlatformConfig(id="future", type="future"))
    adapter.client_self_id = "robot:一"
    set_current_adapter_manager(manager)
    origin = CallOrigin("future", "robot:一", "RoomEvent", "room/群", "user:甲", "message:甲", "request",
                        conversation_kind="group", conversation_id="room/群")
    service = GroupChatService()
    with bind_call_origin(origin):
        group = await service.execute("group_chat_get_group_info", {})
        assert group["item"]["group_id"] == "room/群" and group["item"]["member_count"] == 2
    private = replace(origin, chat_type="DirectEvent", chat_id="user:甲", conversation_kind="private", conversation_id="user:甲")
    with bind_call_origin(private):
        groups = await service.execute("group_chat_list_groups", {}, authorize=lambda _: None)
        assert [item["group_id"] for item in groups["items"]] == ["room/群"]
        adapter._agent_route_memory[("robot:一", "private", "user:甲")] = ({}, 2)
        stale = await service.execute("group_chat_list_groups", {}, authorize=lambda _: None)
        assert stale["error"]["code"] == "stale_call"
    with bind_call_origin(replace(private, self_id="robot:二")):
        wrong_account = await service.execute("group_chat_list_groups", {}, authorize=lambda _: None)
        assert not wrong_account["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["live_config", "saved_config", "plugin", "actor", "account"])
async def test_self_nickname_rejects_forged_target_and_rechecks_revocation(tmp_path, revocation):
    backend, adapter, origin, session, provider, configs = await runtime(tmp_path, True, "group_chat")
    try:
        tool = session._wf.tools_manager.tools["group_chat_set_group_nickname"]
        with bind_call_origin(origin):
            assert not (await tool.execute(nickname="伪造", user_id="321"))["ok"]
            assert not (await tool.execute(nickname="伪造", group_id="999"))["ok"]
            pending = await tool.execute(nickname="")
        assert pending["data"]["state"] == "pending"
        if revocation == "live_config":
            tool.config["self_nickname_enabled"] = False
        elif revocation == "saved_config":
            configs.update("assistant", {"plugins": [{"name": "group_chat", "enabled": True,
                                                       "config": {"self_nickname_enabled": False}}]})
        elif revocation == "plugin":
            await session.disable_plugin("group_chat")
        elif revocation == "actor":
            tool.config["nickname_allowed_callers"] = "999"
        else:
            adapter.bot_self_id = "other"
        if revocation == "account":
            with pytest.raises(ValueError, match="机器人账号已变化"):
                await backend.decide_group_action("ob", "10000", "456", pending["data"]["action_id"], approve=True)
        else:
            result = await backend.decide_group_action("ob", "10000", "456", pending["data"]["action_id"], approve=True)
            assert result["state"] == "failed"
        adapter._bot.set_group_card.assert_not_awaited()
    finally:
        await provider.release_session_async(session)
