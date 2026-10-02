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
