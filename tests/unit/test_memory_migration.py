"""记忆拆分保留数据与关闭状态, 全局和会话覆盖迁移可重复执行"""
from pathlib import Path
import json

import pytest

from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.memory.store import MemoryStore
from satrap.edictum.memory_migration import migrate_memory_overrides
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.edictum.plugin_spec import parse_plugin_specs


@pytest.fixture
def catalog(tmp_path):
    return PluginCatalog(preset_dir=Path(__file__).resolve().parents[2] / "satrap/expend/plugins", user_dir=tmp_path / "plugins")


def test_split_preserves_closed_capabilities_and_is_idempotent(catalog):
    original = [{"name": "base_take", "config": {"memory_mode": "base", "search_timeout": 12}, "capabilities": {
        "tools": {"add_memory": False, "list_memories": True},
        "handlers": {"base_take.memory_inject": False}, "commands": {"memory": False}}}]
    specs = parse_plugin_specs(original, catalog, require_available=True)
    base, memory = specs
    assert base.name == "base_take" and memory.name == "memory"
    assert base.config == {"search_timeout": 12}
    assert set(base.capabilities["tools"]) == {"search", "fetch_page", "code_sandbox", "read_document"}
    assert memory.config == {"memory_mode": "base"}
    assert memory.capabilities["tools"]["add_memory"] is False
    assert memory.capabilities["tools"]["get_memory"] is False
    assert memory.capabilities["handlers"]["memory.memory_inject"] is False
    assert memory.capabilities["commands"]["memory"] is False
    assert not any(memory.capabilities["skills"].values())
    serialized = [spec.to_config() for spec in specs]
    assert [spec.to_config() for spec in parse_plugin_specs(serialized, catalog)] == serialized
    assert original[0]["config"]["memory_mode"] == "base"
    assert len(parse_plugin_specs([base.to_config()], catalog)) == 1


def test_disabled_legacy_parent_cannot_enable_memory(catalog):
    specs = parse_plugin_specs([{"name": "base_take", "enabled": False, "config": {"memory_mode": "full"}}], catalog)
    memory = specs[1]
    assert memory.enabled is False
    assert not any(memory.capabilities["tools"].values())
    assert not any(memory.capabilities["handlers"].values())


def test_existing_memory_config_wins_but_closed_capabilities_stay_closed(catalog):
    specs = parse_plugin_specs([
        {"name": "base_take", "config": {"memory_mode": "full"}, "capabilities": {"tools": {"delete_memory": False}}},
        {"name": "memory", "config": {"memory_mode": "disabled"}, "capabilities": {"tools": {"add_memory": False}}},
    ], catalog)
    memory = specs[1]
    assert memory.config["memory_mode"] == "disabled"
    assert memory.capabilities["tools"]["delete_memory"] is False
    assert memory.capabilities["tools"]["add_memory"] is False


def test_global_migration_preserves_unknown_base_fields_and_new_explicit_values(tmp_path, catalog):
    manager = PluginConfigManager(tmp_path)
    source = tmp_path / "base_take.json"
    target = tmp_path / "memory.json"
    source.write_text(json.dumps({"memory_mode": "base", "memory_scope": "isolated", "search_timeout": 17}), encoding="utf-8")
    target.write_text(json.dumps({"memory_mode": "disabled"}), encoding="utf-8")
    entry = catalog.get("memory")
    manager.load_global("memory", entry.config_schema)
    assert json.loads(source.read_text(encoding="utf-8")) == {"search_timeout": 17}
    expected = {"memory_mode": "disabled", "memory_scope": "isolated"}
    assert json.loads(target.read_text(encoding="utf-8")) == expected
    manager.load_global("memory", entry.config_schema)
    assert json.loads(target.read_text(encoding="utf-8")) == expected


def test_bad_new_global_file_does_not_remove_old_fields(tmp_path, catalog):
    source = tmp_path / "base_take.json"
    source.write_text('{"memory_mode":"base"}', encoding="utf-8")
    (tmp_path / "memory.json").write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        PluginConfigManager(tmp_path).load_global("memory", catalog.get("memory").config_schema)
    assert json.loads(source.read_text(encoding="utf-8"))["memory_mode"] == "base"


def test_session_override_migration_is_transactional_and_preserves_revision(tmp_path):
    store = SessionOverrideStore(tmp_path / "p.db")
    store.replace("s", "plugins.base_take", {"memory_mode": "full", "memory_scope": "isolated", "search_timeout": 8}, expected_revision=0)
    store.replace("s", "plugins.memory", {"memory_mode": "disabled"}, expected_revision=0)
    assert migrate_memory_overrides(store, "s") is True
    assert store.read("s", "plugins.base_take")["overrides"] == {"search_timeout": 8}
    record = store.read("s", "plugins.memory")
    assert record["overrides"] == {"memory_mode": "disabled", "memory_scope": "isolated"}
    assert record["revision"] == 2
    assert migrate_memory_overrides(store, "s") is False
    assert store.read("s", "plugins.memory")["revision"] == 2


def test_existing_memory_records_keep_their_identifiers(tmp_path):
    database = tmp_path / "p.db"
    record = MemoryStore(db_path=database, scope="session:s").add("偏好", "简洁回答")
    reopened = MemoryStore(db_path=database, scope="session:s")
    assert reopened.get(record["memory_id"])["content"] == "简洁回答"
    assert MemoryStore(db_path=database, scope="session:another").list_all() == []
