from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from satrap.core.backend.BackendManager import BackendManager
from satrap.admin_utils.log_state import read_log_increment


def test_backend_health_with_no_adapter_manager_returns_empty_adapters():
    """没有适配器管理器时 health 仍可正常返回"""
    backend = BackendManager()
    backend._running = True

    health = asyncio.run(backend.health())

    assert health["running"] is True
    assert health["adapters"] == {}
    assert health["platform_count"] == 0


@dataclass
class _FakeConfig:
    id: str = "fake"
    type: str = "test"


class _FakeAdapter:
    started = True
    config = _FakeConfig()

    def get_stats(self) -> dict:
        return {
            "status": "running",
            "started": True,
            "started_at": "2026-05-15T00:00:00",
            "error_count": 0,
            "last_error": None,
            "config_id": "fake",
            "config_type": "test",
        }


def test_backend_health_uses_adapter_stats_dict():
    """health 的 adapters 字段应为前端可直接读取的 dict"""
    backend = BackendManager()
    backend._running = True
    backend._adapter_mgr = SimpleNamespace(
        _adapters={"fake": _FakeAdapter()},
        list_adapters=lambda: ["fake"],
    )

    health = asyncio.run(backend.health())

    assert isinstance(health["adapters"], dict)
    assert health["adapters"]["fake"]["status"] == "running"
    assert health["adapters"]["fake"]["started"] is True
    assert health["adapters"]["fake"]["config_type"] == "test"


def test_read_log_increment_keeps_cached_lines_when_no_new_data(tmp_path):
    """没有新增日志时仍显示已有缓冲内容"""
    log_file = tmp_path / "satrap.log"
    log_file.write_bytes("first\nsecond\n".encode("utf-8"))

    position, cached, display = read_log_increment(log_file, 0, [], 10, paused=False)
    assert display == ["first\n", "second\n"]

    position, cached, display = read_log_increment(log_file, position, cached, 10, paused=False)
    assert display == ["first\n", "second\n"]


def test_read_log_increment_paused_does_not_advance_position(tmp_path):
    """暂停时不推进读取位置, 也不清空缓冲"""
    log_file = tmp_path / "satrap.log"
    log_file.write_bytes("first\nsecond\n".encode("utf-8"))

    position, cached, display = read_log_increment(log_file, 0, ["old\n"], 10, paused=True)

    assert position == 0
    assert cached == ["old\n"]
    assert display == ["old\n"]


def test_read_log_increment_resets_after_truncate(tmp_path):
    """日志截断后应从新文件开头读取"""
    log_file = tmp_path / "satrap.log"
    log_file.write_bytes("new\n".encode("utf-8"))

    position, cached, display = read_log_increment(log_file, 100, ["old\n"], 10, paused=False)

    assert position == len("new\n")
    assert cached == ["new\n"]
    assert display == ["new\n"]
