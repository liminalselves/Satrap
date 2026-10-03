from copy import deepcopy
from types import SimpleNamespace
from typing import Any, NamedTuple, cast
from unittest.mock import AsyncMock
import asyncio
import json
from pathlib import Path
from time import time

import pytest

from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.framework.providers import BindingState, BindingStatus, SessionProviderRegistry
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.config.loader import ConfigLoader
from satrap.core.platform import EventDispatcher, PlatformAdapter, PlatformAdapterManager, PlatformConfig
from satrap.core.platform.event import PlatformMetadata
from satrap.core.config.document import config_document_revision, load_config_document, validate_platforms
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore


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


def _scheduler_session_manager() -> AsyncMock:
    """
    构造管线测试用的会话管理器替身

    返回:
    - AsyncMock: 只记录 handle_call_async 调用, 绑定判定恒为可运行

    这些用例只关心配置热更新对事件与窗口的影响, 因此不引入真实注册表
    """
    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    manager.provider_registry = SimpleNamespace(
        binding_status=lambda *_args: BindingStatus(BindingState.RUNNABLE),
    )
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
async def test_input_budget_and_talk_value_are_hot_applied(tmp_path: Path):
    """B10: 输入预算与 talk_value 属逐事件配置, 热更新不重建实例且不改已冻结事件"""
    backend, adapter, path, platform = setup_runtime(tmp_path)

    async def feed(text: str, message_id: str) -> Any:
        await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30,
                                             "message_id": message_id, "message_type": "group",
                                             "message": [{"type": "text", "data": {"text": text}}]})
        return adapter._event_queue.get_nowait()

    frozen = await feed("保存前", "1")
    assert frozen.policy_settings.get("input_text_limit") is None
    platform["settings"].update({"input_text_limit": 512, "input_media_limit": 3,
                                 "wake_talk_value": 0, "wake_mode": "frequency"})
    path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
    result = (await backend.reload_platform_policies())[0]
    assert result["status"] == "applied" and result["saved_revision"] == result["active_revision"]
    # 不重建实例或连接, 只替换配置对象
    assert _require_manager(backend).get_adapter("bot") is adapter
    assert adapter.config.settings["input_text_limit"] == 512
    # 已冻结事件保留其原快照, 下一事件才用新预算与新频率
    assert frozen.policy_settings.get("input_text_limit") is None
    fresh = await feed("保存后", "2")
    assert fresh.policy_settings["input_text_limit"] == 512
    assert fresh.policy_settings["input_media_limit"] == 3
    assert fresh.policy_settings["wake_talk_value"] == 0
    # 新频率立即生效: talk_value=0 的自动参与不触发, 正文留在窗口等待显式唤醒
    manager = _scheduler_session_manager()
    scheduler = PipelineScheduler(manager)
    await scheduler.execute(fresh)
    manager.handle_call_async.assert_not_awaited()
    decision = fresh.get_extra("wake_decision")
    assert decision is not None and decision.triggered is False and "wake_talk_value=0" in decision.reason
    assert len(scheduler.wake_window.peek(fresh)) == 1
    await scheduler.wake_timers.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_type", ["onebot", "probe-runtime"])
async def test_archive_retention_applies_without_reconnect_or_late_policy_overwrite(tmp_path: Path, adapter_type: str) -> None:
    backend, adapter, path, platform = setup_runtime(tmp_path)
    if adapter_type != "onebot":
        platform["type"] = adapter_type
        adapter = cast(Any, _LifecycleAdapter(PlatformConfig(id="bot", type=adapter_type, settings=deepcopy(platform["settings"]))))
        _require_manager(backend)._adapters["bot"] = adapter
        backend._platform_active_configs["bot"] = deepcopy(platform)
    database = backend.storage_layout.platform_db("bot")
    scope = MessageScope("bot", "self", "group", "group")
    old_time = time() - 8 * 86400
    old_store = PlatformMessageStore(database, "bot", clock=lambda: old_time)
    old_store.record(scope, ArchiveMessage("old", "member", old_time, "原文"))
    adapter.message_archive = old_store
    platform["settings"]["message_archive_retention_days"] = 7
    path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
    result = (await backend.reload_platform_policies())[0]
    assert result["status"] == "applied" and _require_manager(backend).get_adapter("bot") is adapter
    assert adapter.message_archive is not old_store and adapter.message_archive is not None
    assert adapter.message_archive.retention_days == 7 and adapter.message_archive.saved_retention_days() == 7
    message = adapter.message_archive.get(scope, "old")
    assert message is not None and message["status"] == "expired"
    old_store.record(scope, ArchiveMessage("late", "member", time(), "延迟入档"))
    assert adapter.message_archive.saved_retention_days() == 7


@pytest.mark.asyncio
async def test_archive_retention_persistence_failure_keeps_applied_store(tmp_path: Path, monkeypatch) -> None:
    backend, adapter, path, platform = setup_runtime(tmp_path)
    old_store = PlatformMessageStore(backend.storage_layout.platform_db("bot"), "bot")
    scope = MessageScope("bot", "self", "group", "group")
    old_store.record(scope, ArchiveMessage("old", "member", time(), "保留原文"))
    adapter.message_archive = old_store
    persist = PlatformMessageStore.persist_retention_policy

    def fail_new_policy(store: PlatformMessageStore) -> None:
        if store.retention_days == 7:
            raise OSError("无法保存策略")
        persist(store)

    monkeypatch.setattr(PlatformMessageStore, "persist_retention_policy", fail_new_policy)
    platform["settings"]["message_archive_retention_days"] = 7
    path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")
    result = (await backend.reload_platform_policies())[0]
    assert result["status"] == "failed" and result["reason"] == "archive_policy_apply_failed"
    assert adapter.message_archive is old_store and old_store.saved_retention_days() == 30
    assert "message_archive_retention_days" not in adapter.config.settings


@pytest.mark.asyncio
async def test_archive_policy_failure_during_replacement_still_restarts_old_receiver(tmp_path: Path, monkeypatch, caplog) -> None:
    life = _lifecycle_backend(tmp_path, _bound_platform(), {"bot-session": True})
    old_store = PlatformMessageStore(life.backend.storage_layout.platform_db("bot"), "bot")
    scope = MessageScope("bot", "self", "group", "group")
    old_store.record(scope, ArchiveMessage("known", "member", time(), "原消息"))
    life.old.message_archive = old_store
    await life.old.start()

    def fail_policy(_store: PlatformMessageStore) -> None:
        raise OSError("档案存储临时故障")

    monkeypatch.setattr(PlatformMessageStore, "persist_retention_policy", fail_policy)
    life.platform["settings"]["event_queue_capacity"] = 17
    _write(life.path, life.platform)
    try:
        result = (await life.backend.reload_platform_policies())[0]
        assert result["status"] == "failed" and result["old_runtime_preserved"] is True
        assert life.manager.get_adapter("bot") is life.old and life.old.started
        assert life.old._run_task is not None and not life.old._run_task.done()
        assert "替换回滚时策略恢复失败" in caplog.text
    finally:
        await life.dispatcher.detach_adapter("bot")
        await life.old.terminate()


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
    manager = _scheduler_session_manager()
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
    registry = _BindingRegistry({"default": True}, {"session_class"})
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


class _BindingRegistry:
    """会话定义注册表替身: 按名字表答复三态, 并记录每次判定调用"""

    def __init__(self, definitions: dict[str, bool], providers: set[str]) -> None:
        self.definitions = definitions
        self.providers = providers
        self.calls: list[tuple[str, str | None]] = []

    def binding_status(self, definition_name: str, provider_name: str | None = None) -> BindingStatus:
        """
        记录判定调用并答复三态

        参数:
        - definition_name: 会话定义名称
        - provider_name: 会话 Provider 名称

        返回:
        - BindingStatus: 未登记的 Provider 与未登记的定义按失效处理, 定义存在但未启用按禁用处理

        与生产实现同构, 生产实现自身的三态语义由 test_session_providers.py 覆盖
        """
        self.calls.append((definition_name, provider_name))
        location = f"provider={provider_name or ''}, name={definition_name}"
        if provider_name is not None and provider_name not in self.providers:
            return BindingStatus(BindingState.INVALID, f"未知会话 Provider: {provider_name}")
        if definition_name not in self.definitions:
            return BindingStatus(BindingState.INVALID, f"会话定义不可用 {location}")
        if not self.definitions[definition_name]:
            return BindingStatus(BindingState.DISABLED, f"会话定义已禁用: {location}")
        return BindingStatus(BindingState.RUNNABLE)

    def resolve_definition(self, definition_name: str, provider_name: str | None = None) -> tuple[object, object] | None:
        """
        记录解析调用并答复定义替身

        参数:
        - definition_name: 会话定义名称
        - provider_name: 会话 Provider 名称

        返回:
        - tuple[object, object] | None: 名称未登记时返回 None

        未登记的 Provider 按注册表既有语义抛异常, 用于覆盖"Provider 不存在也属失效绑定"
        """
        self.calls.append((definition_name, provider_name))
        if provider_name is not None and provider_name not in self.providers:
            raise ValueError(f"未知会话 Provider: {provider_name}")
        if definition_name not in self.definitions:
            return None
        return (SimpleNamespace(), SimpleNamespace(enabled=self.definitions[definition_name]))


class _LifecycleAdapter(PlatformAdapter):
    """生命周期测试适配器: 运行循环保持到被取消, 元数据只回显自身 id"""

    async def run(self) -> None:
        """保持运行直到外部取消"""
        await asyncio.Future()

    def meta(self) -> PlatformMetadata:
        """
        返回适配器元数据

        返回:
        - PlatformMetadata: 名称与 id 均取平台配置 id
        """
        return PlatformMetadata(name=self.config.id, id=self.config.id)


class _RecordingScheduler:
    """记录窗口与定时器清理调用, 用于断言校验失败零副作用"""

    def __init__(self) -> None:
        self.cleared: list[str] = []
        self.runtime_updates = 0
        self.wake_window = SimpleNamespace(clear_adapter=self._record)
        self.wake_timers = SimpleNamespace(clear_adapter=self._record)

    def _record(self, adapter_id: str) -> None:
        self.cleared.append(adapter_id)

    async def clear_manual_wakes(self, adapter_id: str) -> None:
        """
        记录手动唤醒清理

        参数:
        - adapter_id: 平台实例 id
        """
        self.cleared.append(adapter_id)

    def set_platform_runtimes(self, runtimes: dict[str, object]) -> None:
        """
        记录平台运行时刷新

        参数:
        - runtimes: 平台运行时映射
        """
        self.runtime_updates += 1


class _Lifecycle(NamedTuple):
    """生命周期断言所需的装配件"""

    backend: BackendManager
    manager: PlatformAdapterManager
    dispatcher: EventDispatcher
    old: PlatformAdapter
    path: Path
    platform: dict[str, Any]
    registry: _BindingRegistry


def _lifecycle_backend(
    tmp_path: Path,
    platform: dict[str, Any],
    definitions: dict[str, bool],
    providers: set[str] | None = None,
) -> _Lifecycle:
    """
    装配运行中的后端: 管理器, 分发器与鸭子运行时齐备, 供定向替换断言使用

    参数:
    - tmp_path: 临时目录, 必须已存在
    - platform: 目标平台配置, 会被写入启动配置文件
    - definitions: 会话定义名到是否启用的映射, 未列出的定义不存在
    - providers: 已注册的 Provider 名称集合, 默认只含 session_class

    返回:
    - _Lifecycle: 装配件集合, registry 携带解析调用记录
    """
    backend, _, path, _ = setup_runtime(tmp_path)
    manager = _require_manager(backend)
    manager.registry.register("probe-runtime", _LifecycleAdapter)
    manager.registry.register("aiocqhttp", _LifecycleAdapter)
    # 登记 OneBot 别名以验证专有策略热更新, 档案保留期对全部适配器支持热更新
    registry = _BindingRegistry(definitions, {"session_class"} if providers is None else providers)
    backend._platform_runtimes["bot"] = cast(Any, (SimpleNamespace(provider_registry=registry, plugin_environment=None), None))
    backend._platform_active_configs = {"bot": deepcopy(platform)}
    backend._running = True
    dispatcher = EventDispatcher(manager, AsyncMock())
    backend._dispatcher = dispatcher
    old = _LifecycleAdapter(PlatformConfig(
        id="bot", type="probe-runtime", session_provider=str(platform.get("session_provider", "session_class")),
        session_type=str(platform.get("session_type", "")), enable=bool(platform.get("enable", True)),
        settings=deepcopy(platform.get("settings", {})),
    ))
    manager._adapters = {"bot": old}
    _write(path, platform)
    return _Lifecycle(backend, manager, dispatcher, old, path, platform, registry)


def _write(path: Path, platform: dict[str, Any]) -> None:
    """把单个平台配置写回启动文件"""
    path.write_text(json.dumps({"data_root": str(path.parent / "data"), "platforms": [platform]}), encoding="utf-8")


def _write_many(path: Path, platforms: list[dict[str, Any]]) -> None:
    """把多个平台配置写回启动文件"""
    path.write_text(json.dumps({"data_root": str(path.parent / "data"), "platforms": platforms}), encoding="utf-8")


def _dangling_platform(enabled: bool, session_type: str = "missing-session") -> dict[str, Any]:
    """
    构造绑定失效的平台配置

    参数:
    - enabled: 平台是否启用
    - session_type: 绑定的会话类配置名称

    返回:
    - dict[str, Any]: 平台配置
    """
    return {"id": "bot", "type": "probe-runtime", "enable": enabled, "session_provider": "session_class",
            "session_type": session_type, "settings": {}}


def _bound_platform(session_type: str = "bot-session", enabled: bool = True) -> dict[str, Any]:
    """
    构造绑定有效的平台配置

    参数:
    - session_type: 绑定的会话类配置名称
    - enabled: 平台是否启用

    返回:
    - dict[str, Any]: 平台配置
    """
    return {"id": "bot", "type": "probe-runtime", "enable": enabled, "session_provider": "session_class",
            "session_type": session_type, "settings": {}}


@pytest.mark.asyncio
async def test_running_enabled_platform_disabled_with_dangling_binding_in_one_update(tmp_path: Path):
    """运行中的启用平台在同一次更新中变为禁用且绑定失效: 替换为惰性实例且不解析绑定"""
    life = _lifecycle_backend(tmp_path, _dangling_platform(True), {})
    await life.old.start()
    try:
        life.platform["enable"] = False
        _write(life.path, life.platform)
        result = (await life.backend.reload_platform_policies())[0]
        current = life.manager.get_adapter("bot")
        assert result["status"] == "applied"
        assert current is not None and current is not life.old
        assert current.config.enable is False and current.started is False
        assert "bot" not in life.dispatcher._workers
        assert life.old.started is False
        assert life.registry.calls == []
    finally:
        await life.old.terminate()


@pytest.mark.asyncio
async def test_init_platform_disabled_with_dangling_binding_stays_inert(tmp_path: Path):
    """启动路径: 禁用平台绑定失效时不解析绑定, 仍完成装配且保持未启动"""
    backend, _, _, _ = setup_runtime(tmp_path)
    manager = _require_manager(backend)
    manager.registry.register("probe-runtime", _LifecycleAdapter)
    manager._adapters = {}
    registry = _BindingRegistry({}, {"session_class"})
    backend._platform_runtimes["bot"] = cast(Any, (SimpleNamespace(provider_registry=registry, plugin_environment=None), None))
    backend._init_platform("bot", "probe-runtime", _dangling_platform(False))
    adapter = _require_adapter(manager, "bot")
    assert adapter.config.enable is False and adapter.started is False
    assert backend._platform_active_configs["bot"]["enable"] is False
    assert registry.calls == []
    await manager.start_all()
    assert adapter.started is False


@pytest.mark.asyncio
async def test_init_platforms_disabled_dangling_platform_records_no_failure(tmp_path: Path):
    """启动装配: 禁用且绑定失效的平台不进入失败清单"""
    from satrap.core.platform import set_current_adapter_manager
    from satrap.core.platform.notices import set_current_hub

    backend, _, _, _ = setup_runtime(tmp_path)
    registry = _BindingRegistry({}, {"session_class"})
    backend._platform_runtimes["bot"] = cast(Any, (SimpleNamespace(provider_registry=registry, plugin_environment=None), None))
    backend.config.platforms = [dict(_dangling_platform(False), type="onebot")]
    try:
        await backend._init_platforms()
    finally:
        set_current_hub(None)
        set_current_adapter_manager(None)
    assert backend._platform_config_results == []
    assert registry.calls == []
    adapter = _require_adapter(_require_manager(backend), "bot")
    assert adapter.config.enable is False and adapter.started is False


@pytest.mark.asyncio
async def test_ghost_disabled_dangling_platform_applies_and_stays_applied(tmp_path: Path):
    """从未装配成功的禁用平台连续两次重载都应应用成功, 且不牵动其他平台"""
    platform = _bound_platform()
    life = _lifecycle_backend(tmp_path, platform, {"bot-session": True})
    await life.old.start()
    run_task = life.old._run_task
    try:
        ghost = dict(_dangling_platform(False), id="ghost")
        _write_many(life.path, [platform, ghost])
        for _ in range(2):
            entry = next(item for item in await life.backend.reload_platform_policies() if item["id"] == "ghost")
            assert entry["status"] == "applied" and entry["active_revision"] is not None
        ghost_adapter = _require_adapter(life.manager, "ghost")
        assert ghost_adapter.config.enable is False and ghost_adapter.started is False
        assert life.manager.get_adapter("bot") is life.old
        assert life.old._run_task is run_task
        assert life.registry.calls == []
    finally:
        await life.old.terminate()


@pytest.mark.asyncio
async def test_disabled_platform_rebinding_to_missing_definition_applies_without_fallback(tmp_path: Path):
    """禁用平台改绑到不存在的定义: 应用成功, 绑定按配置值保留, 不回退到默认会话类"""
    life = _lifecycle_backend(tmp_path, _dangling_platform(False), {})
    life.platform["session_type"] = "another-missing"
    _write(life.path, life.platform)
    result = (await life.backend.reload_platform_policies())[0]
    current = life.manager.get_adapter("bot")
    assert result["status"] == "applied"
    assert current is not None and current.config.session_type == "another-missing"
    assert current.config.enable is False and current.started is False
    assert life.registry.calls == []


@pytest.mark.asyncio
async def test_disabled_platform_hot_setting_change_keeps_instance_inert(tmp_path: Path):
    """禁用平台的逐事件设置变更走就地在位替换, 不重建实例也不解析绑定"""
    platform = dict(_dangling_platform(False), type="aiocqhttp")
    life = _lifecycle_backend(tmp_path, platform, {})
    life.platform["settings"]["wake_words"] = ["new"]
    _write(life.path, life.platform)
    result = (await life.backend.reload_platform_policies())[0]
    assert result["status"] == "applied"
    assert life.manager.get_adapter("bot") is life.old
    assert life.old.config.settings["wake_words"] == ["new"]
    assert life.old.started is False
    assert life.registry.calls == []


@pytest.mark.asyncio
async def test_enable_dangling_platform_is_rejected_and_keeps_old_instance(tmp_path: Path):
    """从禁用切回启用且绑定仍失效: 拒绝启用, 保留旧实例与原绑定"""
    life = _lifecycle_backend(tmp_path, _dangling_platform(False), {})
    life.platform["enable"] = True
    _write(life.path, life.platform)
    result = (await life.backend.reload_platform_policies())[0]
    assert result["status"] == "failed"
    assert result["old_runtime_preserved"] is True
    assert result["active_revision"] is not None and result["active_revision"] != result["saved_revision"]
    assert life.manager.get_adapter("bot") is life.old
    assert life.old.config.enable is False and life.old.config.session_type == "missing-session"
    assert life.old.started is False
    assert life.registry.calls == [("missing-session", "session_class")]


@pytest.mark.asyncio
async def test_enabled_platform_with_missing_definition_is_rejected(tmp_path: Path):
    """启用平台绑定失效: 定向替换被拒绝, 原实例保持运行"""
    life = _lifecycle_backend(tmp_path, _bound_platform(), {"bot-session": True})
    await life.old.start()
    try:
        life.platform["session_type"] = "missing-session"
        _write(life.path, life.platform)
        result = (await life.backend.reload_platform_policies())[0]
        assert result["status"] == "failed"
        assert life.manager.get_adapter("bot") is life.old and life.old.started
        assert life.registry.calls == [("missing-session", "session_class")]
    finally:
        await life.old.terminate()


@pytest.mark.asyncio
async def test_disabled_definition_keeps_platform_running(tmp_path: Path):
    """绑定到已被禁用的会话定义: 平台仍属可应用状态, 启用时照常建实例并启动, 绑定按配置值保留"""
    life = _lifecycle_backend(tmp_path, _bound_platform(session_type="bot-session"), {"bot-session": False})
    life.platform["enable"] = True
    _write(life.path, life.platform)
    result = (await life.backend.reload_platform_policies())[0]
    assert result["status"] == "applied"
    current = _require_adapter(life.manager, "bot")
    assert current is not life.old and current.config.enable is True and current.started is True
    assert current.config.session_type == "bot-session"
    assert life.registry.calls == [("bot-session", "session_class")]
    life.platform["enable"] = False
    _write(life.path, life.platform)
    assert (await life.backend.reload_platform_policies())[0]["status"] == "applied"
    assert life.registry.calls == [("bot-session", "session_class")]


@pytest.mark.asyncio
async def test_delete_disabled_dangling_platform_applies(tmp_path: Path):
    """删除禁用且绑定失效的平台不经过任何绑定校验"""
    life = _lifecycle_backend(tmp_path, _dangling_platform(False), {})
    _write_many(life.path, [])
    result = (await life.backend.reload_platform_policies())[0]
    assert result["status"] == "applied" and result["active_revision"] is None
    assert life.manager.get_adapter("bot") is None
    assert life.registry.calls == []


@pytest.mark.asyncio
async def test_init_platform_with_disabled_definition_starts(tmp_path: Path):
    """启动路径: 定义存在但被禁用属可应用状态, 平台照常装配并启动, 绑定不被改写"""
    backend, _, _, _ = setup_runtime(tmp_path)
    manager = _require_manager(backend)
    manager.registry.register("probe-runtime", _LifecycleAdapter)
    manager._adapters = {}
    registry = _BindingRegistry({"bot-session": False}, {"session_class"})
    backend._platform_runtimes["bot"] = cast(Any, (SimpleNamespace(provider_registry=registry, plugin_environment=None), None))
    backend._init_platform("bot", "probe-runtime", _bound_platform())
    adapter = _require_adapter(manager, "bot")
    assert adapter.config.enable is True and adapter.config.session_type == "bot-session"
    assert registry.calls == [("bot-session", "session_class")]
    try:
        await manager.start_all()
        assert adapter.started is True
    finally:
        await adapter.terminate()


@pytest.mark.asyncio
async def test_init_platform_enabled_with_missing_definition_is_rejected(tmp_path: Path):
    """启动路径: 启用平台绑定失效仍 fail-closed 并记入初始化失败清单"""
    from satrap.core.platform import set_current_adapter_manager
    from satrap.core.platform.notices import set_current_hub

    backend, _, _, _ = setup_runtime(tmp_path)
    registry = _BindingRegistry({}, {"session_class"})
    backend._platform_runtimes["bot"] = cast(Any, (SimpleNamespace(provider_registry=registry, plugin_environment=None), None))
    _require_manager(backend)._adapters = {}
    pcfg = dict(_dangling_platform(True), type="onebot")
    with pytest.raises(ValueError, match="会话定义不可用"):
        backend._init_platform("bot", "onebot", pcfg)
    assert _require_manager(backend).get_adapter("bot") is None
    backend.config.platforms = [pcfg]
    try:
        await backend._init_platforms()
    finally:
        set_current_hub(None)
        set_current_adapter_manager(None)
    failures = backend._platform_config_results
    assert len(failures) == 1 and failures[0]["status"] == "failed"
    assert "平台初始化失败" in failures[0]["error"]
    assert registry.calls == [("missing-session", "session_class")] * 2


def _spy_binding_status(calls: list[tuple[str, str | None]], status: BindingStatus) -> Any:
    """
    构造记录调用的三态判定替身

    参数:
    - calls: 调用记录列表, 原地追加 (定义名, Provider 名)
    - status: 恒定答复的判定结果

    返回:
    - Callable[..., BindingStatus]: 供替换注册表方法的无绑定函数, 用 staticmethod 包裹避免注入 self
    """
    def spy(definition_name: str, provider_name: str | None = None) -> BindingStatus:
        calls.append((definition_name, provider_name))
        return status
    return spy


@pytest.mark.asyncio
async def test_shared_binding_judgment_gates_init_only_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """启动路径通过共享三态状态源判定: 失效时 fail-closed, 禁用平台完全不判定"""
    backend, _, _, _ = setup_runtime(tmp_path)
    manager = _require_manager(backend)
    manager.registry.register("probe-runtime", _LifecycleAdapter)
    manager._adapters = {}
    runtime = SimpleNamespace(provider_registry=SessionProviderRegistry(), plugin_environment=None)
    backend._platform_runtimes["bot"] = cast(Any, (runtime, None))
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        SessionProviderRegistry, "binding_status",
        staticmethod(_spy_binding_status(calls, BindingStatus(BindingState.INVALID, "绑定判定失效"))),
    )
    pcfg = _bound_platform()
    with pytest.raises(ValueError, match="绑定判定失效"):
        backend._init_platform("bot", "probe-runtime", pcfg)
    assert calls == [("bot-session", "session_class")]
    backend._init_platform("bot", "probe-runtime", dict(pcfg, enable=False))
    adapter = _require_adapter(manager, "bot")
    assert adapter.config.enable is False and adapter.started is False
    assert calls == [("bot-session", "session_class")]


@pytest.mark.asyncio
async def test_shared_binding_judgment_gates_reload_only_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """定向替换路径通过同一状态源判定: 失效时拒绝并保留旧实例, 禁用平台完全不判定"""
    life = _lifecycle_backend(tmp_path, _bound_platform(), {"bot-session": True, "bot-session-2": True})
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        SessionProviderRegistry, "binding_status",
        staticmethod(_spy_binding_status(calls, BindingStatus(BindingState.INVALID, "绑定判定失效"))),
    )
    life.backend._platform_runtimes["bot"][0].provider_registry = SessionProviderRegistry()
    life.platform["session_type"] = "bot-session-2"
    _write(life.path, life.platform)
    with pytest.raises(ValueError, match="绑定判定失效"):
        await life.backend._replace_platform_instance("bot", life.platform)
    assert calls == [("bot-session-2", "session_class")]
    assert (await life.backend.reload_platform_policies())[0]["status"] == "failed"
    assert life.manager.get_adapter("bot") is life.old
    assert calls == [("bot-session-2", "session_class")] * 2
    life.platform["enable"] = False
    _write(life.path, life.platform)
    result = (await life.backend.reload_platform_policies())[0]
    assert result["status"] == "applied"
    current = _require_adapter(life.manager, "bot")
    assert current is not life.old and current.config.enable is False and current.started is False
    assert calls == [("bot-session-2", "session_class")] * 2


@pytest.mark.asyncio
async def test_failed_enable_keeps_old_instance_and_scheduler_untouched(tmp_path: Path):
    """绑定校验失败发生在事务之前: 旧实例, 旧配置与调度器状态都不被触碰"""
    life = _lifecycle_backend(tmp_path, _dangling_platform(False), {})
    scheduler = _RecordingScheduler()
    life.backend._scheduler = cast(Any, scheduler)
    life.platform["enable"] = True
    _write(life.path, life.platform)
    result = (await life.backend.reload_platform_policies())[0]
    assert result["status"] == "failed"
    assert life.manager.get_adapter("bot") is life.old
    assert life.old.config.enable is False and life.old.config.session_type == "missing-session"
    assert life.backend._platform_active_configs["bot"]["enable"] is False
    assert scheduler.cleared == [] and scheduler.runtime_updates == 0


@pytest.mark.asyncio
async def test_disabled_platform_with_unknown_provider_applies_without_resolving(tmp_path: Path):
    """Provider 本身不存在也属失效绑定: 禁用时保存与应用都成功且不发生解析"""
    platform: dict[str, Any] = {"id": "bot", "type": "probe-runtime", "enable": False, "session_provider": "no-such-provider",
                                "session_type": "whatever", "settings": {}}
    saved = validate_platforms([deepcopy(platform)])
    assert saved[0]["session_provider"] == "no-such-provider" and saved[0]["enable"] is False
    life = _lifecycle_backend(tmp_path, platform, {}, {"session_class"})
    life.platform["session_type"] = "whatever-2"
    _write(life.path, life.platform)
    result = (await life.backend.reload_platform_policies())[0]
    current = life.manager.get_adapter("bot")
    assert result["status"] == "applied"
    assert current is not None and current.config.session_type == "whatever-2"
    assert current.config.enable is False and current.started is False
    assert life.registry.calls == []
    init_dir = tmp_path / "init"
    init_dir.mkdir()
    backend2, _, _, _ = setup_runtime(init_dir)
    init_manager = _require_manager(backend2)
    init_manager.registry.register("probe-runtime", _LifecycleAdapter)
    init_manager._adapters = {}
    init_registry = _BindingRegistry({}, {"session_class"})
    backend2._platform_runtimes["bot"] = cast(Any, (
        SimpleNamespace(provider_registry=init_registry, plugin_environment=None), None,
    ))
    backend2._init_platform("bot", "probe-runtime", platform)
    assert init_registry.calls == []
    init_adapter = _require_adapter(init_manager, "bot")
    assert init_adapter.config.enable is False and init_adapter.started is False


@pytest.mark.asyncio
async def test_enabled_platform_with_unknown_provider_keeps_unknown_provider_semantics(tmp_path: Path):
    """Provider 不存在时启用平台仍 fail-closed, 且保留既有的未知 Provider 语义"""
    platform: dict[str, Any] = {"id": "bot", "type": "probe-runtime", "enable": True, "session_provider": "no-such-provider",
                                "session_type": "whatever", "settings": {}}
    life = _lifecycle_backend(tmp_path, platform, {}, {"session_class"})
    life.platform["session_type"] = "whatever-2"
    _write(life.path, life.platform)
    assert (await life.backend.reload_platform_policies())[0]["status"] == "failed"
    assert life.manager.get_adapter("bot") is life.old
    assert life.registry.calls == [("whatever-2", "no-such-provider")]
    init_dir = tmp_path / "init"
    init_dir.mkdir()
    backend2, _, _, _ = setup_runtime(init_dir)
    backend2._platform_runtimes["bot"] = cast(Any, (
        SimpleNamespace(provider_registry=_BindingRegistry({}, {"session_class"}), plugin_environment=None), None,
    ))
    with pytest.raises(ValueError, match="未知会话 Provider: no-such-provider"):
        backend2._init_platform("bot", "probe-runtime", platform)


@pytest.mark.asyncio
async def test_unknown_provider_and_missing_definition_conclude_identically(tmp_path: Path):
    """两种失效绑定形态在同一 enable 取值下结论一致"""
    shapes: list[dict[str, str]] = [{"session_provider": "session_class"}, {"session_provider": "no-such-provider"}]
    cases = [(enabled, index, shape) for enabled in (False, True) for index, shape in enumerate(shapes)]
    for enabled, index, shape in cases:
        case_dir = tmp_path / f"{int(enabled)}-{index}"
        case_dir.mkdir()
        active: dict[str, Any] = {"id": "bot", "type": "probe-runtime", "enable": enabled,
                                  "session_type": "missing-session", "settings": {}}
        active.update(shape)
        life = _lifecycle_backend(case_dir, active, {"session_class": True})
        # 改绑到另一个名字以强制走重建路径, 否则沿用就地在位分支而不校验绑定
        life.platform["session_type"] = "rebind-1"
        _write(life.path, life.platform)
        result = (await life.backend.reload_platform_policies())[0]
        assert result["status"] == ("applied" if not enabled else "failed"), (enabled, shape)
        assert life.manager.get_adapter("bot") is not None


@pytest.mark.asyncio
async def test_route_store_failure_preserves_hot_platform_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, adapter, path, platform = setup_runtime(tmp_path)
    original = deepcopy(adapter.config.settings)
    platform["settings"]["wake_words"] = ["new"]
    path.write_text(json.dumps({"platforms": [platform]}), encoding="utf-8")

    def fail_route_store() -> int:
        raise OSError("路由存储不可用")

    monkeypatch.setattr(adapter, "apply_agent_routes", fail_route_store)
    result = (await backend.reload_platform_policies())[0]
    assert result["status"] == "failed" and result["reason"] == "agent_route_apply_failed"
    assert result["old_runtime_preserved"] is True
    assert adapter.config.settings == original
