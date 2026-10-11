"""单次应用快照的来源核验, 副本隔离和实例覆盖契约"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.edictum.plugin_apply import PluginApplySnapshot, logger
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.edictum.plugin_settings import resolve_runtime_specs
from satrap.edictum.plugin_spec import PluginSpec


def _sources(tmp_path):
    """
    创建完全隔离的插件及全局参数

    参数:
    - tmp_path: 测试目录

    返回:
    - 目录, 配置管理器及参数文件
    """
    plugin = tmp_path / "plugins" / "probe"
    plugin.mkdir(parents=True)
    (plugin / "meta.yaml").write_text("name: probe\nconfig_schema:\n  note:\n    type: str\n    default: default", encoding="utf-8")
    manager = PluginConfigManager(tmp_path / "globals")
    manager._dir.mkdir()
    path = manager._dir / "probe.json"
    path.write_text('{"note":"before"}', encoding="utf-8")
    return PluginCatalog(tmp_path / "builtin", tmp_path / "plugins"), manager, path


def test_snapshot_reads_globals_once_and_returns_private_copies(tmp_path, monkeypatch):
    """
    同次解析不重读全局字段, 返回副本不修改其他实例目标

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 读取计数夹具
    """
    catalog, manager, path = _sources(tmp_path)
    read = manager.load_global_explicit
    calls = []

    def counted(name, schema):
        """
        记录全局正文解析

        参数:
        - name: 插件名称
        - schema: 原字段声明

        返回:
        - 原读取结果
        """
        calls.append(name)
        return read(name, schema)

    monkeypatch.setattr(manager, "load_global_explicit", counted)
    snapshot = PluginApplySnapshot.capture(catalog, manager)
    copied = snapshot.global_config("probe")
    copied["note"] = "outside"
    assert snapshot.global_config("probe") == {"note": "before"}
    for index in range(5):
        session = SimpleNamespace(session_id=str(index))
        result = resolve_runtime_specs(session, [PluginSpec(name="probe")], snapshot.catalog, snapshot=snapshot)
        assert result[0].config == {"note": "before"}
    assert calls == ["probe"]
    path.write_text('{"note":"after"}', encoding="utf-8")
    assert not snapshot.is_current()
    assert snapshot.global_config("probe") == {"note": "before"}
    assert PluginApplySnapshot.capture(catalog, manager).global_config("probe") == {"note": "after"}
    assert calls == ["probe", "probe"]


def test_snapshot_keeps_named_and_fresh_instance_overrides(tmp_path):
    """
    快照固定全局字段, 会话覆盖仍即时读取且不跨实例传播

    参数:
    - tmp_path: 隔离目录
    """
    catalog, manager, _path = _sources(tmp_path)
    snapshot = PluginApplySnapshot.capture(catalog, manager)
    store = SessionOverrideStore(tmp_path / "instances.db")
    store.replace("one", "plugins.probe", {"note": "local-one"}, expected_revision=0)
    one = SimpleNamespace(session_id="one", plugin_override_store=store)
    two = SimpleNamespace(session_id="two", plugin_override_store=store)
    spec = PluginSpec(name="probe", config={"note": "named"})
    first = resolve_runtime_specs(one, [spec], snapshot.catalog, snapshot=snapshot)
    second = resolve_runtime_specs(two, [spec], snapshot.catalog, snapshot=snapshot)
    assert first[0].config["note"] == "local-one"
    assert second[0].config["note"] == "named"
    store.replace("one", "plugins.probe", {"note": "new-local"}, expected_revision=1)
    assert resolve_runtime_specs(one, [spec], snapshot.catalog, snapshot=snapshot)[0].config["note"] == "new-local"
    assert snapshot.global_config("probe")["note"] == "before"


def test_capture_retries_when_source_changes_during_read(tmp_path, monkeypatch):
    """
    捕获中途变化时整次重读, 不保留旧读取正文与新版本标识

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 来源变化夹具
    """
    catalog, manager, path = _sources(tmp_path)
    read = manager.load_global_explicit
    calls = []

    def changing(name, schema):
        """
        在首次读取结束后改写来源

        参数:
        - name: 插件名称
        - schema: 字段定义

        返回:
        - 改写前的读取结果
        """
        calls.append(name)
        result = read(name, schema)
        if len(calls) == 1:
            path.write_text('{"note":"changed"}', encoding="utf-8")
        return result

    monkeypatch.setattr(manager, "load_global_explicit", changing)
    snapshot = PluginApplySnapshot.capture(catalog, manager)
    assert snapshot.global_config("probe") == {"note": "changed"}
    assert calls == ["probe", "probe"]
    assert snapshot.is_current()


def test_corrupt_source_is_logged_and_never_becomes_default_snapshot(tmp_path, monkeypatch):
    """
    损坏配置明确失败, 不安装默认参数来替代已保存配置

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 日志捕获夹具
    """
    catalog, manager, path = _sources(tmp_path)
    path.write_text("broken", encoding="utf-8")
    messages = []
    monkeypatch.setattr(logger, "error", messages.append)
    with pytest.raises(ValueError):
        PluginApplySnapshot.capture(catalog, manager)
    assert len(messages) == 1 and "Traceback" in messages[0]


def test_missing_source_uses_defaults_and_later_file_or_meta_invalidates(tmp_path):
    """
    缺失文件按默认值, 新建来源和元数据编辑使排队快照失效

    参数:
    - tmp_path: 隔离目录
    """
    catalog, manager, path = _sources(tmp_path)
    path.unlink()
    snapshot = PluginApplySnapshot.capture(catalog, manager)
    assert snapshot.global_config("probe") == {"note": "default"}
    path.write_text("{}", encoding="utf-8")
    assert not snapshot.is_current()
    snapshot = PluginApplySnapshot.capture(catalog, manager)
    meta = tmp_path / "plugins" / "probe" / "meta.yaml"
    meta.write_text(meta.read_text(encoding="utf-8") + "\nversion: '2'", encoding="utf-8")
    assert not snapshot.is_current()
    old_entry = snapshot.catalog.get("probe")
    new_entry = PluginApplySnapshot.capture(catalog, manager).catalog.get("probe")
    if old_entry is None or new_entry is None:
        raise AssertionError("测试插件未加载")
    assert old_entry.version != "2"
    assert new_entry.version == "2"
