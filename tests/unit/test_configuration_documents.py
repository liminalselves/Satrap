"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import pytest
from typing import Any
from satrap.core.config.document import ConfigRevisionConflict, config_document_revision, create_default_config
from satrap.core.config.document import delete_platform, load_config_document, save_config_document, upsert_platform
import json
import os
from concurrent.futures import ThreadPoolExecutor
from satrap.core import config_paths
from satrap.core.config import versions
from satrap.core.config.versions import ConfigVersionService


def test_raw_yaml_preflight_uses_production_validation_without_writes(tmp_path):
    from satrap.core.config.document import validate_yaml_document

    before = list(tmp_path.iterdir())
    assert validate_yaml_document("session_classes:\n  # 空映射\nplatforms:\n  # 空列表\n") == {"valid": True, "errors": []}
    assert validate_yaml_document("session_scan_paths: []\nplatforms: []\n")["valid"]
    syntax = validate_yaml_document("platforms: [\n  {id: bad\n")
    assert not syntax["valid"]
    problem = syntax["errors"][0]
    assert problem["code"] == "yaml_syntax_error" and problem["line"] == 3 and problem["column"] == 1
    semantic = validate_yaml_document("platforms: invalid\n")
    assert not semantic["valid"] and semantic["errors"][0]["code"] == "invalid_config"
    assert semantic["errors"][0]["line"] is None and semantic["errors"][0]["column"] is None
    with pytest.raises(ValueError):
        validate_yaml_document("中" * (1024 * 1024))
    assert list(tmp_path.iterdir()) == before


async def test_maintenance_policy_cas_unique_owner_and_archive_lease(tmp_path, monkeypatch):
    import asyncio
    import time
    from satrap.core.config.maintenance_settings import MaintenanceRunner, MaintenanceSettingsStore
    from satrap.core.storage import StorageLayout, StorageMaintenanceService
    from satrap.core.storage.file_lock import FileLock
    from satrap.core.storage.layout import storage_key

    settings = MaintenanceSettingsStore(tmp_path / "policy.json")
    initial = settings.read()
    assert initial == {"trash_retention_days": 30, "auto_purge_enabled": False, "revision": "missing"}
    assert not settings.path.exists()
    layout = StorageLayout(tmp_path / "data")
    maintenance = StorageMaintenanceService(layout)
    layout.ensure_platform("chat")
    folder = layout.session_root("chat", "one")
    folder.mkdir(parents=True)
    (folder / "note.txt").write_text("恢复后仍在", encoding="utf-8")
    archive = maintenance.archive_session("chat", "one")
    archive_id = archive["archive_id"]
    target = layout.trash_root("chat") / "sessions" / archive_id
    os.utime(target, (time.time() - 40 * 86400,) * 2)
    def no_scan(*args, **kwargs):
        raise AssertionError("永久清理不能全库扫描")
    monkeypatch.setattr(StorageMaintenanceService, "scan", no_scan)
    runner = MaintenanceRunner(layout, store=settings, interval=3600)
    runner.cycle()
    assert target.exists()
    policy = settings.save({"trash_retention_days": 30, "auto_purge_enabled": True, "expected_revision": "missing"})
    with pytest.raises(ConfigRevisionConflict):
        settings.save({"trash_retention_days": 30, "auto_purge_enabled": False, "expected_revision": "missing"})
    assert settings.read() == policy
    lock = layout.platform_root("chat") / "locks" / f"archive-{storage_key(archive_id, fallback='archive')}.lock"
    with FileLock(lock, timeout=0):
        await asyncio.to_thread(runner.cycle)
        assert target.exists()
    runner.cycle()
    assert not target.exists()
    settings.save({"trash_retention_days": 30, "auto_purge_enabled": False, "expected_revision": policy["revision"]})
    first = MaintenanceRunner(layout, store=settings, interval=3600)
    second = MaintenanceRunner(layout, store=settings, interval=3600)
    try:
        assert await first.start()
        assert not await second.start()
        await asyncio.sleep(0)
    finally:
        await first.close()
        await second.close()
        await runner.close()

def test_stale_config_revision_preserves_latest_document(tmp_path: Path):
    """过期写入必须保留最新文档, 且错误不回显配置凭据"""
    path = tmp_path / "config.json"
    original = save_config_document(path, {"platforms": []})
    revision = config_document_revision(original)
    latest = save_config_document(path, {"platforms": [], "secret": "test-secret"}, expected_revision=revision)
    with pytest.raises(ConfigRevisionConflict) as error:
        save_config_document(path, original, expected_revision=revision)
    assert "test-secret" not in str(error.value)
    assert load_config_document(path) == latest
    assert config_document_revision({"a": 1, "b": 2}) == config_document_revision({"b": 2, "a": 1})

def test_config_revision_serializes_independent_processes(tmp_path: Path):
    """相同修订的跨进程保存只能有一个成功"""
    path = tmp_path / "config.json"
    revision = config_document_revision(save_config_document(path, {"platforms": []}))
    script = """
import sys
from satrap.core.config.document import ConfigRevisionConflict, save_config_document
try:
    save_config_document(sys.argv[1], {"platforms": [], "writer": sys.argv[3]}, expected_revision=sys.argv[2])
except ConfigRevisionConflict:
    print("conflict")
else:
    print("saved")
"""
    processes = [subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", script, str(path), revision, str(index)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    ) for index in range(2)]
    try:
        outputs = [process.communicate(timeout=30) for process in processes]
        assert all(process.returncode == 0 for process in processes), outputs
        assert sorted(output.strip() for output, _ in outputs) == ["conflict", "saved"]
        assert load_config_document(path)["writer"] in {"0", "1"}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()

def test_save_config_document_validates_and_replaces_atomically(tmp_path: Path):
    """配置保存应规范化平台并且不残留临时文件"""
    path = tmp_path / "config.yaml"
    config_data: dict[str, Any] = {
        "platforms": [
            {
                "id": " demo ",
                "type": " misskey ",
                "session_type": " assistant ",
                "settings": {},
            }
        ],
    }
    saved = save_config_document(
        path,
        config_data,
    )

    assert saved["platforms"][0]["id"] == "demo"
    assert saved["platforms"][0]["session_provider"] == "session_class"
    assert saved["platforms"][0]["session_type"] == "assistant"
    assert load_config_document(path) == saved
    assert list(tmp_path.glob(".*.tmp")) == []

def test_default_config_does_not_overwrite_existing_file(tmp_path: Path):
    """默认配置创建不应覆盖已有配置"""
    path = tmp_path / "config.yaml"
    save_config_document(path, {"api": {"port": 19999}, "platforms": []})

    result = create_default_config(path)

    assert result["api"]["port"] == 19999

def test_platform_crud_rejects_duplicates_and_missing_targets():
    """平台 CRUD 应拒绝重复 id 和不存在的更新或删除目标"""
    platforms = upsert_platform([], {"id": "main", "type": "misskey", "settings": {}})

    with pytest.raises(ValueError, match="已存在"):
        upsert_platform(platforms, {"id": "main", "type": "onebot", "settings": {}})
    with pytest.raises(ValueError, match="不存在"):
        upsert_platform(
            platforms,
            {"id": "renamed", "type": "misskey", "settings": {}},
            original_id="missing",
        )
    with pytest.raises(ValueError, match="不存在"):
        delete_platform(platforms, "missing")

def test_platform_update_preserves_list_position():
    """平台更新应保持原列表顺序"""
    platforms: list[dict[str, Any]] = [
        {"id": "first", "type": "misskey", "settings": {}},
        {"id": "second", "type": "onebot", "settings": {}},
    ]

    updated = upsert_platform(
        platforms,
        {"id": "first", "type": "misskey", "settings": {"base_url": "https://example.test"}},
        original_id="first",
    )

    assert [item["id"] for item in updated] == ["first", "second"]
    assert updated[0]["settings"]["base_url"] == "https://example.test"

def test_replace_with_retry_retries_transient_busy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """EACCES/EPERM/EBUSY 类瞬时占用会重试直至成功"""
    import errno
    import os
    from satrap.core.config import document

    attempts: list[int] = []
    real_replace = os.replace

    def flaky(source: str, target: str) -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError(errno.EBUSY, "busy")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", flaky)
    source = tmp_path / "a.tmp"
    target = tmp_path / "b.json"
    source.write_text("x", encoding="utf-8")
    document._replace_with_retry(source, target, attempts=5, interval=0)
    assert target.read_text(encoding="utf-8") == "x" and len(attempts) == 3

def test_replace_with_retry_does_not_retry_other_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """非占用类错误立即抛出, 不做无谓重试"""
    import errno
    import os
    from satrap.core.config import document

    def always_missing(source: str, target: str) -> None:
        raise OSError(errno.ENOENT, "missing")

    monkeypatch.setattr(os, "replace", always_missing)
    with pytest.raises(OSError):
        document._replace_with_retry(tmp_path / "a.tmp", tmp_path / "b.json", attempts=5, interval=0)

@pytest.fixture
def config_root(tmp_path, monkeypatch):
    """将默认配置及旧路径都隔离到临时目录"""
    root = tmp_path / ".satrap"
    root.mkdir()
    monkeypatch.setattr(config_paths, "_data_dir", lambda: root)
    monkeypatch.delenv("SATRAP_CONFIG_ROOT", raising=False)
    return root

@pytest.mark.parametrize("name", ["model_config.json", "session_class_config.json", "edictum_session_config.json", "chat_plugins.json", "logging.json"])
def test_legacy_config_preserves_bytes_and_does_not_recreate_old_path(config_root, name):
    content = '{"说明": "原始配置", "enabled": false}\n'.encode("utf-8")
    legacy = config_root / name
    legacy.write_bytes(content)
    target = config_paths.get_config_path(name)
    assert target == config_root / "config" / name
    assert target.read_bytes() == content and not legacy.exists()
    assert config_paths.get_config_path(name) == target

def test_new_config_wins_without_overwriting_either_copy(config_root):
    legacy = config_root / "model_config.json"
    legacy.write_text('{"old": true}', encoding="utf-8")
    target = config_root / "config" / legacy.name
    target.parent.mkdir()
    target.write_text('{"new": true}', encoding="utf-8")
    assert config_paths.get_config_path(legacy.name) == target
    assert target.read_text(encoding="utf-8") == '{"new": true}'
    assert legacy.read_text(encoding="utf-8") == '{"old": true}'

def test_concurrent_migration_keeps_one_complete_config(config_root):
    content = '{"模型": "同一配置"}\n'.encode("utf-8")
    for attempt in range(20):
        legacy = config_root / f"model_config_{attempt}.json"
        legacy.write_bytes(content)
        with ThreadPoolExecutor(max_workers=8) as executor:
            paths = list(executor.map(lambda _: config_paths.get_config_path(legacy.name), range(16)))
        assert all(path == config_root / "config" / legacy.name for path in paths)
        assert paths[0].read_bytes() == content and not legacy.exists()

def test_plugin_directory_migration_preserves_configs_and_locks(config_root):
    legacy = config_root / "plugin_config"
    legacy.mkdir()
    (legacy / "rag.json").write_text('{"说明": "知识库"}', encoding="utf-8")
    (legacy / ".rag.json.lock").write_bytes(b"\0")
    target = config_paths.get_config_path("plugins", legacy_name="plugin_config")
    assert target == config_root / "config" / "plugins" and not legacy.exists()
    assert json.loads((target / "rag.json").read_text(encoding="utf-8")) == {"说明": "知识库"}
    assert (target / ".rag.json.lock").read_bytes() == b"\0"

def test_explicit_config_root_leaves_old_project_config_alone(config_root, tmp_path, monkeypatch):
    legacy = config_root / "model_config.json"
    legacy.write_bytes(b"{}")
    custom = tmp_path / "custom"
    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(custom))
    assert config_paths.get_config_path(legacy.name) == custom / legacy.name
    assert legacy.exists() and not custom.exists()

def test_failed_migration_logs_and_uses_existing_config(config_root, monkeypatch):
    from satrap.core.log import managed

    legacy = config_root / "model_config.json"
    legacy.write_bytes(b'{"model": "keep"}')
    errors = []
    monkeypatch.setattr(managed.LogFileLock, "acquire", lambda self, timeout=0: False)
    monkeypatch.setattr(managed, "report_failure", errors.append)
    assert config_paths.get_config_path(legacy.name) == legacy
    assert legacy.read_bytes() == b'{"model": "keep"}'
    assert len(errors) == 1 and "配置迁移失败" in errors[0] and "TimeoutError" in errors[0]

@pytest.mark.parametrize("name", ["../outside.json", ".", "..", ""])
def test_path_escape_rejected(config_root, name):
    with pytest.raises(ValueError):
        config_paths.get_config_path(name)

def test_logger_cold_start_migrates_existing_policy_without_import_cycle(config_root):
    (config_root / "logging.json").write_text(
        json.dumps({"version": 1, "enabled": False, "retention_days": 90}), encoding="utf-8",
    )
    environment = {key: value for key, value in os.environ.items() if key not in {"SATRAP_CONFIG_ROOT", "SATRAP_LOG_CONFIG"}}
    environment["SATRAP_LOG_ROOT"] = str(config_root / "logs")
    script = """
import sys
from pathlib import Path
import types
package = types.ModuleType('satrap')
package.__path__ = [str(Path.cwd() / 'satrap')]
sys.modules['satrap'] = package
from satrap.core import config_paths
root = Path(sys.argv[1])
config_paths._data_dir = lambda: root
from satrap.core.log import logger
from satrap.core.log.policy import LoggingPolicyStore
store = LoggingPolicyStore()
assert store.path == root / 'config' / 'logging.json'
assert store.read()[0].retention_days == 90
assert store.read()[0].enabled is False
assert not (root / 'logging.json').exists()
assert logger.maintenance.store.path == store.path
logger.close()
"""
    result = subprocess.run([sys.executable, "-c", script, str(config_root)], env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr

def test_all_default_managers_use_central_config_directory(config_root, monkeypatch):
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.core.framework.SessionClassManager import SessionClassConfigManager
    from satrap.edictum.config import EdictumConfigManager
    from satrap.display.plugins import _default_state_path
    from satrap.core.log.policy import LoggingPolicyStore

    for key in ["SATRAP_MODEL_CONFIG_PATH", "SATRAP_SESSION_CLASS_CONFIG_PATH", "SATRAP_EDICTUM_CONFIG_PATH", "SATRAP_LOG_CONFIG"]:
        monkeypatch.delenv(key, raising=False)
    assert ModelConfigManager._default_storage_path() == config_root / "config" / "model_config.json"
    assert SessionClassConfigManager._default_storage_path() == config_root / "config" / "session_class_config.json"
    assert EdictumConfigManager._default_storage_path() == config_root / "config" / "edictum_session_config.json"
    assert _default_state_path() == config_root / "config" / "chat_plugins.json"
    assert LoggingPolicyStore().path == config_root / "config" / "logging.json"

    # 显式环境变量覆盖仍优先于集中目录
    for key, manager in [
        ("SATRAP_MODEL_CONFIG_PATH", ModelConfigManager),
        ("SATRAP_SESSION_CLASS_CONFIG_PATH", SessionClassConfigManager),
        ("SATRAP_EDICTUM_CONFIG_PATH", EdictumConfigManager),
    ]:
        custom = config_root / (key.lower() + ".json")
        monkeypatch.setenv(key, str(custom))
        assert manager._default_storage_path() == custom
    explicit = config_root / "separate-policy.json"
    assert LoggingPolicyStore(path=explicit).path == explicit

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
