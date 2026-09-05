from __future__ import annotations

from dataclasses import dataclass
import asyncio
from pathlib import Path
import pytest
from typing import Any, cast
from types import SimpleNamespace

from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.log.reader import read_log_increment
from satrap.core.platform import EventDispatcher


@pytest.mark.asyncio
async def test_backend_health_with_no_adapter_manager_returns_empty_adapters():
    """没有适配器管理器时 health 仍可正常返回"""
    backend = BackendManager()
    backend._running = True

    health = await backend.health()

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

    def get_stats(self) -> dict[str, Any]:
        return {
            "status": "running",
            "started": True,
            "started_at": "2026-05-15T00:00:00",
            "error_count": 0,
            "last_error": None,
            "config_id": "fake",
            "config_type": "test",
        }


@pytest.mark.asyncio
async def test_backend_health_uses_adapter_stats_dict(monkeypatch: pytest.MonkeyPatch):
    """health 的 adapters 字段应为前端可直接读取的 dict"""
    backend = BackendManager()
    backend._running = True
    monkeypatch.setattr(
        backend,
        "_adapter_mgr",
        SimpleNamespace(
            _adapters={"fake": _FakeAdapter()},
            list_adapters=lambda: ["fake"],
        ),
    )

    health = await backend.health()

    assert isinstance(health["adapters"], dict)
    assert health["adapters"]["fake"]["status"] == "running"
    assert health["adapters"]["fake"]["started"] is True
    assert health["adapters"]["fake"]["config_type"] == "test"


class _RestartingDispatcher:
    """首次失败后保持运行的事件分发器替身"""

    def __init__(self) -> None:
        """初始化调用计数与重启通知"""
        self.calls = 0
        self.restarted = asyncio.Event()
        self.keep_running = asyncio.Event()

    async def dispatch_loop(self) -> None:
        """首次调用抛错, 第二次调用等待测试结束"""
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("dispatch failed")
        self.restarted.set()
        await self.keep_running.wait()


@pytest.mark.asyncio
async def test_dispatch_supervisor_restarts_failed_loop_and_reports_health() -> None:
    """事件分发异常后应自动重启并在健康状态中保留故障记录"""
    backend = BackendManager()
    dispatcher = _RestartingDispatcher()
    backend._dispatcher = cast(EventDispatcher, dispatcher)
    backend._dispatch_restart_base_delay = 0
    backend._running = True
    backend._dispatch_task = asyncio.create_task(backend._dispatch_loop())

    await asyncio.wait_for(dispatcher.restarted.wait(), timeout=1)
    health = await backend.health()

    assert dispatcher.calls == 2
    assert health["healthy"] is True
    assert health["dispatch"] == {
        "status": "running",
        "healthy": True,
        "restart_count": 1,
        "last_error": "dispatch failed",
    }

    backend._running = False
    backend._dispatch_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await backend._dispatch_task


@pytest.mark.asyncio
async def test_health_endpoint_returns_503_while_dispatch_is_restarting() -> None:
    """事件分发处于重启退避时健康端点应明确返回降级状态"""
    backend = BackendManager()
    backend._running = True
    backend._dispatcher = cast(EventDispatcher, _RestartingDispatcher())
    backend._dispatch_state = "restarting"
    backend._dispatch_task = asyncio.current_task()
    server = BackendHTTPServer(backend)

    status, payload = await server._route("GET", "/api/health", b"")

    assert status == 503
    assert payload["running"] is True
    assert payload["healthy"] is False
    assert payload["dispatch"]["status"] == "restarting"


def test_read_log_increment_keeps_cached_lines_when_no_new_data(tmp_path: Path):
    """
    没有新增日志时仍显示已有缓冲内容

    参数:
    - tmp_path: tmp路径
    """
    log_file = tmp_path / "satrap.log"
    log_file.write_bytes("first\nsecond\n".encode("utf-8"))

    position, cached, display = read_log_increment(log_file, 0, [], 10, paused=False)
    assert display == ["first\n", "second\n"]

    position, cached, display = read_log_increment(log_file, position, cached, 10, paused=False)
    assert display == ["first\n", "second\n"]


def test_read_log_increment_paused_does_not_advance_position(tmp_path: Path):
    """
    暂停时不推进读取位置, 也不清空缓冲

    参数:
    - tmp_path: tmp路径
    """
    log_file = tmp_path / "satrap.log"
    log_file.write_bytes("first\nsecond\n".encode("utf-8"))

    position, cached, display = read_log_increment(log_file, 0, ["old\n"], 10, paused=True)

    assert position == 0
    assert cached == ["old\n"]
    assert display == ["old\n"]


def test_read_log_increment_resets_after_truncate(tmp_path: Path):
    """
    日志截断后应从新文件开头读取

    参数:
    - tmp_path: tmp路径
    """
    log_file = tmp_path / "satrap.log"
    log_file.write_bytes("new\n".encode("utf-8"))

    position, cached, display = read_log_increment(log_file, 100, ["old\n"], 10, paused=False)

    assert position == len("new\n")
    assert cached == ["new\n"]
    assert display == ["new\n"]
