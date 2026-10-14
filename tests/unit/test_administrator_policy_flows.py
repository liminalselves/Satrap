"""业务链路场景与集中边界契约"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import pytest
from satrap.core.call_context import CallOrigin
from satrap.core.config.administrator_groups import AdministratorService, migrate_legacy_scope_exclusions
from satrap.core.config.administrator_groups import normalize_administrator_groups, normalize_administrator_overrides
from satrap.core.config.platform_identity import platform_instance_id
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from satrap.core.call_context import bind_call_origin
from satrap.core.platform import current_adapter_manager, set_current_adapter_manager
from satrap.edictum import AsyncSimpleSession
from satrap.edictum.plugin_catalog import PluginCatalog, PluginCatalogEntry
from satrap.core.plugin_authorization import evaluate_plugin_permissions
from satrap.expend.plugins.group_chat.tools import get_tools as chat_tools
from support.administration import _setup_adapter, _origin, _async_tools
from support.plugin_runtime import runtime
import asyncio
import threading
from satrap.core.config.document import load_config_document, save_config_document
from satrap.core.utils.async_worker import WorkerBusyError
from support.administrator_http import context, document

def platform(identity="qq", instance="instance-1"):
    return {"id": identity, "type": "future-adapter", "instance_id": instance}

def group(identity="admins", user="user-alpha", *, mode="selected", included=None, excluded=None, enabled=True, protect=None):
    value = {"id": identity, "name": "管理员", "enabled": enabled,
             "members": [{"platform_id": "qq", "platform_instance_id": "instance-1", "user_id": user}],
             "plugin_scope": {"mode": mode, "included": ["example"] if included is None else included, "excluded": excluded or []}}
    if protect is not None:
        value["protect"] = protect
    return value

def override(identity="entry", user="user-beta", *, allow=None, deny=None, enabled=True, protect=None, instance="instance-1"):
    value = {"id": identity, "enabled": enabled, "platform_id": "qq", "platform_instance_id": instance,
             "user_id": user, "allow": allow or [], "deny": deny or []}
    if protect is not None:
        value["protect"] = protect
    return value

def origin(user="user-alpha", adapter="qq"):
    return CallOrigin(adapter, "bot", "GroupMessage", "room", user, "message", "request")

def test_platform_user_identity_is_scoped_and_not_numeric_only():
    platforms = [platform(), platform("other")]
    service = AdministratorService(lambda: platforms, [group()])
    assert service.resolve(origin(), "example").allowed
    assert not service.resolve(origin(adapter="other"), "example").allowed
    assert not service.resolve(origin(user="other-member"), "example").allowed
    assert not service.resolve(None, "example").allowed

def test_groups_union_and_exclusion_priority_only_for_matching_members():
    groups = [group(), group("all", mode="all", included=[]), group("deny", mode="all", included=[], excluded=["example"]),
              group("unrelated", user="someone-else", excluded=["another"])]
    service = AdministratorService(lambda: [platform()], groups)
    result = service.resolve(origin(), "example")
    assert not result.allowed and result.excluded_by == ("deny",)
    assert service.resolve(origin(), "another").allowed
    assert service.protected_users("qq") == ["someone-else", "user-alpha"]

def test_recreated_platform_never_inherits_old_grant_or_protection():
    platforms = [platform()]
    service = AdministratorService(lambda: platforms, [group()])
    platforms[0] = platform(instance="replacement")
    assert not service.resolve(origin(), "example").allowed
    assert service.protected_users("qq") == []
    saved = normalize_administrator_groups([group()], platforms, bind_new=True, previous=[group()])
    assert saved[0]["members"][0]["platform_instance_id"] == "instance-1"

def test_server_binds_new_members_and_keeps_missing_saved_platform():
    submitted = group()
    submitted["members"][0]["platform_instance_id"] = "forged"
    normalized = normalize_administrator_groups([submitted], [platform()], bind_new=True)
    assert normalized[0]["members"][0]["platform_instance_id"] == platform_instance_id(platform())
    assert normalize_administrator_groups(normalized, []) == normalized
    with pytest.raises(ValueError, match="平台不存在"):
        normalize_administrator_groups([submitted], [], bind_new=True)

def test_atomic_snapshot_and_revocation():
    submitted = [group()]
    service = AdministratorService(lambda: [platform()], submitted)
    submitted[0]["members"].clear()
    assert service.resolve(origin(), "example").allowed
    saved, saved_overrides, revision = service.snapshot()
    saved[0]["enabled"] = False
    assert service.resolve(origin(), "example").allowed
    service.apply(saved, saved_overrides)
    assert not service.resolve(origin(), "example").allowed
    assert service.snapshot()[2] != revision
    with pytest.raises(ValueError):
        service.apply({})
    assert service.snapshot()[0] == saved

@pytest.mark.parametrize("mutate", [
    lambda g: g.update(enabled="true"),
    lambda g: g.update(members=[{"platform_id": "qq", "user_id": 123}]),
    lambda g: g["plugin_scope"].update(mode="unknown"),
    lambda g: g["plugin_scope"].update(mode=[]),
    lambda g: g["plugin_scope"].update(included=["example", "example"]),
    lambda g: g.update(unknown="x"),
])
def test_invalid_administrator_configuration_rejected(mutate):
    value = deepcopy(group())
    mutate(value)
    with pytest.raises(ValueError):
        normalize_administrator_groups([value], [platform()])

def test_empty_configuration_and_disabled_group_grant_nothing():
    assert normalize_administrator_groups(None, []) == []
    service = AdministratorService(lambda: [platform()], [group(enabled=False)])
    assert not service.resolve(origin(), "example").allowed
    assert service.protected_users("qq") == []

def test_bad_platform_entry_does_not_interrupt_other_platforms_or_empty_groups():
    platforms = ["bad-config", {"type": "missing-id"}, platform()]
    service = AdministratorService(lambda: platforms, [])
    assert not service.resolve(origin(), "example").allowed
    service.apply([group()])
    assert service.resolve(origin(), "example").allowed

def test_strict_scope_rejects_selected_exclusions_only_on_save_path():
    legacy = group(mode="selected", excluded=["example"])
    with pytest.raises(ValueError, match="成员例外"):
        normalize_administrator_groups([legacy], [platform()], strict_scope=True)
    kept = normalize_administrator_groups([legacy], [platform()])
    assert kept[0]["plugin_scope"]["excluded"] == ["example"]   # 读取路径保留原组合交给迁移

def test_override_normalization_rejects_unknown_fields_and_duplicate_identity():
    with pytest.raises(ValueError):
        normalize_administrator_overrides([{**override(), "unknown": 1}], [platform()])
    with pytest.raises(ValueError):
        normalize_administrator_overrides([override(), override(identity="other")], [platform()])
    with pytest.raises(ValueError):
        normalize_administrator_overrides([override(allow=["example", "example"])], [platform()])

def test_override_flags_default_and_type_are_validated():
    normalized = normalize_administrator_overrides([override(allow=["example"])], [platform()])[0]
    assert normalized["enabled"] is True and normalized["protect"] is False
    for field, value in (("enabled", "true"), ("protect", 1)):
        with pytest.raises(ValueError, match="布尔值"):
            normalize_administrator_overrides([{**override(allow=["example"]), field: value}], [platform()])

def test_empty_override_entries_are_dropped_unless_protecting():
    assert normalize_administrator_overrides([override()], [platform()]) == []
    kept = normalize_administrator_overrides([override(protect=True)], [platform()])
    assert len(kept) == 1 and kept[0]["protect"] is True

def test_override_overlap_is_logged_and_evaluated_as_denial(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr("satrap.core.config.administrator_groups.logger.error", logged.append)
    groups = normalize_administrator_groups([], [platform()])
    overrides = normalize_administrator_overrides([override(allow=["example"], deny=["example"])], [platform()])
    assert logged and "否决优先" in logged[0]
    service = AdministratorService(lambda: [platform()], groups, overrides)
    assert not service.resolve(origin("user-beta"), "example").allowed

def test_override_grant_and_deny_priority():
    """例外层可独立授予; 例外否决优先于组允许并记录两侧来源"""
    granted = AdministratorService(lambda: [platform()], [], [override(allow=["example"])]).resolve(origin("user-beta"), "example")
    assert granted.allowed and granted.allowed_overrides == ("entry",) and granted.group_ids == ()
    assert not AdministratorService(lambda: [platform()], [], [override(allow=["example"])]).resolve(origin("user-alpha"), "example").allowed

    denied = AdministratorService(
        lambda: [platform()], [group()], [override(user="user-alpha", deny=["example"])]).resolve(origin(), "example")
    assert not denied.allowed
    assert denied.group_ids == ("admins",) and denied.denied_overrides == ("entry",)

def test_all_mode_group_exclusion_and_override_deny_coexist():
    service = AdministratorService(
        lambda: [platform()], [group(mode="all", included=[], excluded=["other"])], [override(user="user-alpha", deny=["example"])])
    assert not service.resolve(origin(), "example").allowed
    assert not service.resolve(origin(), "other").allowed

def test_disabled_and_rebuilt_identity_match_nothing():
    disabled = AdministratorService(lambda: [platform()], [], [override(allow=["example"], enabled=False)])
    assert not disabled.resolve(origin("user-beta"), "example").allowed
    platforms = [platform()]
    service = AdministratorService(lambda: platforms, [], [override(allow=["example"])])
    platforms[0] = platform(instance="replacement")
    assert not service.resolve(origin("user-beta"), "example").allowed
    saved = normalize_administrator_overrides([override(allow=["example"])], platforms, bind_new=True, previous=[override(allow=["example"])])
    assert saved[0]["platform_instance_id"] == "instance-1"   # 保留旧绑定, 同名平台重建不自动恢复

def test_override_rebind_requires_saved_binding_and_keeps_other_identity():
    platforms = [platform()]
    previous = [override(allow=["example"])]
    forged = override(allow=["example"], instance="forged")
    kept = normalize_administrator_overrides([forged], platforms, bind_new=True, previous=previous)
    assert kept[0]["platform_instance_id"] == "instance-1"
    with pytest.raises(ValueError, match="平台不存在"):
        normalize_administrator_overrides([override(identity="new", user="user-gamma", allow=["example"])], [], bind_new=True)

def test_protection_requires_enabled_matched_protect_switch():
    """保护只对启用且实例匹配的条目生效, 组与例外一致"""
    off = AdministratorService(lambda: [platform()], [group(protect=False)], [])
    assert off.protected_users("qq") == []
    on = AdministratorService(lambda: [platform()], [group(protect=False)], [override(protect=True)])
    assert on.protected_users("qq") == ["user-beta"]
    disabled = AdministratorService(lambda: [platform()], [], [override(protect=True, enabled=False)])
    assert disabled.protected_users("qq") == []
    disabled_protect = AdministratorService(lambda: [platform()], [group("off", enabled=False, protect=True)], [])
    assert disabled_protect.protected_users("qq") == []
    leaked = AdministratorService(lambda: [platform()], [group("p", protect=True, enabled=False), group("np", protect=False)], [])
    assert leaked.protected_users("qq") == []
    enabled_protect = AdministratorService(lambda: [platform()], [group(protect=True)], [])
    assert enabled_protect.protected_users("qq") == ["user-alpha"]

def test_migration_expands_legacy_exclusions_and_merges_existing_entries():
    groups = normalize_administrator_groups([group(mode="selected", excluded=["example", "other"])], [platform()])
    existing = normalize_administrator_overrides([override(user="user-alpha", deny=["example"])], [platform()])
    migrated_groups, migrated, pending = migrate_legacy_scope_exclusions(groups, existing)
    assert migrated_groups[0]["plugin_scope"]["excluded"] == []
    assert migrated[0]["deny"] == ["example", "other"]   # 并入既有条目而不是新建
    assert len(migrated) == 1 and pending == []
    assert migrate_legacy_scope_exclusions(migrated_groups, migrated) == (migrated_groups, migrated, [])   # 幂等

def test_migration_creates_entry_for_other_member_and_keeps_all_mode_group_exclusion():
    groups = normalize_administrator_groups([group(mode="selected", excluded=["example"])], [platform()])
    existing = normalize_administrator_overrides([override(user="user-beta", deny=["other"])], [platform()])
    _, migrated, pending = migrate_legacy_scope_exclusions(groups, existing)
    assert len(migrated) == 2 and sorted(item["deny"] for item in migrated) == [["example"], ["other"]]
    assert pending == []
    service = AdministratorService(lambda: [platform()], [group(mode="all", included=[], excluded=["example"])])
    assert not service.resolve(origin(), "example").allowed
    assert service.snapshot()[1] == []   # all 模式排除留在组内, 迁移只处理指定插件模式

def test_migration_on_read_path_keeps_effective_denial():
    service = AdministratorService(lambda: [platform()], [group(mode="selected", excluded=["example"])])
    groups, overrides, _ = service.snapshot()
    assert groups[0]["plugin_scope"]["excluded"] == []
    assert overrides[0]["deny"] == ["example"] and overrides[0]["protect"] is False
    assert not service.resolve(origin(), "example").allowed   # 迁移后有效行为不变

def test_revision_covers_both_sections_and_snapshot_is_atomic():
    service = AdministratorService(lambda: [platform()], [group()])
    _, _, before = service.snapshot()
    service.apply([group()], [override(allow=["example"])])
    groups, overrides, after = service.snapshot()
    assert after != before and len(groups) == 1 and len(overrides) == 1
    service.apply([group()], [override(allow=["other"])])
    assert service.snapshot()[2] != after

def test_migration_tolerates_legacy_entries_without_protect_field():
    legacy = group()
    assert "protect" not in legacy
    normalized = normalize_administrator_groups([legacy], [platform()])
    assert normalized[0]["protect"] is True   # 缺省保持现行全保护行为
    service = AdministratorService(lambda: [platform()], [legacy])
    assert service.protected_users("qq") == ["user-alpha"]

def test_migration_of_disabled_group_captures_denial_without_changing_behavior():
    # 源组停用 → 条目 enabled=False, 否决惰性; 迁移前后该成员都允许
    service = AdministratorService(lambda: [platform()], [group("allow"), group("off", enabled=False, excluded=["example"])])
    groups, overrides, _ = service.snapshot()
    assert [item["enabled"] for item in overrides] == [False]
    assert overrides[0]["deny"] == ["example"]
    assert groups[1]["plugin_scope"]["excluded"] == []
    assert service.resolve(origin(), "example").allowed

def test_migration_conflicting_entry_skips_round_trips_and_resolves():
    conflicting = override(user="user-alpha", deny=["other"], enabled=False)
    service = AdministratorService(lambda: [platform()], [group(excluded=["example"])], [conflicting])
    groups, overrides, _ = service.snapshot()
    assert groups[0]["plugin_scope"]["excluded"] == ["example"]   # 旧排除原样保留, 行为零变化
    assert overrides[0]["deny"] == ["other"] and overrides[0]["enabled"] is False
    assert not service.resolve(origin(), "example").allowed       # 旧规则仍生效
    # 待迁移组带着旧排除原样回存必须放行, 且语义不变 (规则6 解除死锁)
    round_tripped = normalize_administrator_groups(groups, [platform()], previous=groups, strict_scope=True)
    assert round_tripped[0]["plugin_scope"]["excluded"] == ["example"]
    service.apply(round_tripped, overrides)
    assert service.snapshot()[0][0]["plugin_scope"]["excluded"] == ["example"]
    assert not service.resolve(origin(), "example").allowed
    # 启用状态对齐后下次读取即完成迁移
    service.apply(service.snapshot()[0], [dict(service.snapshot()[1][0], enabled=True)])
    groups, overrides, _ = service.snapshot()
    assert groups[0]["plugin_scope"]["excluded"] == []
    assert overrides[0]["deny"] == ["other", "example"]

def test_migration_merges_when_enabled_state_matches():
    matching = override(user="user-alpha", deny=["other"], enabled=True)
    service = AdministratorService(lambda: [platform()], [group(excluded=["example"])], [matching])
    groups, overrides, _ = service.snapshot()
    assert groups[0]["plugin_scope"]["excluded"] == []
    assert overrides[0]["deny"] == ["other", "example"] and overrides[0]["enabled"] is True

def test_migration_generates_unique_entry_id_when_digest_collides():
    digest = hashlib.sha1(f"qq\0user-alpha".encode("utf-8")).hexdigest()[:16]
    collision = override(identity=f"legacy-{digest}", user="user-beta", deny=["other"])
    groups = normalize_administrator_groups([group(excluded=["example"])], [platform()])
    overrides = normalize_administrator_overrides([collision], [platform()])
    _, migrated, _ = migrate_legacy_scope_exclusions(groups, overrides)
    assert len({item["id"] for item in migrated}) == len(migrated)   # ID 不冲突
    normalize_administrator_overrides(migrated, [platform()])        # 链式再归一化不报错

def test_strict_scope_accepts_saved_excluded_subset_only():
    saved = normalize_administrator_groups([group(excluded=["example", "other"])], [platform()])
    intact = normalize_administrator_groups([group(excluded=["example", "other"])], [platform()], previous=saved, strict_scope=True)
    assert intact[0]["plugin_scope"]["excluded"] == ["example", "other"]   # 原样往返
    trimmed = normalize_administrator_groups([group(excluded=["example"])], [platform()], previous=saved, strict_scope=True)
    assert trimmed[0]["plugin_scope"]["excluded"] == ["example"]           # 逐项收敛

def test_strict_scope_rejects_added_exclusion_and_names_it():
    saved = normalize_administrator_groups([group(excluded=["example"])], [platform()])
    with pytest.raises(ValueError, match=r"不能新增排除插件: \['other'\]"):
        normalize_administrator_groups([group(excluded=["example", "other"])], [platform()], previous=saved, strict_scope=True)

def test_strict_scope_treats_new_or_renamed_group_as_empty_previous():
    saved = normalize_administrator_groups([group(excluded=["example"])], [platform()])
    with pytest.raises(ValueError, match="不能新增排除插件"):
        normalize_administrator_groups([group("fresh", excluded=["example"])], [platform()], previous=saved, strict_scope=True)

def test_protection_ignores_rebuilt_platform_instance():
    platforms = [platform()]
    service = AdministratorService(lambda: platforms, [group(protect=True), group("o", user="user-beta", protect=True)], [])
    assert sorted(service.protected_users("qq")) == ["user-alpha", "user-beta"]
    platforms[0] = platform(instance="replacement")
    assert service.protected_users("qq") == []

def groups(platform, plugins, *, enabled=True, excluded=()):
    return [{"id": "main", "name": "主要管理员", "enabled": enabled,
             "members": [{"platform_id": platform["id"], "platform_instance_id": platform_instance_id(platform), "user_id": "123"}],
             "plugin_scope": {"mode": "selected", "included": list(plugins), "excluded": list(excluded)}}]

@pytest.fixture()
def restore_manager():
    previous = current_adapter_manager()
    yield
    set_current_adapter_manager(previous)

@pytest.mark.usefixtures('restore_manager')
def test_management_gates_use_admin_grants_and_preserve_local_empty_rules(tmp_path):
    platform = {"id": "ob", "instance_id": "original"}
    service = AdministratorService(lambda: [platform], groups(platform, ["group_admin", "group_chat", "friend_manager"]))
    catalog = PluginCatalog(user_dir=tmp_path / "plugins")
    cases = [("group_admin", "group_admin_get_honors", "allowed_read_callers", True),
             ("group_admin", "group_admin_kick", "allowed_callers", False),
             ("group_admin", "group_admin_list_group_requests", "request_managers", False),
             ("group_chat", "group_chat_list_groups", "cross_group_query_callers", False),
             ("group_chat", "group_chat_set_group_nickname", "nickname_allowed_callers", True),
             ("friend_manager", "friend_manager_list_friends", "managers", False)]
    for name, tool, field, empty_allow in cases:
        entry = catalog.get(name)
        assert entry and entry.permissions.supports_administrators
        config = {field: ""}
        assert evaluate_plugin_permissions(name, entry.permissions, "tools", tool, config, _origin(), service).status == "allowed"
        decision = evaluate_plugin_permissions(name, entry.permissions, "tools", tool, config, _origin())
        assert (decision.status == "allowed") == empty_allow
        assert config[field] == ""

@pytest.mark.usefixtures('restore_manager')
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
        assert result["ok"] is True
        denied = await write.execute(request_id="request", approve=True)
        assert denied["ok"] is False and denied["error_type"] == "permission_denied" and "未在插件配置中开启" in denied["error"]
    with bind_call_origin(_origin(actor="999")):
        assert not query.is_available_for_call()
        assert (await query.execute())["ok"] is False

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_admin_private_group_list_rechecks_revocation_after_adapter_read(tmp_path, monkeypatch):
    from support.group_read import _setup
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

@pytest.mark.usefixtures('restore_manager')
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

@pytest.mark.usefixtures('restore_manager')
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
            assert query.is_available_for_call() and not (await query.execute())["ok"]
        action = cast(str, pending["data"]["action_id"])
        record = await adapter.friend_host.decide("10000", action, True)
        assert record["state"] == "failed"
        adapter._bot.delete_friend.assert_not_awaited()
    finally:
        await provider.release_session_async(session)

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("chat_type", ["GroupMessage", "FriendMessage"])
async def test_real_agent_self_deletion_approval_uses_current_group_or_private_route(tmp_path, asynchronous, chat_type):
    import asyncio
    backend, adapter, origin, session, provider, _ = await runtime(
        tmp_path, asynchronous, "friend_manager", {"delete_friend_enabled": True},
    )
    try:
        adapter.friend_host = backend.friend_service("ob")
        adapter._bot.get_friend_list.return_value = [{"user_id": 321, "nickname": "本人"}]
        own = replace(origin, chat_type=chat_type, chat_id="456" if chat_type == "GroupMessage" else "321", actor_id="321")
        deletion = cast(Any, session)._wf.tools_manager.tools["friend_manager_delete_friend"]
        with bind_call_origin(own):
            result = await deletion.execute() if asynchronous else await asyncio.to_thread(deletion.execute)
        assert result["ok"] and result["data"]["state"] == "pending"
        adapter._bot.delete_friend.assert_not_awaited()
        approved = await adapter.friend_host.decide("10000", result["data"]["action_id"], True)
        assert approved["state"] == "succeeded", approved
        adapter._bot.delete_friend.assert_awaited_once_with(user_id=321)
    finally:
        await provider.release_session_async(session)

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_pending_friend_deletion_rechecks_instance_tool_switch(tmp_path):
    """实例工具关闭后, 已提交的删除申请不能继续执行"""
    from satrap.edictum.plugin_settings import PluginSettingsService
    from satrap.edictum.plugin_spec import parse_plugin_specs
    from satrap.core.config.session_overrides import SessionOverrideStore

    backend, adapter, origin, session, provider, configs = await runtime(
        tmp_path, True, "friend_manager", {"delete_friend_enabled": True},
    )
    try:
        session.plugin_override_store = SessionOverrideStore(backend.platform_db_path("ob"))
        adapter.friend_host = backend.friend_service("ob")
        adapter._bot.get_friend_list.return_value = [{"user_id": 321, "nickname": "本人"}]
        own = replace(origin, chat_type="FriendMessage", chat_id="321", actor_id="321")
        deletion = session._wf.tools_manager.tools["friend_manager_delete_friend"]
        with bind_call_origin(own):
            pending = await deletion.execute()
        assert pending["ok"] and pending["data"]["state"] == "pending"
        settings = PluginSettingsService(backend.platform_db_path("ob"))
        spec = parse_plugin_specs(configs.get_config("assistant")["plugins"], provider.plugin_catalog)[0]
        entry = cast(PluginCatalogEntry, provider.plugin_catalog.get("friend_manager"))
        metadata = settings.tool_settings(session.session_id, spec, entry)
        record = settings.get(session.session_id, spec.name, entry.config_schema, spec.config)
        settings.save(session.session_id, spec.name, entry.config_schema, record["overrides"],
                      expected_revision=record["revision"], named=spec.config,
                      tool_overrides={"friend_manager_delete_friend": False}, expected_tool_revision=metadata["tool_revision"])
        approved = await adapter.friend_host.decide("10000", cast(str, pending["data"]["action_id"]), True)
        assert approved["state"] == "failed", approved
        adapter._bot.delete_friend.assert_not_awaited()
    finally:
        await provider.release_session_async(session)

@pytest.mark.asyncio
async def test_config_snapshot_does_not_wait_for_runtime_and_never_applies(tmp_path, monkeypatch):
    """运行宿主挂起时设置仍可读, 所有新增 GET 不写入或应用"""
    from satrap.core.backend import control_server as control

    path = tmp_path / "admin.json"
    saved = save_config_document(path, document())
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    original_load = control.load_config_document
    main_thread = threading.get_ident()
    reads = []
    calls = []
    entered, release = threading.Event(), threading.Event()

    def load(source):
        """检查文件读取不占用事件循环线程"""
        reads.append(threading.get_ident())
        return original_load(source)

    def runtime(revision, *, apply=False):
        """挂起只读状态, 不模拟设置快照"""
        calls.append(apply)
        entered.set()
        if not release.wait(2):
            raise RuntimeError("验收未释放运行状态查询")
        return {"status": "applied", "section_revision": revision}

    monkeypatch.setattr(control, "load_config_document", load)
    monkeypatch.setattr(control, "_administrator_runtime_request", runtime)
    root = "/config/administrator-groups"
    first = cast(Any, await control._route_administrator_settings(context(control, "GET", root + "/snapshot")))
    assert first and first[0] == 200 and first[1]["runtime"]["status"] == "unconfirmed"
    assert calls == []
    task = asyncio.create_task(control._route_administrator_settings(context(control, "GET", root + "/runtime")))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        snapshot = cast(Any, await asyncio.wait_for(control._route_administrator_settings(context(control, "GET", root + "/snapshot")), .5))
        assert snapshot and snapshot[1]["revision"] == first[1]["revision"]
        assert not task.done()
    finally:
        release.set()
        result = cast(Any, await task)
    assert result and result[1]["runtime"]["status"] == "applied"
    assert result[1]["section_revision"] == first[1]["section_revision"]
    legacy = cast(Any, await control._route_administrator_settings(context(control, "GET", root)))
    assert legacy and legacy[1]["runtime"]["status"] == "applied"
    assert all(thread != main_thread for thread in reads)
    assert calls == [False, False] and load_config_document(path) == saved

@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/snapshot"])
async def test_administrator_read_busy_queue_returns_explicit_failure(tmp_path, monkeypatch, suffix):
    """读取队列满时明确失败并记录日志, 不进入保存事务"""
    from satrap.core.backend import control_server as control

    path = tmp_path / "admin.json"
    saved = save_config_document(path, document())
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    messages = []

    async def busy(*args, **kwargs):
        """模拟有界工作队列已满"""
        raise WorkerBusyError("验收队列已满")

    monkeypatch.setattr(control.RAG_WORKERS, "run", busy)
    monkeypatch.setattr(control.logger, "warning", messages.append)
    result = cast(Any, await control._route_administrator_settings(context(control, "GET", "/config/administrator-groups" + suffix)))
    assert result[0] == 503 and "队列已满" in result[1]["error"]
    assert messages and "队列已满" in messages[0]
    assert load_config_document(path) == saved
