"""会话覆盖的持久化, 并发, 继承和归档行为"""
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest

from satrap.core.config.session_overrides import OverrideConflictError, SessionOverrideStore
from satrap.edictum.plugin_settings import PluginSettingsService
from satrap.core.storage.database import delete_session_domain_rows, restore_session_domain, snapshot_session_domain
from satrap.edictum.plugin_config import ConfigField, PluginConfigManager, parse_config_schema
from satrap.core.storage import StorageLayout, StorageMaintenanceService, maintenance as module


def test_override_isolation_and_restart(tmp_path):
    database = tmp_path / "platform.db"
    store = SessionOverrideStore(database)
    values = {"flag": False, "zero": 0, "empty": "", "array": [], "nullable": None}
    store.replace("one", "plugins.example", values, expected_revision=0)
    assert SessionOverrideStore(database).read("one", "plugins.example")["overrides"] == values
    assert store.read("two", "plugins.example")["overrides"] == {}
    assert store.read("one", "plugins.other")["overrides"] == {}
    assert not (tmp_path / "sessions").exists()


def test_concurrent_save_rejects_stale_revision(tmp_path):
    database = tmp_path / "platform.db"
    stores = [SessionOverrideStore(database), SessionOverrideStore(database)]
    barrier = threading.Barrier(2)

    def save(index):
        barrier.wait()
        try:
            stores[index].replace("one", "plugins.example", {"top_k": index + 1}, expected_revision=0)
            return "saved"
        except OverrideConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(save, [0, 1])) == ["conflict", "saved"]


def test_archive_and_override_save_cannot_lose_an_acknowledged_revision(tmp_path, monkeypatch):
    """归档快照与删除期间阻止覆盖写入, 已回收会话的旧修订号不能继续保存"""
    layout = StorageLayout(tmp_path / "data")
    store = SessionOverrideStore(layout.platform_db("chat"))
    store.replace("one", "example", {"value": 1}, expected_revision=0)
    entered, release = threading.Event(), threading.Event()
    original = module.snapshot_session_domain
    def snapshot(*args):
        result = original(*args)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(module, "snapshot_session_domain", snapshot)
    maintenance = StorageMaintenanceService(layout)
    with ThreadPoolExecutor(max_workers=2) as executor:
        archive = executor.submit(maintenance.archive_session, "chat", "one")
        assert entered.wait(5)
        save = executor.submit(store.replace, "one", "example", {"value": 2}, expected_revision=1)
        try:
            with pytest.raises(TimeoutError):
                save.result(timeout=0.1)
        finally:
            release.set()
        archived = archive.result(timeout=5)
        with pytest.raises(OverrideConflictError):
            save.result(timeout=5)
    maintenance.restore_archive("chat", archived["archive_id"])
    assert store.read("one", "example")["overrides"] == {"value": 1}


def test_reset_retains_revision_and_blocks_old_editor(tmp_path):
    store = SessionOverrideStore(tmp_path / "platform.db")
    store.replace("one", "plugins.example", {"top_k": 10}, expected_revision=0)
    record = store.replace("one", "plugins.example", {}, expected_revision=1)
    assert record["overrides"] == {}
    assert record["revision"] == 2
    with pytest.raises(OverrideConflictError):
        store.replace("one", "plugins.example", {"top_k": 3}, expected_revision=0)


def test_layers_and_restore_inheritance(tmp_path):
    manager = PluginConfigManager(tmp_path / "config")
    schema = {
        "top_k": ConfigField("top_k", "number", 5),
        "enabled": ConfigField("enabled", "bool", True),
        "ids": ConfigField("ids", "knowledge_bases", ["base"]),
    }
    manager.save_global("example", schema, {"top_k": 6})
    service = PluginSettingsService(tmp_path / "platform.db", manager)
    record = service.save("one", "example", schema, {"enabled": False, "ids": []}, expected_revision=0, named={"top_k": 7})
    assert record["config"] == {"top_k": 7, "enabled": False, "ids": []}
    assert record["sources"] == {"top_k": "named", "enabled": "session", "ids": "session"}
    assert record["inherited_sources"] == {"top_k": "named", "enabled": "default", "ids": "default"}
    manager.save_global("example", schema, {"top_k": 9})
    assert service.get("one", "example", schema)["config"]["top_k"] == 9
    record = service.save("one", "example", schema, {}, expected_revision=1)
    assert record["config"] == {"top_k": 9, "enabled": True, "ids": ["base"]}
    assert record["sources"]["enabled"] == "default"


def test_optional_model_without_default_can_remain_unconfigured(tmp_path):
    schema = parse_config_schema({"config_schema": {"embed": {"type": "embed"}, "enabled": {"type": "bool", "default": True}}})
    service = PluginSettingsService(tmp_path / "platform.db", PluginConfigManager(tmp_path / "config"))
    saved = service.save("one", "example", schema, {"enabled": False}, expected_revision=0)
    assert saved["config"] == {"enabled": False, "embed": None}
    with pytest.raises(ValueError, match="null"):
        service.save("one", "example", schema, {"embed": None}, expected_revision=1)


def test_invalid_overrides_are_not_saved(tmp_path):
    schema = parse_config_schema({"config_schema": {
        "top_k": {"type": "number", "integer": True, "minimum": 1},
        "private": {"type": "string", "session_overridable": False},
    }})
    service = PluginSettingsService(tmp_path / "platform.db", PluginConfigManager(tmp_path / "config"))
    for values in ({"top_k": 0}, {"top_k": True}, {"top_k": float("nan")}, {"private": "x"}, {"unknown": 2}):
        with pytest.raises(ValueError):
            service.save("one", "example", schema, values, expected_revision=0)
    assert service.overrides.store.read("one", "plugins.example")["revision"] == 0


def test_archive_roundtrip_and_identity_validation(tmp_path):
    database = tmp_path / "platform.db"
    store = SessionOverrideStore(database)
    store.replace("one", "plugins.example", {"top_k": 8}, expected_revision=0)
    store.replace("two", "plugins.example", {"top_k": 9}, expected_revision=0)
    records = snapshot_session_domain(database, "one")
    delete_session_domain_rows(database, "one")
    assert store.read("one", "plugins.example")["overrides"] == {}
    assert store.read("two", "plugins.example")["overrides"] == {"top_k": 9}
    restore_session_domain(database, "one", records)
    assert store.read("one", "plugins.example")["overrides"] == {"top_k": 8}
    with pytest.raises(ValueError, match="身份不匹配"):
        restore_session_domain(tmp_path / "other.db", "wrong", records)


def test_null_is_a_value_and_not_reset(tmp_path):
    schema = {"threshold": ConfigField("threshold", "number", 0.5, nullable=True)}
    service = PluginSettingsService(tmp_path / "platform.db", PluginConfigManager(tmp_path / "config"))
    record = service.save("one", "example", schema, {"threshold": None}, expected_revision=0)
    assert record["config"]["threshold"] is None
    assert record["sources"]["threshold"] == "session"
