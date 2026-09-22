from copy import deepcopy
from typing import Any, cast
from unittest.mock import AsyncMock
import json
from pathlib import Path

import pytest

from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.config.loader import ConfigLoader
from satrap.core.platform import PlatformAdapter, PlatformAdapterManager, PlatformConfig
from satrap.core.config.document import config_document_revision, load_config_document


def _require_adapter(manager: PlatformAdapterManager, adapter_id: str) -> PlatformAdapter:
    """取管理器中的适配器, 缺失视为装配错误"""
    adapter = manager.get_adapter(adapter_id)
    if adapter is None:
        raise AssertionError(f"适配器缺失: {adapter_id}")
    return adapter


def _require_manager(backend: BackendManager) -> PlatformAdapterManager:
    """取后端装配的适配器管理器, 缺失视为装配错误"""
    manager = backend._adapter_mgr
    if manager is None:
        raise AssertionError("运行时装配缺失适配器管理器")
    return manager


def setup_runtime(tmp_path: Path) -> tuple[BackendManager, OneBotAdapter, Path, dict[str, Any]]:
    config_path = tmp_path / "custom.json"
    platform: dict[str, Any] = {"id": "bot", "type": "onebot", "settings": {"wake_words": ["old"], "port": 6789}}
    config_path.write_text(json.dumps({"data_root": str(tmp_path / "data"), "platforms": [platform]}), encoding="utf-8")
    backend = BackendManager(ConfigLoader.from_json(config_path))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=deepcopy(platform["settings"])))
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    backend._platform_active_configs["bot"] = deepcopy(platform)
    return backend, adapter, config_path, platform


@pytest.mark.asyncio
async def test_saved_revision_must_match_actual_backend_source(tmp_path):
    backend, adapter, path, platform = setup_runtime(tmp_path)
    stale_revision = config_document_revision(load_config_document(path))
    platform["settings"]["wake_words"] = ["new"]
    path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
    result = (await backend.reload_platform_policies(stale_revision))[0]
    assert result["status"] == "failed"
    assert result["reason"] == "source_revision_mismatch"
    assert adapter.config.settings["wake_words"] == ["old"]
    assert backend.config.platforms[0]["settings"]["wake_words"] == ["old"]
    current_revision = config_document_revision(load_config_document(path))
    assert (await backend.reload_platform_policies(current_revision))[0]["status"] == "applied"
    assert adapter.config.settings["wake_words"] == ["new"]
    backend.config.source_path = None
    assert (await backend.reload_platform_policies(current_revision))[0]["reason"] == "source_revision_mismatch"


@pytest.mark.asyncio
async def test_reload_tracks_actual_file_and_updates_new_events_only(tmp_path):
    backend, adapter, path, platform = setup_runtime(tmp_path)
    assert backend.config.source_path == str(path.resolve())
    await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_type": "group",
                                         "message": [{"type": "text", "data": {"text": "old"}}]})
    old_event = adapter._event_queue.get_nowait()
    platform["settings"]["wake_words"] = ["new"]
    path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
    result = (await backend.reload_platform_policies())[0]
    assert result["status"] == "applied"
    assert result["saved_revision"] == result["active_revision"]
    assert adapter.config.settings["wake_words"] == ["new"]
    assert old_event.policy_settings["wake_words"] == ["old"]
    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    scheduler = PipelineScheduler(manager)
    await scheduler.execute(old_event)
    for text in ["old", "new"]:
        await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_type": "group",
                                             "message": [{"type": "text", "data": {"text": text}}]})
        await scheduler.execute(adapter._event_queue.get_nowait())
    assert manager.handle_call_async.await_count == 2


@pytest.mark.asyncio
async def test_connection_change_remains_pending_without_partial_policy_application(tmp_path):
    backend, adapter, path, platform = setup_runtime(tmp_path)
    original = (await backend.reload_platform_policies())[0]["active_revision"]
    platform["settings"].update(port=6790, wake_words=["new"])
    path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
    result = (await backend.reload_platform_policies())[0]
    assert result["status"] == "pending_restart"
    assert result["active_revision"] == original != result["saved_revision"]
    assert adapter.port == 6789 and adapter.config.settings["wake_words"] == ["old"]


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["missing", "invalid", "invalid_policy"])
async def test_invalid_source_preserves_active_config(tmp_path, broken):
    backend, adapter, path, platform = setup_runtime(tmp_path)
    original = (await backend.reload_platform_policies())[0]["active_revision"]
    if broken == "missing":
        path.unlink()
    elif broken == "invalid":
        path.write_text("[", encoding="utf-8")
    else:
        platform["settings"]["group_whitelist"] = "secret-value"
        path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
    result = (await backend.reload_platform_policies())[0]
    assert result["status"] == "failed"
    assert result["active_revision"] == original
    assert adapter.config.settings["wake_words"] == ["old"]
    assert "secret-value" not in json.dumps(result)


def test_yaml_loader_records_source_without_persisting_internal_path(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text('platforms: []\nsource_path: "wrong-file"\n', encoding="utf-8")
    config = ConfigLoader.from_yaml(path)
    assert config.source_path == str(path.resolve())
    assert "source_path" not in ConfigLoader.default_config_document(config)


@pytest.mark.asyncio
async def test_form_default_connection_values_do_not_force_restart(tmp_path):
    backend, adapter, path, platform = setup_runtime(tmp_path)
    platform["settings"].update(host="127.0.0.1", access_token="", secret="", self_id="", wake_words=["new"])
    path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
    assert (await backend.reload_platform_policies())[0]["status"] == "applied"
    assert adapter.config.settings["wake_words"] == ["new"]


@pytest.mark.asyncio
async def test_invalid_connection_retains_runtime(tmp_path):
    backend, adapter, path, platform = setup_runtime(tmp_path)
    platform["settings"]["port"] = "invalid"
    path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
    assert (await backend.reload_platform_policies())[0]["status"] == "failed"
    assert adapter.port == 6789


@pytest.mark.asyncio
async def test_reload_survives_unvalidated_active_config(tmp_path):
    backend, adapter, path, platform = setup_runtime(tmp_path)
    backend._platform_active_configs["bot"] = {"id": "bot", "type": "onebot", "enable": "yes", "settings": {"group_whitelist": "oops"}}
    platform["settings"]["port"] = "invalid"
    path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
    result = (await backend.reload_platform_policies())[0]
    assert result["status"] == "failed" and result["active_revision"]
    platform["settings"]["port"] = 6789
    path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
    assert (await backend.reload_platform_policies())[0]["status"] in {"applied", "pending_restart"}


@pytest.mark.asyncio
async def test_targeted_replace_rollback_disable_and_delete(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from satrap.core.platform import PlatformAdapter, EventDispatcher
    from satrap.core.platform.event import PlatformMetadata

    class Adapter(PlatformAdapter):
        async def run(self):
            if self.config.settings.get("fail"):
                raise RuntimeError("simulated startup failure")
            await asyncio.Future()

        def meta(self):
            return PlatformMetadata(name=self.config.id, id=self.config.id)

    backend, _, path, platform = setup_runtime(tmp_path)
    platform["type"] = "test-runtime"
    mgr = _require_manager(backend)
    mgr.registry.register("test-runtime", Adapter)
    old = Adapter(PlatformConfig(id="bot", type="test-runtime", settings=deepcopy(platform["settings"])))
    other = Adapter(PlatformConfig(id="other", type="test-runtime"))
    adapters: dict[str, PlatformAdapter] = {"bot": old, "other": other}
    mgr._adapters = adapters
    backend._platform_active_configs = {"bot": deepcopy(platform)}
    registry = SimpleNamespace(resolve_definition=lambda *args: (None, SimpleNamespace(enabled=True)))
    runtime = SimpleNamespace(provider_registry=registry, plugin_environment=None)
    # 鸭子类型替身: 运行时只读 provider_registry/plugin_environment
    backend._platform_runtimes["bot"] = cast(Any, (runtime, None))
    backend._running = True
    backend._dispatcher = EventDispatcher(mgr, AsyncMock())
    await old.start()
    await other.start()
    await old.wait_ready()
    dispatch = asyncio.create_task(backend._dispatcher.dispatch_loop())
    await asyncio.sleep(0)
    unrelated = other._run_task
    if unrelated is None:
        raise AssertionError("适配器主循环任务缺失")
    try:
        platform["settings"]["port"] = 6790
        path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
        first = (await backend.reload_platform_policies())[0]
        assert first["status"] == "applied"
        current = _require_adapter(mgr, "bot")
        assert current is not old and current.started and not old.started
        assert other._run_task is unrelated and not unrelated.done()
        platform["settings"]["fail"] = True
        path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
        failed = (await backend.reload_platform_policies())[0]
        assert failed["status"] == "failed"
        assert failed["old_runtime_preserved"] is True
        assert failed["active_revision"] == first["active_revision"]
        assert mgr.get_adapter("bot") is current and current.started
        assert not current.config.settings.get("fail")
        platform["settings"].pop("fail")
        platform["enable"] = False
        path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
        assert (await backend.reload_platform_policies())[0]["status"] == "applied"
        assert not _require_adapter(mgr, "bot").started
        assert "bot" not in backend._dispatcher._workers
        platform["enable"] = True
        path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
        assert (await backend.reload_platform_policies())[0]["status"] == "applied"
        assert _require_adapter(mgr, "bot").started
        path.write_text('{"platforms": []}', encoding="utf-8")
        deleted = (await backend.reload_platform_policies())[0]
        assert deleted["status"] == "applied" and deleted["active_revision"] is None
        assert mgr.get_adapter("bot") is None
        path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
        assert (await backend.reload_platform_policies())[0]["status"] == "applied"
        assert _require_adapter(mgr, "bot").started
        assert other._run_task is unrelated and not unrelated.done()
    finally:
        dispatch.cancel()
        await asyncio.gather(dispatch, return_exceptions=True)
        for adapter in mgr._adapters.values():
            await adapter.terminate()


@pytest.mark.asyncio
async def test_onebot_readiness_checks_own_listener(unused_tcp_port):
    adapter = OneBotAdapter(PlatformConfig(id="ready", type="onebot", settings={"port": unused_tcp_port}))
    try:
        await adapter.start()
        await adapter.wait_ready(timeout=3)
        assert adapter.started
    finally:
        await adapter.terminate()
    run_task = adapter._run_task
    if run_task is None:
        raise AssertionError("适配器主循环任务缺失")
    assert run_task.done()


@pytest.mark.asyncio
async def test_onebot_occupied_port_is_not_ready(unused_tcp_port):
    import asyncio

    async def unrelated(reader, writer):
        try:
            await reader.read(4096)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nother")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(unrelated, "127.0.0.1", unused_tcp_port)
    adapter = OneBotAdapter(PlatformConfig(id="collision", type="onebot", settings={"port": unused_tcp_port}))
    try:
        await adapter.start()
        with pytest.raises((RuntimeError, TimeoutError)):
            await adapter.wait_ready(timeout=1)
    finally:
        await adapter.terminate()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_reload_failures_are_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    import logging
    backend, adapter, path, platform = setup_runtime(tmp_path)
    path.write_text("[", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert (await backend.reload_platform_policies())[0]["status"] == "failed"
    assert any("平台配置读取或校验失败" in r.getMessage() for r in caplog.records)
    caplog.clear()
    path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        await backend.reload_platform_policies(expected_config_revision="deadbeef")
    assert any("修订与本次保存不一致" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_media_plaintext_http_toggle_is_hot_applied(tmp_path: Path):
    """media_plaintext_http 逐事件读取, 开关切换热生效不触发实例重建"""
    backend, adapter, path, platform = setup_runtime(tmp_path)
    platform["settings"]["media_plaintext_http"] = True
    path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
    result = (await backend.reload_platform_policies())[0]
    assert result["status"] == "applied"
    assert adapter.config.settings["media_plaintext_http"] is True
