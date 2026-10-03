"""独立插件管理目录的来源与引用行为测试"""
from satrap.core.config.plugin_service import PluginManagementService
from satrap.display.plugins import ChatPluginRegistry
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.core.config.document import ConfigRevisionConflict
import pytest


def test_catalog_sources_precedence_and_references_without_execution(tmp_path):
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    for base, name, version in [(builtin, "shared", "builtin"), (user, "shared", "shadow"), (user, "custom", "user")]:
        plugin = base / name
        plugin.mkdir(parents=True)
        (plugin / "meta.yaml").write_text(f"name: {name}\nversion: {version}\ntools:\n  probe: 测试\n", encoding="utf-8")
        (plugin / "tools.py").write_text("raise RuntimeError('不应执行插件代码')", encoding="utf-8")
    catalog = PluginCatalog(builtin, user)
    registry = ChatPluginRegistry(tmp_path / "chat.json")
    registry.catalog = catalog
    registry.set_enabled("shared", True)
    service = PluginManagementService(catalog, {
        "first": {"plugins": ["shared", "shared"]},
        "second": {"plugins": [{"name": "shared", "enabled": False}, {"name": "custom"}]},
        "empty": {"plugins": []},
    }, registry)
    result = {item["name"]: item for item in service.list_plugins()}
    assert result["shared"]["source"] == "builtin"
    assert result["shared"]["version"] == "builtin"
    assert result["shared"]["usage_count"] == 3
    assert result["shared"]["edictum_configs"] == ["first", "second"]
    assert result["custom"]["source"] == "user"
    assert result["custom"]["usage_count"] == 1
    assert not result["custom"]["chat_enabled"]
    assert result["custom"]["capabilities"]["tools"] == {"probe": "测试"}


@pytest.fixture
def configured_service(tmp_path):
    plugin = tmp_path / "user" / "probe"
    plugin.mkdir(parents=True)
    (plugin / "meta.yaml").write_text("""name: probe
tools:
  probe_tool: 测试工具
skills:
  probe_skill: 测试技能
config_schema:
  note:
    type: string
    default: default
  count:
    type: number
    default: 1
    minimum: 0
    maximum: 10
    integer: true
  model:
    type: llm
    default: ''
  optional_model:
    type: llm
    default: ''
    nullable: true
""", encoding="utf-8")
    catalog = PluginCatalog(tmp_path / "builtin", tmp_path / "user")
    chat = ChatPluginRegistry(tmp_path / "chat.json")
    chat.catalog = catalog
    return PluginManagementService(catalog, {}, chat, manager=PluginConfigManager(tmp_path / "config"))


def test_global_parameters_defaults_cas_and_restore(configured_service):
    service = configured_service
    first = service.get_config("probe")
    assert first["overrides"] == {}
    assert first["config"]["note"] == "default"
    saved = service.save_config("probe", {"note": "自定义", "count": 0}, first["revision"])
    assert saved["saved"]
    assert saved["config"]["count"] == 0
    with pytest.raises(ConfigRevisionConflict):
        service.save_config("probe", {"note": "旧页面覆盖"}, first["revision"])
    assert service.get_config("probe")["config"]["note"] == "自定义"
    restored = service.save_config("probe", {}, saved["revision"])
    assert restored["overrides"] == {}
    assert restored["config"]["note"] == "default"


@pytest.mark.parametrize("values", [{"undeclared": "x"}, {"count": -1}, {"count": 11}, {"count": 0.5}, {"note": None}])
def test_invalid_parameters_preserve_previous_file(configured_service, values):
    service = configured_service
    previous = service.get_config("probe")
    with pytest.raises(ValueError):
        service.save_config("probe", values, previous["revision"])
    assert service.get_config("probe") == previous


def test_global_model_reference_validation(configured_service):
    from types import SimpleNamespace
    service = configured_service
    service.models = SimpleNamespace(has_config=lambda kind, name: False)
    before = service.get_config("probe")
    with pytest.raises(ValueError, match="模型配置不存在"):
        service.save_config("probe", {"model": "missing"}, before["revision"])
    assert service.get_config("probe") == before


def test_global_explicit_null_is_preserved(configured_service):
    service = configured_service
    before = service.get_config("probe")
    saved = service.save_config("probe", {"optional_model": None}, before["revision"])
    assert saved["overrides"] == {"optional_model": None}
    assert saved["config"]["optional_model"] is None


def test_chat_usage_disable_preserves_capabilities_and_remove_clears_reference(configured_service):
    service = configured_service
    initial = service.get_usages("probe")["locations"][0]
    assert not initial["present"]
    added = service.save_usage("probe", "chat", "chat", {"present": True, "enabled": True, "capabilities": {"tools": {"probe_tool": False}}}, initial["revision"])["locations"][0]
    disabled = service.save_usage("probe", "chat", "chat", {"present": True, "enabled": False, "capabilities": added["capabilities"]}, added["revision"])["locations"][0]
    assert disabled["present"] and not disabled["enabled"]
    assert disabled["capabilities"]["tools"]["probe_tool"] is False
    assert service.list_plugins()[0]["usage_count"] == 1
    removed = service.save_usage("probe", "chat", "chat", {"present": False, "enabled": False, "capabilities": disabled["capabilities"]}, disabled["revision"])["locations"][0]
    assert not removed["present"]
    assert service.list_plugins()[0]["usage_count"] == 0


def test_chat_usage_rejects_stale_edits_and_unknown_capabilities(configured_service):
    service = configured_service
    initial = service.get_usages("probe")["locations"][0]
    external = ChatPluginRegistry(service.chat.state_path)
    external.catalog = service.catalog
    external.set_enabled("probe", True)
    with pytest.raises(ConfigRevisionConflict):
        service.save_usage("probe", "chat", "chat", {"present": True, "enabled": False, "capabilities": {}}, initial["revision"])
    current = service.get_usages("probe")["locations"][0]
    with pytest.raises(ValueError, match="未声明能力"):
        service.save_usage("probe", "chat", "chat", {"present": True, "enabled": True, "capabilities": {"tools": {"unknown": True}}}, current["revision"])
    assert service.get_usages("probe")["locations"][0]["enabled"]


def test_named_usage_preserves_overrides_and_other_plugins(configured_service, tmp_path, monkeypatch):
    from satrap.edictum.registry import create_default_edictum_type_registry
    from satrap.edictum.config import EdictumConfigManager
    from satrap.core.config.edictum_service import EdictumConfigService

    service = configured_service
    registry = create_default_edictum_type_registry()
    manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    manager.plugin_catalog = service.catalog
    manager.create("assistant", {"edictum_type": "async_simple", "plugins": [{"name": "probe", "config": {"note": "命名覆盖"}}, "other"]})
    named = EdictumConfigService(manager, registry)
    named.plugin_catalog = service.catalog
    monkeypatch.setattr("satrap.core.config.edictum_service.PluginConfigManager", lambda: service.manager)
    service.edictum = named
    service.configs = named.list_configs()
    before = service.get_usages("probe")["locations"][1]
    result = service.save_usage("probe", "edictum", "assistant", {"present": True, "enabled": False, "capabilities": {"skills": {"probe_skill": False}}}, before["revision"])
    after = named.get("assistant")
    target = next(item for item in after["plugins"] if item["name"] == "probe")
    assert target["config"] == {"note": "命名覆盖"}
    assert target["capabilities"]["skills"]["probe_skill"] is False
    assert next(item for item in after["plugins"] if item["name"] == "other")
    location = result["locations"][1]
    manager.update("assistant", {"description": "其他页面修改"})
    with pytest.raises(ConfigRevisionConflict):
        service.save_usage("probe", "edictum", "assistant", {"present": False, "enabled": False, "capabilities": {}}, location["revision"])
    assert len(manager.get_config("assistant")["plugins"]) == 2
