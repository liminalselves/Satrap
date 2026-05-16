from __future__ import annotations

from argparse import Namespace

from satrap.cli.backend_lock import BackendInstanceLock
from satrap.cli.client import DaemonInfo
from satrap.cli.cmd_run import load_run_config
from satrap.core.backend.BackendManager import BackendConfig


def test_daemon_info_from_config_uses_config_values(monkeypatch):
    """Daemon 地址默认来自 BackendConfig"""
    monkeypatch.delenv("SATRAP_API_HOST", raising=False)
    monkeypatch.delenv("SATRAP_API_PORT", raising=False)

    config = BackendConfig(api_host="127.0.0.2", api_port=19871)
    daemon = DaemonInfo.from_config(config)

    assert daemon.host == "127.0.0.2"
    assert daemon.port == 19871
    assert daemon.base_url == "http://127.0.0.2:19871"


def test_daemon_info_from_config_allows_env_override(monkeypatch):
    """环境变量优先覆盖配置中的 API 地址"""
    monkeypatch.setenv("SATRAP_API_HOST", "127.0.0.3")
    monkeypatch.setenv("SATRAP_API_PORT", "19872")

    daemon = DaemonInfo.from_config(BackendConfig(api_host="127.0.0.2", api_port=19871))

    assert daemon.host == "127.0.0.3"
    assert daemon.port == 19872


def test_load_run_config_allows_cli_api_override(tmp_path, monkeypatch):
    """run 命令的 --api-host/--api-port 应覆盖配置文件"""
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"api": {"host": "127.0.0.2", "port": 19871}}',
        encoding="utf-8",
    )

    args = Namespace(
        config=str(config_path),
        api_host="127.0.0.4",
        api_port=19873,
    )
    config = load_run_config(args)

    assert config.api_host == "127.0.0.4"
    assert config.api_port == 19873


def test_backend_instance_lock_is_exclusive(tmp_path):
    """同一个锁文件同时只能被一个后端实例持有"""
    lock_path = tmp_path / "backend.lock"
    first = BackendInstanceLock(lock_path)
    second = BackendInstanceLock(lock_path)

    assert first.acquire("127.0.0.1", 19870)
    try:
        assert not second.acquire("127.0.0.1", 19870)
    finally:
        first.release()

    assert second.acquire("127.0.0.1", 19870)
    second.release()
