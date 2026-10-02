"""独立插件管理目录的来源与引用行为测试"""
from satrap.core.config.plugin_service import PluginManagementService
from satrap.display.plugins import ChatPluginRegistry
from satrap.edictum.plugin_catalog import PluginCatalog


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
