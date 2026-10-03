"""验证配置迁移不丢数据, 自定义路径不被迁移, 日志冷启动无循环依赖"""
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from satrap.core import config_paths


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


def test_manager_specific_overrides_remain_unchanged(config_root, monkeypatch):
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.core.framework.SessionClassManager import SessionClassConfigManager
    from satrap.edictum.config import EdictumConfigManager
    from satrap.core.log.policy import LoggingPolicyStore

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
