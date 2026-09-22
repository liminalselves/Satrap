"""平台生命周期隔离: 单个平台配置错误, 启动/停止失败与主循环退出不影响后端整体"""
from unittest.mock import AsyncMock
from typing import Any
import asyncio
import logging

import pytest

from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.platform.event import PlatformMetadata
from satrap.core.platform import PlatformAdapter, PlatformAdapterManager, PlatformConfig, PlatformStatus


class _Adapter(PlatformAdapter):
    adapter_type = "lifecycle-test"

    async def run(self) -> None:
        mode = self.config.settings.get("mode")
        if mode == "raise":
            raise RuntimeError("boom")
        if mode == "return":
            return
        await asyncio.Future()

    async def start(self) -> None:
        if self.config.settings.get("mode") == "start_fail":
            raise RuntimeError("start failed")
        await super().start()

    async def terminate(self) -> None:
        if self.config.settings.get("mode") == "stop_fail":
            raise RuntimeError("stop failed")
        await super().terminate()

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(name=self.config.id, id=self.config.id)


def _adapter(pid: str, **settings: object) -> _Adapter:
    return _Adapter(PlatformConfig(id=pid, type="lifecycle-test", settings=dict(settings)))


@pytest.mark.asyncio
async def test_run_task_done_callback_marks_error_for_raise_and_silent_return():
    raising, returning = _adapter("a", mode="raise"), _adapter("b", mode="return")
    await raising.start()
    await returning.start()
    await asyncio.sleep(0.01)
    assert raising.status is PlatformStatus.ERROR and "RuntimeError" in str(raising._errors[-1])
    assert returning.status is PlatformStatus.ERROR and "意外结束" in str(returning._errors[-1])
    healthy = _adapter("c")
    await healthy.start()
    await asyncio.sleep(0)
    assert healthy.status is PlatformStatus.RUNNING
    await healthy.stop()
    assert healthy.status is PlatformStatus.STOPPED


@pytest.mark.asyncio
async def test_start_all_and_stop_all_isolate_failures(caplog: pytest.LogCaptureFixture):
    manager = PlatformAdapterManager()
    bad_start, good, bad_stop = _adapter("x", mode="start_fail"), _adapter("y"), _adapter("z", mode="stop_fail")
    manager._adapters = {"x": bad_start, "y": good, "z": bad_stop}
    await manager.start_all()
    assert bad_start.status is PlatformStatus.ERROR and good.started and bad_stop.started
    with caplog.at_level(logging.WARNING):
        await manager.stop_all()
    assert not good.started
    assert any("停止 z 失败" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_init_platforms_isolates_bad_config_and_reports_it(monkeypatch: pytest.MonkeyPatch):
    from satrap.core.platform import registry
    registry.register("lifecycle-test", _Adapter)
    monkeypatch.setattr(PlatformAdapterManager, "start_all", AsyncMock())
    platforms: list[Any] = [
        {"id": "good", "type": "lifecycle-test", "settings": {}},
        {"id": "bad", "type": "lifecycle-test", "settings": "not-a-dict"},
        {"id": "badport", "type": "onebot", "settings": {"port": "abc"}},
        "garbage",
        {"id": "", "type": "lifecycle-test"},
    ]
    backend = BackendManager(BackendConfig(platforms=platforms))
    try:
        await backend._init_platforms()
        assert backend._adapter_mgr is not None
        assert set(backend._adapter_mgr._adapters) == {"good"}
        failed = {item["id"]: item for item in backend._platform_config_results}
        assert set(failed) == {"bad", "badport"} and all(item["status"] == "failed" for item in failed.values())
        assert "ValueError" in failed["bad"]["error"] and "ValueError" in failed["badport"]["error"]
        health = await backend.health()
        assert {item["id"] for item in health["platform_config"]} == {"bad", "badport"}
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_health_reflects_errored_adapters(monkeypatch: pytest.MonkeyPatch):
    backend = BackendManager(BackendConfig(platforms=[]))
    backend._running = True
    manager = PlatformAdapterManager()
    broken, fine = _adapter("broken", mode="raise"), _adapter("fine")
    manager._adapters = {"broken": broken, "fine": fine}
    backend._adapter_mgr = manager
    await manager.start_all()
    await asyncio.sleep(0.01)
    try:
        health = await backend.health()
        assert health["healthy"] is False and health["adapters_errored"] == ["broken"]
        broken.config = PlatformConfig(id="broken", type="lifecycle-test", enable=False)
        health = await backend.health()
        assert health["healthy"] is True and health["adapters_errored"] == []
    finally:
        await manager.stop_all()
