"""好友重复开关迁移, 默认关闭与多层覆盖不意外放行"""
from pathlib import Path
from types import SimpleNamespace
import json
from copy import deepcopy

import pytest

from satrap.edictum.friend_migration import FRIEND_SWITCHES, GLOBAL_RECEIPT, migrate_friend_globals
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_spec import parse_plugin_specs
from satrap.edictum.plugin_settings import resolve_runtime_specs, PluginSettingsService
from satrap.core.config.session_overrides import SessionOverrideStore, OverrideConflictError


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setattr("satrap.edictum.plugin_config.CONFIG_DIR", root)
    return PluginConfigManager(root), PluginCatalog(user_dir=tmp_path / "user")


@pytest.mark.parametrize("field,tool", list(FRIEND_SWITCHES.items()))
@pytest.mark.parametrize("global_flag", [False, True])
@pytest.mark.parametrize("named_flag", [None, False, True])
@pytest.mark.parametrize("tool_enabled", [False, True])
def test_old_effective_state_preserved_and_migration_idempotent(config, field, tool, global_flag, named_flag, tool_enabled):
    manager, catalog = config
    path = manager._global_path("friend_manager")
    path.write_text(json.dumps({field: global_flag}), encoding="utf-8")
    source = [{"name": "friend_manager", "config": {field: named_flag} if named_flag is not None else {}, "capabilities": {"tools": {tool: tool_enabled}}}]
    before = deepcopy(source)
    spec = parse_plugin_specs(source, catalog, migration_manager=manager)[0]
    assert source == before
    assert spec.capabilities["tools"][tool] is (tool_enabled and (global_flag if named_flag is None else named_flag))
    assert not set(spec.config) & set(FRIEND_SWITCHES)
    canonical = spec.to_config()
    assert parse_plugin_specs([canonical], catalog, migration_manager=manager)[0].to_config() == canonical
    migrated_global = json.loads(path.read_text(encoding="utf-8"))
    assert not set(migrated_global) & set(FRIEND_SWITCHES)
    assert migrated_global[GLOBAL_RECEIPT]["legacy_defaults"][tool] is global_flag


def test_absent_old_switches_stay_disabled_and_new_config_can_enable(config):
    manager, catalog = config
    old = parse_plugin_specs(["friend_manager"], catalog, migration_manager=manager)[0]
    assert all(old.capabilities["tools"][name] is False for name in FRIEND_SWITCHES.values())
    updated = old.to_config()
    updated["capabilities"]["tools"]["friend_manager_handle_request"] = True
    assert parse_plugin_specs([updated], catalog, migration_manager=manager)[0].capabilities["tools"]["friend_manager_handle_request"]
    new = parse_plugin_specs([{"name": "friend_manager", "config_version": 1}], catalog, migration_manager=manager)[0]
    assert all(new.capabilities["tools"][name] for name in FRIEND_SWITCHES.values())


def test_global_save_preserves_legacy_baseline_but_not_live_gate(config):
    manager, catalog = config
    path = manager._global_path("friend_manager")
    path.write_text(json.dumps({"request_handling_enabled": True}), encoding="utf-8")
    migrate_friend_globals(manager)
    schema = catalog.get("friend_manager").config_schema
    manager.save_global("friend_manager", schema, {"managers": "123"})
    old = parse_plugin_specs(["friend_manager"], catalog, migration_manager=manager)[0]
    assert old.capabilities["tools"]["friend_manager_handle_request"]
    new = old.to_config()
    new["capabilities"]["tools"]["friend_manager_handle_request"] = False
    assert not parse_plugin_specs([new], catalog, migration_manager=manager)[0].capabilities["tools"]["friend_manager_handle_request"]
    assert GLOBAL_RECEIPT not in manager.load_global("friend_manager", schema)


@pytest.mark.parametrize("original_tool", [False, True])
@pytest.mark.parametrize("named_switch", [False, True])
@pytest.mark.parametrize("instance_switch", [False, True])
def test_instance_highest_priority_converted_to_tool_override(config, tmp_path, original_tool, named_switch, instance_switch):
    manager, catalog = config
    spec = parse_plugin_specs([{"name": "friend_manager", "config": {"request_handling_enabled": named_switch},
                               "capabilities": {"tools": {"friend_manager_handle_request": original_tool}}}], catalog, migration_manager=manager)[0]
    store = SessionOverrideStore(tmp_path / "session.db")
    store.replace("s", "plugins.friend_manager", {"request_handling_enabled": instance_switch, "managers": "123"}, expected_revision=0)
    session = SimpleNamespace(session_id="s", plugin_override_store=store)
    resolved = resolve_runtime_specs(session, [spec], catalog)[0]
    assert resolved.capabilities["tools"]["friend_manager_handle_request"] is (original_tool and instance_switch)
    assert resolved.config["managers"] == "123"
    old_record = store.read("s", "plugins.friend_manager")
    assert old_record["overrides"] == {"managers": "123"}
    receipt = store.read("s", "plugin_capabilities.friend_manager")
    assert receipt["overrides"] == {"friend_manager_handle_request": original_tool and instance_switch}
    again = resolve_runtime_specs(session, [spec], catalog)[0]
    assert again.capabilities == resolved.capabilities
    assert store.read("s", "plugins.friend_manager") == old_record
    assert store.read("s", "plugin_capabilities.friend_manager") == receipt


def test_invalid_global_migration_does_not_overwrite_source(config):
    manager, _ = config
    path = manager._global_path("friend_manager")
    path.write_text("{bad-json", encoding="utf-8")
    with pytest.raises(ValueError):
        migrate_friend_globals(manager)
    assert path.read_text(encoding="utf-8") == "{bad-json"


def test_migrated_instance_tool_state_is_editable_and_can_restore_inheritance(config, tmp_path):
    manager, catalog = config
    entry = catalog.get("friend_manager")
    spec = parse_plugin_specs([{"name": "friend_manager", "config": {"request_handling_enabled": True}}], catalog, migration_manager=manager)[0]
    service = PluginSettingsService(tmp_path / "settings.db", manager)
    service.overrides.store.replace("s", "plugins.friend_manager", {"request_handling_enabled": False}, expected_revision=0)
    tools = service.tool_settings("s", spec, entry)
    data = service.get("s", "friend_manager", entry.config_schema, spec.config)
    assert tools["tool_overrides"]["friend_manager_handle_request"] is False
    assert tools["inherited_tools"]["friend_manager_handle_request"] is True
    service.save("s", "friend_manager", entry.config_schema, {"managers": "123"}, expected_revision=data["revision"],
                 named=spec.config, tool_overrides={"friend_manager_handle_request": True}, expected_tool_revision=tools["tool_revision"])
    session = SimpleNamespace(session_id="s", plugin_override_store=service.overrides.store)
    assert resolve_runtime_specs(session, [spec], catalog)[0].capabilities["tools"]["friend_manager_handle_request"]
    current = service.get("s", "friend_manager", entry.config_schema, spec.config)
    latest = service.tool_settings("s", spec, entry)
    service.save("s", "friend_manager", entry.config_schema, {}, expected_revision=current["revision"], named=spec.config,
                 tool_overrides={}, expected_tool_revision=latest["tool_revision"])
    assert service.tool_settings("s", spec, entry)["tool_overrides"] == {}
    assert resolve_runtime_specs(session, [spec], catalog)[0].capabilities["tools"]["friend_manager_handle_request"]


def test_config_and_tool_revision_conflict_roll_back_together(config, tmp_path):
    manager, catalog = config
    service = PluginSettingsService(tmp_path / "settings.db", manager)
    entry = catalog.get("friend_manager")
    service.overrides.store.replace("s", "plugin_capabilities.friend_manager", {"friend_manager_delete_friend": False}, expected_revision=0)
    with pytest.raises(OverrideConflictError):
        service.save("s", "friend_manager", entry.config_schema, {"managers": "123"}, expected_revision=0,
                     tool_overrides={"friend_manager_delete_friend": True}, expected_tool_revision=0)
    assert service.overrides.store.read("s", "plugins.friend_manager")["revision"] == 0
    assert service.overrides.store.read("s", "plugin_capabilities.friend_manager")["overrides"] == {"friend_manager_delete_friend": False}


def test_tool_settings_returns_values_and_revision_from_same_read(config, tmp_path, monkeypatch):
    """迁移后若有并发写入, 返回的工具值与修订号必须属于同一记录"""
    manager, catalog = config
    spec = parse_plugin_specs([{"name": "friend_manager", "config_version": 1}], catalog, migration_manager=manager)[0]
    service = PluginSettingsService(tmp_path / "settings.db", manager)
    def migrate_then_modify(store, session_id, spec):
        store.replace(session_id, "plugin_capabilities.friend_manager", {"friend_manager_delete_friend": False}, expected_revision=0)
        return {}
    monkeypatch.setattr("satrap.edictum.friend_migration.migrate_friend_overrides", migrate_then_modify)
    result = service.tool_settings("s", spec, catalog.get("friend_manager"))
    assert result["tool_revision"] == 1
    assert result["tool_overrides"] == {"friend_manager_delete_friend": False}
