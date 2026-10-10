import json
from pathlib import Path

import pytest

from satrap.core.config import versions
from satrap.core.config.versions import ConfigVersionService


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = tmp_path / 'config'
    config.mkdir()
    monkeypatch.setenv('SATRAP_CONFIG_ROOT', str(config))
    monkeypatch.delenv('SATRAP_MODEL_CONFIG_PATH', raising=False)
    monkeypatch.delenv('SATRAP_SESSION_CLASS_CONFIG_PATH', raising=False)
    monkeypatch.delenv('SATRAP_EDICTUM_CONFIG_PATH', raising=False)
    monkeypatch.setattr(versions, 'CONFIG_DIR', config / 'plugins')
    monkeypatch.setattr(versions, 'PLUGINS_PRESET_DIR', tmp_path / 'builtin')
    monkeypatch.setattr(versions, 'USER_PLUGINS_DIR', tmp_path / 'user')
    main = tmp_path / 'main.yaml'
    main.write_text('api_key: secret-never-returned\n', encoding='utf-8')
    return ConfigVersionService(main, tmp_path), config


def test_resource_subset_is_private_and_warm_probe_does_not_parse(setup, monkeypatch):
    service, config = setup
    (config / 'edictum_session_config.json').write_text('{}', encoding='utf-8')
    first = service.get(['edictum_configs'])
    assert first['resources']['edictum_configs']['state'] == 'available'
    assert 'secret-never-returned' not in json.dumps(first)
    assert str(config) not in json.dumps(first)
    assert len(first['resources']) == 1
    monkeypatch.setattr(versions, 'safe_yaml_load', lambda text: pytest.fail('暖探测不应解析 YAML'))
    monkeypatch.setattr(versions.json, 'loads', lambda text: pytest.fail('暖探测不应解析 JSON'))
    assert service.get(['edictum_configs']) == first


def test_atomic_replace_damage_recovery_delete_and_recreate(setup):
    service, config = setup
    path = config / 'edictum_session_config.json'
    def read():
        return service.get(['edictum_configs'])['resources']['edictum_configs']
    missing = read()
    assert missing['state'] == 'missing'
    path.write_text('{}', encoding='utf-8')
    first = read()
    replacement = path.with_suffix('.new')
    replacement.write_text('{broken', encoding='utf-8')
    replacement.replace(path)
    damaged = read()
    assert damaged['state'] == 'damaged'
    assert first['change_token'] != damaged['change_token']
    path.write_text('{}', encoding='utf-8')
    assert read()['state'] == 'available'
    path.unlink()
    assert read()['state'] == 'missing'
    path.write_text('{}', encoding='utf-8')
    assert read()['change_token'] != first['change_token']


def test_main_paths_override_environment_and_external_path_switch(setup, monkeypatch):
    service, config = setup
    named = service.project_root / 'named.json'
    named.write_text('{}', encoding='utf-8')
    monkeypatch.setenv('SATRAP_EDICTUM_CONFIG_PATH', str(config / 'absent.json'))
    service.main_path.write_text('edictum_config_path: named.json\n', encoding='utf-8')
    assert service.get(['edictum_configs'])['resources']['edictum_configs']['state'] == 'available'
    service.main_path.write_text('{}', encoding='utf-8')
    assert service.get(['edictum_configs'])['resources']['edictum_configs']['state'] == 'missing'


def test_bad_main_does_not_substitute_default_paths(setup):
    service, config = setup
    (config / 'edictum_session_config.json').write_text('{}', encoding='utf-8')
    service.main_path.write_text('[not an object]', encoding='utf-8')
    result = service.get(['main_config', 'edictum_configs'])['resources']
    assert result['main_config']['state'] == 'damaged'
    assert result['edictum_configs']['state'] == 'unreadable'


def test_catalog_and_defaults_inventory_without_yaml_parsing(setup, monkeypatch):
    service, config = setup
    service.get(['main_config'])
    monkeypatch.setattr(versions, 'safe_yaml_load', lambda text: pytest.fail('目录探测不得解析插件 YAML'))
    plugin = versions.USER_PLUGINS_DIR / 'probe'
    plugin.mkdir(parents=True)
    meta = plugin / 'meta.yaml'
    meta.write_text('name: probe', encoding='utf-8')
    defaults = config / 'plugins'
    defaults.mkdir()
    before = service.get(['plugin_catalog', 'plugin_defaults'])['resources']
    meta.write_text('name: changed', encoding='utf-8')
    (defaults / 'probe.json').write_text('{bad', encoding='utf-8')
    after = service.get(['plugin_catalog', 'plugin_defaults'])['resources']
    assert after['plugin_catalog']['change_token'] != before['plugin_catalog']['change_token']
    assert after['plugin_defaults']['state'] == 'damaged'
    meta.unlink()
    assert service.get(['plugin_catalog'])['resources']['plugin_catalog']['change_token'] != after['plugin_catalog']['change_token']


def test_forced_content_check_detects_unchanged_stat_and_low_frequency_verifies(setup, monkeypatch):
    service, config = setup
    path = config / 'edictum_session_config.json'
    path.write_text('{"a":1}', encoding='utf-8')
    invalidations = []
    service._on_content_change = lambda: invalidations.append(True)
    before = service.get(['edictum_configs'])['resources']['edictum_configs']
    info = path.stat()
    original = Path.stat
    path.write_text('{"a":2}', encoding='utf-8')
    monkeypatch.setattr(Path, 'stat', lambda target, *args, **kwargs: info if target == path else original(target, *args, **kwargs))
    assert service.get(['edictum_configs'])['resources']['edictum_configs'] == before
    assert invalidations == []
    forced = service.get(['edictum_configs'], force=True)['resources']['edictum_configs']
    assert forced['change_token'] != before['change_token']
    assert invalidations == [True]
    path.write_text('{"a":3}', encoding='utf-8')
    service.verify_interval = 0
    assert service.get(['edictum_configs'])['resources']['edictum_configs']['change_token'] != forced['change_token']
    assert invalidations == [True, True]


def test_unreadable_and_unknown_resource(setup, monkeypatch):
    service, config = setup
    path = config / 'edictum_session_config.json'
    path.write_text('{}', encoding='utf-8')
    original = Path.open
    def denied(target, *args, **kwargs):
        if target == path:
            raise PermissionError('private-path')
        return original(target, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', denied)
    result = service.get(['edictum_configs'])
    assert result['resources']['edictum_configs']['state'] == 'unreadable'
    assert 'private-path' not in json.dumps(result)
    with pytest.raises(ValueError):
        service.get(['unknown'])
