"""业务链路场景与集中边界契约"""
from __future__ import annotations

from unittest.mock import AsyncMock
from collections.abc import Awaitable
import asyncio
from pathlib import Path
import pytest
from typing import cast
from satrap.core.platform.misskey.client import AuthenticationError, MisskeyAPI
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.misskey.adapter import MisskeyAdapter
from satrap.core.platform.connection import ConnectionProbeError
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.platform import PlatformAdapterManager, PlatformConfig
from typing import Any
import logging
from satrap.core.platform.event import PlatformMetadata
from satrap.core.platform import PlatformAdapter, PlatformStatus
from argparse import Namespace
from satrap.cli.backend_lock import BackendInstanceLock
from satrap.cli.cmd_run import load_run_config
from satrap.cli.client import DaemonInfo

def onebot() -> OneBotAdapter:
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot"))
    adapter.started = True
    adapter.bot_self_id = "123"
    adapter._client_connected = True
    adapter._bot = AsyncMock()
    adapter._bot.get_version_info.return_value = {"app_name": "NapCat", "app_version": "1"}
    return adapter

def misskey() -> MisskeyAdapter:
    adapter = MisskeyAdapter(PlatformConfig(id="mk", type="misskey"))
    adapter.started = True
    adapter._running = True
    adapter.bot_self_id = "user"
    adapter._client = cast(MisskeyAPI, AsyncMock(spec=MisskeyAPI))
    cast(AsyncMock, adapter._client.get_current_user).return_value = {"id": "user"}
    return adapter

@pytest.mark.asyncio
async def test_onebot_probes_bound_connection_without_group_or_message():
    adapter = onebot()
    await adapter.check_connection()
    adapter._bot.get_version_info.assert_awaited_once_with(self_id="123")
    adapter._bot.send_group_msg.assert_not_called()

@pytest.mark.asyncio
async def test_onebot_listener_without_client_is_not_connected():
    adapter = onebot()
    adapter._bot = None
    with pytest.raises(ConnectionProbeError, match="尚未连接"):
        await adapter.check_connection()

@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, {}, {"app_name": 1}])
async def test_onebot_rejects_invalid_response(response: object):
    adapter = onebot()
    adapter._bot.get_version_info.return_value = response
    with pytest.raises(ConnectionProbeError, match="无效"):
        await adapter.check_connection()

@pytest.mark.asyncio
async def test_onebot_reconnect_during_probe_invalidates_response():
    adapter = onebot()

    async def reconnect(**_params: object) -> dict[str, str]:
        adapter._connection_generation += 1
        return {"app_name": "NapCat"}

    adapter._bot.get_version_info.side_effect = reconnect
    with pytest.raises(ConnectionProbeError, match="连接已变化"):
        await adapter.check_connection()

@pytest.mark.asyncio
async def test_misskey_probes_authenticated_api_without_sending():
    adapter = misskey()
    assert adapter._client is not None
    await adapter.check_connection()
    cast(AsyncMock, adapter._client.get_current_user).assert_awaited_once_with()
    cast(AsyncMock, adapter._client.create_note).assert_not_called()

@pytest.mark.asyncio
async def test_misskey_auth_failure_has_safe_detail():
    adapter = misskey()
    assert adapter._client is not None
    cast(AsyncMock, adapter._client.get_current_user).side_effect = AuthenticationError("secret-token")
    with pytest.raises(ConnectionProbeError, match="认证失败") as error:
        await adapter.check_connection()
    assert "secret-token" not in str(error.value)

@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, {}, {"id": "other"}])
async def test_misskey_rejects_invalid_or_changed_account(response: object):
    adapter = misskey()
    assert adapter._client is not None
    cast(AsyncMock, adapter._client.get_current_user).return_value = response
    with pytest.raises(ConnectionProbeError):
        await adapter.check_connection()

def backend(tmp_path: Path, adapter: OneBotAdapter | None) -> BackendManager:
    manager = BackendManager(BackendConfig(data_root=str(tmp_path)))
    manager._adapter_mgr = PlatformAdapterManager()
    if adapter:
        manager._adapter_mgr._adapters[adapter.config.id] = adapter
    return manager

@pytest.mark.asyncio
async def test_probe_endpoint_returns_result_for_encoded_platform_id(tmp_path: Path):
    adapter = onebot()
    adapter.config.id = "bot 中文"
    server = BackendHTTPServer(backend(tmp_path, adapter))
    status, result = await server._route("POST", "/api/platforms/bot%20%E4%B8%AD%E6%96%87/connection-test", b"{}")
    assert status == 200
    assert result["ok"] is True
    assert result["elapsed_ms"] >= 0
    assert "对端已响应" in result["detail"]
    status, _ = await server._route("GET", "/api/platforms/bot/connection-test", b"")
    assert status == 404

@pytest.mark.asyncio
async def test_missing_or_stopped_platform_does_not_probe(tmp_path: Path):
    manager = backend(tmp_path, None)
    assert (await manager.check_platform_connection("bot")).ok is False
    adapter = onebot()
    adapter.started = False
    assert manager._adapter_mgr is not None
    manager._adapter_mgr._adapters["bot"] = adapter
    assert (await manager.check_platform_connection("bot")).ok is False
    adapter._bot.get_version_info.assert_not_called()

@pytest.mark.asyncio
async def test_probe_timeout_is_bounded_and_cancels_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    adapter = onebot()
    cancelled = asyncio.Event()

    async def hung(**_params: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    adapter._bot.get_version_info.side_effect = hung
    wait_for = asyncio.wait_for

    async def short_wait(awaitable: Awaitable[None], timeout: float) -> None:
        assert timeout == 8.0
        return await wait_for(awaitable, 0.01)

    monkeypatch.setattr(asyncio, "wait_for", short_wait)
    result = await backend(tmp_path, adapter).check_platform_connection("bot")
    assert result.ok is False
    assert "超时" in result.detail
    assert cancelled.is_set()

@pytest.mark.asyncio
async def test_generic_error_does_not_expose_raw_response(tmp_path: Path):
    adapter = onebot()
    adapter._bot.get_version_info.side_effect = RuntimeError("secret-token")
    result = await backend(tmp_path, adapter).check_platform_connection("bot")
    assert result.ok is False
    assert "RuntimeError" in result.detail
    assert "secret-token" not in result.detail

@pytest.mark.asyncio
async def test_replaced_platform_cannot_report_success(tmp_path: Path):
    adapter = onebot()
    manager = backend(tmp_path, adapter)
    assert manager._adapter_mgr is not None
    adapter_manager = manager._adapter_mgr

    async def replace(**_params: object) -> dict[str, str]:
        adapter_manager._adapters["bot"] = onebot()
        return {"app_name": "NapCat"}

    adapter._bot.get_version_info.side_effect = replace
    result = await manager.check_platform_connection("bot")
    assert result.ok is False
    assert "实例已变化" in result.detail

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
async def test_invalid_platform_config_log_omits_settings_secrets(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    """跳过无效平台配置的警告只带 id/type, 不泄露 settings 中的凭据"""
    monkeypatch.setattr(PlatformAdapterManager, "start_all", AsyncMock())
    platforms: list[Any] = [
        {"id": "", "type": "onebot", "settings": {"access_token": "tok-secret", "secret": "tok-secret"}},
        "garbage-tok-secret",
    ]
    backend = BackendManager(BackendConfig(platforms=platforms))
    try:
        with caplog.at_level(logging.WARNING):
            await backend._init_platforms()
        messages = [record.getMessage() for record in caplog.records]
        assert any("跳过无效平台配置" in message for message in messages)
        assert not any("tok-secret" in message for message in messages)
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

def test_daemon_info_from_config_uses_config_values(monkeypatch: pytest.MonkeyPatch):
    """Daemon 地址默认来自 BackendConfig"""
    monkeypatch.delenv("SATRAP_API_HOST", raising=False)
    monkeypatch.delenv("SATRAP_API_PORT", raising=False)

    config = BackendConfig(api_host="127.0.0.2", api_port=19871)
    daemon = DaemonInfo.from_config(config)

    assert daemon.host == "127.0.0.2"
    assert daemon.port == 19871
    assert daemon.base_url == "http://127.0.0.2:19871"

def test_daemon_info_from_config_allows_env_override(monkeypatch: pytest.MonkeyPatch):
    """环境变量优先覆盖配置中的 API 地址"""
    monkeypatch.setenv("SATRAP_API_HOST", "127.0.0.3")
    monkeypatch.setenv("SATRAP_API_PORT", "19872")

    daemon = DaemonInfo.from_config(BackendConfig(api_host="127.0.0.2", api_port=19871))

    assert daemon.host == "127.0.0.3"
    assert daemon.port == 19872

def test_load_run_config_allows_cli_api_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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

def test_backend_instance_lock_is_exclusive(tmp_path: Path):
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
