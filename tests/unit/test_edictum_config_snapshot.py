"""Edictum 只读配置快照, 来源变化与归一化回归"""
import json

import pytest

from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.registry import create_default_edictum_type_registry


def test_constructed_readers_share_normalization_but_not_mutable_data(tmp_path, monkeypatch):
    registry = create_default_edictum_type_registry()
    path = tmp_path / 'edictum.json'
    writer = EdictumConfigManager(registry, path)
    writer.create('agent', {'edictum_type': 'async_simple', 'params': {}})
    calls = []
    normalize = EdictumConfigManager._normalize_entry

    def counted(self, entry, catalog=None):
        calls.append(1)
        return normalize(self, entry, catalog)

    monkeypatch.setattr(EdictumConfigManager, '_normalize_entry', counted)
    first = EdictumConfigManager(registry, path)
    first._configs['agent']['description'] = 'private change'
    second = EdictumConfigManager(registry, path)
    second_config = second.get_config('agent')
    assert second_config is not None and second_config['description'] == ''
    assert calls == [1]
    second.reload()
    assert calls == [1, 1]
    second.update('agent', {'description': 'saved'})
    assert len(calls) == 4
    saved = EdictumConfigManager(registry, path).get_config('agent')
    assert saved is not None and saved['description'] == 'saved'


def test_external_edit_damage_recovery_and_type_revocation(tmp_path):
    registry = create_default_edictum_type_registry()
    path = tmp_path / 'edictum.json'
    writer = EdictumConfigManager(registry, path)
    writer.create('agent', {'edictum_type': 'async_simple'})
    assert EdictumConfigManager(registry, path).get_config('agent')
    data = json.loads(path.read_text(encoding='utf-8'))
    data['agent']['description'] = 'external'
    replacement = path.with_suffix('.new')
    replacement.write_text(json.dumps(data), encoding='utf-8')
    replacement.replace(path)
    external = EdictumConfigManager(registry, path).get_config('agent')
    assert external is not None and external['description'] == 'external'
    path.write_text('{invalid', encoding='utf-8')
    with pytest.raises(ValueError):
        EdictumConfigManager(registry, path)
    path.write_text(json.dumps(data), encoding='utf-8')
    restored = EdictumConfigManager(registry, path).get_config('agent')
    assert restored is not None and restored['description'] == 'external'
    registry.unregister('async_simple')
    with pytest.raises(ValueError, match='未知 Edictum 类型'):
        EdictumConfigManager(registry, path)


def test_metadata_change_invalidates_normalized_config(tmp_path):
    registry = create_default_edictum_type_registry()
    plugin = tmp_path / 'plugins' / 'probe'
    plugin.mkdir(parents=True)
    meta = plugin / 'meta.yaml'
    meta.write_text('name: probe\ntools:\n  first: first\n', encoding='utf-8')
    catalog = PluginCatalog(tmp_path / 'builtin', tmp_path / 'plugins')
    path = tmp_path / 'edictum.json'
    writer = EdictumConfigManager(registry, path, plugin_catalog=catalog)
    writer.create('agent', {'edictum_type': 'async_simple', 'plugins': ['probe']})
    before = EdictumConfigManager(registry, path, plugin_catalog=catalog)
    before_config = before.get_config('agent')
    assert before_config is not None and before_config['plugins'][0]['capabilities']['tools'] == {'first': True}
    meta.write_text('name: probe\ntools:\n  first: first\n  second: second\n', encoding='utf-8')
    after = EdictumConfigManager(registry, path, plugin_catalog=catalog)
    after_config = after.get_config('agent')
    assert after_config is not None and after_config['plugins'][0]['capabilities']['tools'] == {'first': True, 'second': True}


def test_legacy_normalization_is_read_only_and_runs_once_per_snapshot(tmp_path, monkeypatch):
    """旧格式读取只归一化内存, 不改变来源指纹或触发快照重试"""
    path = tmp_path / 'edictum.json'
    original = json.dumps({' agent ': {'edictum_type': 'async_simple', 'params': {}}})
    path.write_text(original, encoding='utf-8')
    fingerprint = path.stat()
    registry = create_default_edictum_type_registry()
    normalize = EdictumConfigManager._normalize_entry
    calls = []

    def counted(self, entry, catalog=None):
        calls.append(1)
        return normalize(self, entry, catalog)

    monkeypatch.setattr(EdictumConfigManager, '_normalize_entry', counted)
    first = EdictumConfigManager(registry, path)
    second = EdictumConfigManager(registry, path)
    assert first.get_config('agent') == second.get_config('agent')
    assert calls == [1]
    assert path.read_text(encoding='utf-8') == original
    current = path.stat()
    assert (current.st_mtime_ns, current.st_ctime_ns, current.st_size, current.st_ino) == (
        fingerprint.st_mtime_ns, fingerprint.st_ctime_ns, fingerprint.st_size, fingerprint.st_ino)
    second.update('agent', {'description': 'saved'})
    persisted = json.loads(path.read_text(encoding='utf-8'))
    assert list(persisted) == ['agent']
    assert persisted['agent']['description'] == 'saved'
