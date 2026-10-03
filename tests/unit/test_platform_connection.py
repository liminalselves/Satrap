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
