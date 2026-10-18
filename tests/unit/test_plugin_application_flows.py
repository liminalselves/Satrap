"""业务链路场景与集中边界契约"""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any, cast
import pytest
from satrap.core.backend import control_server
from satrap.core.framework.providers.edictum import EdictumProvider
from satrap.core.framework.SessionManager import SessionManager
from satrap.display.service import ChatService
from satrap.display import service as service_module
from satrap.edictum.plugin_spec import PluginSpec
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.config.model_snapshot import ModelConfigSnapshot
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.edictum.plugin_apply import PluginApplySnapshot, logger as _plugin_apply_snapshot_logger
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.edictum.plugin_settings import resolve_runtime_specs
from pathlib import Path
from satrap.core.backend.BackendManager import BackendManager, logger as _scoped_plugin_apply_logger
from satrap.edictum.plugin_apply import EdictumPluginApplySnapshot
from contextvars import copy_context
from contextlib import nullcontext
import urllib.error
import urllib.request
from satrap.core.backend import plugin_application as relay_module
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.config.plugin_service import PluginManagementService
from satrap.core.server_auth import ServerAuth
from satrap.core.utils.application_progress import ApplicationOperations, progress_external, progress_instance
from satrap.core.utils.application_progress import progress_phase
from satrap.core.utils.async_worker import BoundedAsyncWorker
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.server import ChatHTTPServer
from support.http import _request, _json_body
from support.progress import _finished

def test_scoped_internal_request_serializes_utf8(monkeypatch):
    """内部范围通过 JSON 请求体传递且保留认证"""
    def request(req, **kwargs):
        """核验内部请求并模拟离线"""
        assert json.loads(req.data.decode("utf-8")) == {"plugin_name": "插件", "config_name": "助手"}
        assert req.get_header("Authorization").startswith("Bearer ")
        raise control_server.urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(control_server.urllib.request, "urlopen", request)
    result = control_server._plugin_runtime_request("Edictum", "http://127.0.0.1:1", {"plugin_name": "插件", "config_name": "助手"})
    assert result["status"] == "next_activation"

def test_provider_reference_filter_keeps_removed_runtime_and_new_reference():
    """旧运行时引用移除后仍需卸载, 新引用及无关配置区分"""
    provider = cast(Any, EdictumProvider.__new__(EdictumProvider))
    state = SimpleNamespace(config_name="agent", plugins=[SimpleNamespace(name="removed")])
    provider._get_runtime_state = lambda session: state
    provider.config_manager = SimpleNamespace(get_config=lambda name: {"plugins": ["new", {"name": "disabled", "enabled": False}]})
    assert provider.references_plugin(object(), "removed")
    assert provider.references_plugin(object(), "new")
    assert provider.references_plugin(object(), "disabled")
    assert not provider.references_plugin(object(), "unrelated")
    assert not provider.references_plugin(object(), "new", desired_plugins=[])

@pytest.mark.asyncio
async def test_session_manager_intersects_plugin_and_named_config_scope():
    """只协调同时满足插件引用与命名配置范围的实例"""
    manager = cast(Any, SessionManager.__new__(SessionManager))
    applied = []

    async def reconcile(session, *, desired_plugins):
        """记录被协调的实例"""
        applied.append(session.name)
        return {"ok": True}

    provider = SimpleNamespace(reconcile_session_plugins_async=reconcile, references_plugin=lambda session, name, **kwargs: session.name != "unrelated")
    manager.provider_registry = SimpleNamespace(get=lambda name: provider)
    entries = {name: SimpleNamespace(session=SimpleNamespace(name=name, _satrap_provider_name="edictum"), async_operation_lock=asyncio.Lock(),
                                    sync_operation_lock=threading.RLock(), instance_generation=name) for name in ("matched", "unrelated", "other-config")}
    manager.pool = SimpleNamespace(list_entries=lambda: entries)
    manager.store = SimpleNamespace(get=lambda name: SimpleNamespace(session_type_name="other" if name == "other-config" else "agent"))
    manager.platform_id = "test"
    result = await manager.reconcile_edictum_plugins_async(config_name="agent", plugin_name="target")
    assert applied == ["matched"]
    assert [item["session_id"] for item in result] == ["matched"]

@pytest.mark.asyncio
async def test_chat_bound_concurrency_preserves_lock_and_isolates_failure(monkeypatch, tmp_path):
    """锁被占用的会话不阻塞其他会话, 并发有界且单项失败不取消同批"""
    service = cast(Any, ChatService.__new__(ChatService))
    spec = PluginSpec(name="target")
    service._platform_id = "chat"
    service._plugins = SimpleNamespace(resolve_specs=lambda manager, **kwargs: [spec], catalog=SimpleNamespace(scan=lambda: []), state_path=tmp_path / "chat.json", refresh=lambda **kwargs: None)
    monkeypatch.setattr(service_module.PluginApplySnapshot, "capture", lambda catalog, manager, **kwargs: SimpleNamespace(catalog=catalog, is_current=lambda: True, has_model_sources=False))
    sessions = []
    for index in range(5):
        sessions.append(SimpleNamespace(conversation_id=str(index), persisted=True, plugin_states=[], plugin_lock=asyncio.Lock(),
                                        session=SimpleNamespace(install_plugin=str(index), uninstall_plugin=object(), plugin_model_manager=None)))
    service._conversations = {conv.conversation_id: conv for conv in sessions}
    await sessions[0].plugin_lock.acquire()
    started = []
    running = 0
    peak = 0
    two_started = asyncio.Event()
    release = asyncio.Event()

    async def reconcile(states, specs, installer, uninstaller):
        """暂停生命周期操作以核验并发和锁"""
        nonlocal running, peak
        started.append(installer)
        running += 1
        peak = max(peak, running)
        if len(started) >= 2:
            two_started.set()
        try:
            await release.wait()
            if installer == "3":
                raise RuntimeError("单个会话安装失败")
            return {"ok": True, "plugins": []}
        finally:
            running -= 1

    monkeypatch.setattr(service_module, "resolve_runtime_specs", lambda session, specs, catalog, **kwargs: specs)
    monkeypatch.setattr(service_module, "reconcile_plugin_states_async", reconcile)
    task = asyncio.create_task(service._reconcile_chat_plugins(plugin_name="target", concurrency=3))
    try:
        await asyncio.wait_for(two_started.wait(), 2)
        assert "0" not in started
        assert peak == 2
        release.set()
        await asyncio.sleep(0)
    finally:
        release.set()
        sessions[0].plugin_lock.release()
    result = await asyncio.wait_for(task, 2)
    assert [item["conversation_id"] for item in result] == [str(index) for index in range(5)]
    assert [item["ok"] for item in result] == [True, True, True, False, True]
    assert peak <= 3
    assert all(not conv.plugin_lock.locked() for conv in sessions)

@pytest.mark.asyncio
async def test_chat_filter_skips_unrelated_and_keeps_old_enabled_reference(monkeypatch, tmp_path):
    """关闭或移除旧插件仍协调, 无关预加载实例不失效"""
    service = cast(Any, ChatService.__new__(ChatService))
    service._plugins = SimpleNamespace(resolve_specs=lambda manager, **kwargs: [PluginSpec(name="target", enabled=False)], catalog=SimpleNamespace(scan=lambda: []), state_path=tmp_path / "chat.json", refresh=lambda **kwargs: None)
    service._platform_id = "chat"
    monkeypatch.setattr(service_module.PluginApplySnapshot, "capture", lambda catalog, manager, **kwargs: SimpleNamespace(catalog=catalog, is_current=lambda: True, has_model_sources=False))
    old = SimpleNamespace(conversation_id="old", persisted=True, plugin_states=[SimpleNamespace(name="target", enabled=True, handle=object())],
                          plugin_lock=asyncio.Lock(), session=SimpleNamespace(install_plugin="old", uninstall_plugin=None, plugin_model_manager=None))
    unrelated = SimpleNamespace(conversation_id="preload", persisted=False, plugin_states=[], build_fingerprint="unchanged", session=object())
    service._conversations = {"old": old, "preload": unrelated}
    applied = []

    async def reconcile(states, specs, installer, uninstaller):
        """记录仍需卸载的旧实例"""
        applied.append(installer)
        return {"ok": True}

    monkeypatch.setattr(service_module, "resolve_runtime_specs", lambda session, specs, catalog, **kwargs: specs)
    monkeypatch.setattr(service_module, "reconcile_plugin_states_async", reconcile)
    result = await service._reconcile_chat_plugins(plugin_name="target")
    assert applied == ["old"]
    assert len(result) == 1
    assert unrelated.build_fingerprint == "unchanged"

def _sources(tmp_path):
    """创建完全隔离的插件及全局参数"""
    plugin = tmp_path / "plugins" / "probe"
    plugin.mkdir(parents=True)
    (plugin / "meta.yaml").write_text("name: probe\nconfig_schema:\n  note:\n    type: str\n    default: default", encoding="utf-8")
    manager = PluginConfigManager(tmp_path / "globals")
    manager._dir.mkdir()
    path = manager._dir / "probe.json"
    path.write_text('{"note":"before"}', encoding="utf-8")
    return PluginCatalog(tmp_path / "builtin", tmp_path / "plugins"), manager, path

@pytest.mark.parametrize("source_kind", ["model", "global"])
def test_capture_retries_when_source_changes_during_read(tmp_path, monkeypatch, source_kind):
    """模型文件跨实例只读一次, 任一来源捕获中变化整体重读且旧正文不沿用"""
    catalog, manager, path = _sources(tmp_path)
    if source_kind == "model":
        models = ModelConfigManager(tmp_path / "models.json")
        models.update_llm_config("resource", model="old", api_key="test")
        writer = ModelConfigManager(models.storage_path)
        reads = []
        read = ModelConfigSnapshot.read

        def changing_model(source):
            """首次读取结束后改写来源以验证整次重试"""
            result = read(source)
            reads.append(source.storage_path)
            if len(reads) == 1:
                writer.update_llm_config("resource", model="new")
            return result

        monkeypatch.setattr(ModelConfigSnapshot, "read", changing_model)
        snapshot = PluginApplySnapshot.capture(catalog, manager, model_managers=(models, writer, models))
        assert reads == [models.storage_path, models.storage_path]
        captured = snapshot.models_for(models)
        if captured is None:
            raise AssertionError("模型来源未捕获")
        assert captured is snapshot.models_for(writer)
        assert captured.get_llm_config("resource").model == "new"
        assert models.get_llm_config("resource").model == "new"
        assert snapshot.is_current()
        writer.update_llm_config("resource", model="latest")
        assert not snapshot.is_current()
        assert captured.get_llm_config("resource").model == "new"
        return
    read = manager.load_global_explicit
    calls = []

    def changing_global(name, schema):
        """在首次读取结束后改写来源"""
        calls.append(name)
        result = read(name, schema)
        if len(calls) == 1:
            path.write_text('{"note":"changed"}', encoding="utf-8")
        return result

    monkeypatch.setattr(manager, "load_global_explicit", changing_global)
    snapshot = PluginApplySnapshot.capture(catalog, manager)
    assert snapshot.global_config("probe") == {"note": "changed"}
    assert calls == ["probe", "probe"]
    assert snapshot.is_current()

def test_snapshot_capture_global_models_and_override_invariants(tmp_path, monkeypatch):
    """快照固定目录与全局字段, 捕获期间新增模型引用重读, 会话与命名覆盖按来源解析"""
    # 捕获期间元数据新增模型字段: 从固定目录重读并捕获来源
    catalog, manager, _path = _sources(tmp_path)
    models = ModelConfigManager(tmp_path / "models.json")
    models.update_llm_config("resource", model="expected", api_key="test")
    meta = tmp_path / "plugins" / "probe" / "meta.yaml"
    load = manager.load_global_explicit
    calls = []

    def changing(name, schema):
        """首次读取字段后新增模型引用, 触发完整来源重试"""
        result = load(name, schema)
        calls.append(name)
        if len(calls) == 1:
            meta.write_text(meta.read_text(encoding="utf-8") + "\n  model:\n    type: llm\n    default: resource\n", encoding="utf-8")
        return result

    monkeypatch.setattr(manager, "load_global_explicit", changing)
    snapshot = PluginApplySnapshot.capture(catalog, manager, model_managers=(models,), model_plugin_filter=lambda entry: True)
    captured = snapshot.models_for(models)
    if captured is None:
        raise AssertionError("新增模型引用未捕获来源")
    assert len(calls) == 2 and snapshot.has_model_sources
    assert snapshot.global_config("probe")["model"] == "resource"
    assert captured.get_llm_config("resource").model == "expected"

    # 同次解析不重读全局字段, 返回副本且固定快照不再构造实时来源
    catalog, manager, path = _sources(tmp_path / "second")
    read = manager.load_global_explicit
    counted = []

    def counted_global(name, schema):
        """记录全局正文解析"""
        counted.append(name)
        return read(name, schema)

    monkeypatch.setattr(manager, "load_global_explicit", counted_global)
    snapshot = PluginApplySnapshot.capture(catalog, manager)
    copied = snapshot.global_config("probe")
    copied["note"] = "outside"
    assert snapshot.global_config("probe") == {"note": "before"}

    def unexpected_manager():
        """固定快照解析不能再构造实时参数来源"""
        raise AssertionError("已有快照不应构造实时配置管理器")

    monkeypatch.setattr("satrap.edictum.plugin_settings.PluginConfigManager", unexpected_manager)
    for index in range(5):
        session: Any = SimpleNamespace(session_id=str(index))
        result = resolve_runtime_specs(session, [PluginSpec(name="probe")], snapshot.catalog, snapshot=snapshot)
        assert result[0].config == {"note": "before"}
    assert counted == ["probe"]
    path.write_text('{"note":"after"}', encoding="utf-8")
    assert not snapshot.is_current()
    assert snapshot.global_config("probe") == {"note": "before"}
    assert PluginApplySnapshot.capture(catalog, manager).global_config("probe") == {"note": "after"}
    assert counted == ["probe", "probe"]

    # 命名配置与实例覆盖仍即时读取且不跨实例传播
    catalog, manager, _path = _sources(tmp_path / "third")
    snapshot = PluginApplySnapshot.capture(catalog, manager)
    store = SessionOverrideStore(tmp_path / "instances.db")
    store.replace("one", "plugins.probe", {"note": "local-one"}, expected_revision=0)
    one: Any = SimpleNamespace(session_id="one", plugin_override_store=store)
    two: Any = SimpleNamespace(session_id="two", plugin_override_store=store)
    spec = PluginSpec(name="probe", config={"note": "named"})
    first = resolve_runtime_specs(one, [spec], snapshot.catalog, snapshot=snapshot)
    second = resolve_runtime_specs(two, [spec], snapshot.catalog, snapshot=snapshot)
    assert first[0].config["note"] == "local-one"
    assert second[0].config["note"] == "named"
    store.replace("one", "plugins.probe", {"note": "new-local"}, expected_revision=1)
    assert resolve_runtime_specs(one, [spec], snapshot.catalog, snapshot=snapshot)[0].config["note"] == "new-local"
    assert snapshot.global_config("probe")["note"] == "before"

def test_corrupt_source_is_logged_and_never_becomes_default_snapshot(tmp_path, monkeypatch):
    """损坏配置明确失败, 不安装默认参数来替代已保存配置"""
    catalog, manager, path = _sources(tmp_path)
    path.write_text("broken", encoding="utf-8")
    messages = []
    monkeypatch.setattr(_plugin_apply_snapshot_logger, "error", messages.append)
    with pytest.raises(ValueError):
        PluginApplySnapshot.capture(catalog, manager)
    assert len(messages) == 1 and "Traceback" in messages[0]

def test_missing_source_uses_defaults_and_later_file_or_meta_invalidates(tmp_path):
    """缺失文件按默认值, 新建来源和元数据编辑使排队快照失效"""
    catalog, manager, path = _sources(tmp_path)
    path.unlink()
    snapshot = PluginApplySnapshot.capture(catalog, manager)
    assert snapshot.global_config("probe") == {"note": "default"}
    path.write_text("{}", encoding="utf-8")
    assert not snapshot.is_current()
    snapshot = PluginApplySnapshot.capture(catalog, manager)
    meta = tmp_path / "plugins" / "probe" / "meta.yaml"
    meta.write_text(meta.read_text(encoding="utf-8") + "\nversion: '2'", encoding="utf-8")
    assert not snapshot.is_current()
    old_entry = snapshot.catalog.get("probe")
    new_entry = PluginApplySnapshot.capture(catalog, manager).catalog.get("probe")
    if old_entry is None or new_entry is None:
        raise AssertionError("测试插件未加载")
    assert old_entry.version != "2"
    assert new_entry.version == "2"

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plugins", "runtime"])
async def test_scoped_plugin_read_does_not_block_loop_or_reload_unrelated_models(kind: str, tmp_path: Path):
    """慢冷配置读取允许其它协程运行, 后续协调保留指定会话范围"""
    entered = threading.Event()
    release = threading.Event()
    read_threads: list[int] = []
    applied: list[dict[str, object]] = []

    def reload() -> None:
        """暂停读取以核验事件循环可用性"""
        read_threads.append(threading.get_ident())
        entered.set()
        if not release.wait(3):
            raise TimeoutError("测试未释放读取")

    async def reconcile(**scope: object) -> list[dict[str, object]]:
        """保存后端传入的定向协调范围"""
        applied.append(scope)
        return [{"ok": True, "session_id": "one"}]

    def unrelated_reload() -> None:
        """禁止插件协调调用无关模型重载"""
        raise AssertionError("不应重载模型")

    backend = cast(Any, BackendManager.__new__(BackendManager))
    backend._edictum_cfg = SimpleNamespace(reload=reload, storage_path=tmp_path / "named.json", list_configs=lambda: {},
                                         plugin_catalog=PluginCatalog(tmp_path / "builtin", tmp_path / "user"))
    backend._model_cfg = SimpleNamespace(reload=unrelated_reload)
    runtime = SimpleNamespace(reconcile_edictum_plugins_async=reconcile, reconcile_edictum_runtime_async=reconcile,
                              model_config_manager=None,
                              edictum_plugin_apply_scope=lambda **kwargs: {"assistant": False},
                              edictum_runtime_apply_targets=lambda **kwargs: [("one", None, "assistant", "generation")])
    backend._platform_runtimes = {"ob": (runtime, None), "other": (runtime, None)}
    apply = getattr(backend, f"reconcile_edictum_{kind}_async")
    task = asyncio.create_task(apply(
        config_name="assistant", session_refs=[{"platform_id": "ob", "session_id": "one"}],
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        heartbeat = asyncio.create_task(asyncio.sleep(0, result="alive"))
        assert await asyncio.wait_for(heartbeat, 1) == "alive"
        assert not task.done()
    finally:
        release.set()
    assert await task == [{"ok": True, "session_id": "one"}]
    assert len(read_threads) == 1
    assert read_threads[0] != threading.get_ident()
    desired_key = "desired_plugins" if kind == "plugins" else "desired_config"
    expected: dict[str, object] = {"config_name": "assistant", "session_ids": {"one"}, desired_key: None, "concurrency": 4}
    snapshot = applied[0].get("apply_snapshot")
    assert isinstance(snapshot, EdictumPluginApplySnapshot)
    expected["apply_snapshot"] = snapshot
    expected["apply_targets"] = [("one", None, "assistant", "generation")]
    guard = applied[0]["runtime_is_current"]
    assert callable(guard) and guard()
    expected["runtime_is_current"] = guard
    assert applied == [expected]
    backend._platform_runtimes.pop("ob")
    assert not guard()

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plugins", "runtime"])
async def test_read_failure_is_logged_and_does_not_apply_instances(monkeypatch, kind: str, tmp_path: Path):
    """配置读取失败记录堆栈, 不沿用旧配置安装插件"""
    def failed_read() -> None:
        """模拟文件读取失败"""
        raise OSError("source unavailable")

    messages: list[str] = []
    monkeypatch.setattr(_scoped_plugin_apply_logger, "error", messages.append)
    backend = cast(Any, BackendManager.__new__(BackendManager))
    backend._edictum_cfg = SimpleNamespace(reload=failed_read, storage_path=tmp_path / "named.json")
    backend._platform_runtimes = {}
    with pytest.raises(OSError, match="source unavailable"):
        await getattr(backend, f"reconcile_edictum_{kind}_async")(config_name="assistant")
    assert len(messages) == 1
    assert "Traceback" in messages[0] and "source unavailable" in messages[0]

_TOKEN = "control-runtime-test-only-token-0001"

@pytest.fixture
def operations(monkeypatch):
    """任务记录和认证均使用测试值, 不写入现有服务配置"""
    monkeypatch.setenv("SATRAP_API_TOKEN", _TOKEN)
    monkeypatch.setattr(control_server, "_CONTROL_AUTH", ServerAuth.create("127.0.0.1", 0))
    registry = ApplicationOperations()
    monkeypatch.setattr(control_server, "_PLUGIN_APPLICATIONS", registry)
    return registry

@pytest.mark.asyncio
@pytest.mark.parametrize("kind,location,targets", [(None, None, {"Chat", "Edictum"}), ("chat", "chat", {"Chat"}), ("edictum", "agent", {"Edictum"})])
async def test_actual_control_bridge_auth_progress_scope_and_one_dispatch(operations, monkeypatch, tmp_path: Path, kind, location, targets):
    """两个真实临时 HTTP 服务独立执行, 控制查询不重发, 单线程池在生命周期等待期间可处理其它工作"""
    release = asyncio.Event()
    calls = {"Chat": [], "Edictum": []}
    pool = BoundedAsyncWorker("test-plugin-relay", workers=1, capacity=2)
    monkeypatch.setattr(relay_module, "RUNTIME_REQUEST_WORKERS", pool)

    async def chat_apply(**scope):
        calls["Chat"].append(scope)
        with progress_instance("same-platform", "same-id"), progress_phase("install_ms"):
            await release.wait()
        return {"ok": True, "applied": False, "sessions": [{"ok": False, "session_id": "same-id", "error": "测试安装失败"}]}

    async def backend_apply(**scope):
        calls["Edictum"].append(scope)
        with progress_instance("same-platform", "same-id"), progress_phase("model_build_ms"):
            await release.wait()
        return [{"ok": True, "session_id": "same-id"}]

    chat = ChatHTTPServer(cast(Any, SimpleNamespace(reconcile_plugins=chat_apply)), port=0)
    backend = BackendHTTPServer(cast(Any, SimpleNamespace(reconcile_edictum_plugins_async=backend_apply)), port=0, static_dir=tmp_path)
    key = "control-bridge-operation-001"
    body = {"operation_id": key, "plugin_name": "插件", **({"kind": kind, "location_id": location} if kind else {})}
    try:
        await chat.start()
        await backend.start()
        if chat._server is None or backend._server is None:
            raise AssertionError("隔离 HTTP 服务未启动")
        monkeypatch.setenv("SATRAP_CHAT_PORT", str(chat._server.sockets[0].getsockname()[1]))
        backend_port = backend._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(control_server, "_configured_backend_address", lambda: ("127.0.0.1", backend_port))
        rejected = await _request("/config/plugins/reconcile", "POST", json.dumps(body).encode("utf-8"), authorized=False)
        assert rejected.startswith(b"HTTP/1.1 401") and not any(calls.values())
        response = await _request("/config/plugins/reconcile", "POST", json.dumps(body).encode("utf-8"))
        assert response.startswith(b"HTTP/1.1 202") and _json_body(response)["operation_id"] == key
        for _attempt in range(200):
            current = operations.get(key)
            service_phases = {item["platform_id"]: item["phase"] for item in current["instances"] if not item["session_id"]}
            if all(service_phases.get(target) == ("install" if target == "Chat" else "model_build") for target in targets):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("没有读到真实子服务阶段")
        assert await asyncio.wait_for(pool.run(lambda: "available"), 0.5) == "available"
        assert current["status"] == "running" and _TOKEN not in str(current)
        assert {item["platform_id"] for item in current["instances"] if item["session_id"]} == {f"{target}/same-platform" for target in targets}
        assert (await _request(f"/config/plugin-operations/{key}", authorized=False)).startswith(b"HTTP/1.1 401")
        assert _json_body(await _request(f"/config/plugin-operations/{key}"))["status"] == "running"
        assert (await _request("/config/plugin-operations/missing-operation-001")).startswith(b"HTTP/1.1 404")
        assert (await _request("/config/plugins/reconcile", "POST", json.dumps(body).encode("utf-8"))).startswith(b"HTTP/1.1 202")
        changed = {**body, "plugin_name": "different"}
        assert (await _request("/config/plugins/reconcile", "POST", json.dumps(changed).encode("utf-8"))).startswith(b"HTTP/1.1 409")
        assert {target for target, scopes in calls.items() if scopes} == targets
        assert all(len(calls[target]) == 1 for target in targets)
        assert calls["Chat"] == ([{"plugin_name": "插件"}] if "Chat" in targets else [])
        assert calls["Edictum"] == ([{"config_name": location if kind == "edictum" else None, "session_refs": None, "concurrency": 4, "plugin_name": "插件"}] if "Edictum" in targets else [])
        monkeypatch.setattr(control_server, "_configured_backend_address", lambda: (_ for _ in ()).throw(AssertionError("不能在查询期间重新路由")))
        release.set()
        for _attempt in range(300):
            final = operations.get(key)
            if final["status"] != "running":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("子服务完成后控制任务未结束")
        assert final["status"] == ("failed" if "Chat" in targets else "completed")
        assert {row["target"] for row in final["result"]["runtime"]} == targets
        assert all(len(calls[target]) == 1 for target in targets)
    finally:
        release.set()
        operation = operations._operations.get(key)
        if operation is not None and operation.task is not None:
            await asyncio.wait_for(asyncio.shield(operation.task), 5)
        await chat.stop()
        await backend.stop()
        pool.close()

@pytest.mark.asyncio
async def test_cold_save_flags_preserve_revision_and_do_not_dispatch(operations, monkeypatch, tmp_path: Path):
    """仅保存仍执行原校验/版本事务, 错误开关与冲突不写入或应用, 配置正文不进入任务记录"""
    plugin = tmp_path / "plugins" / "probe"
    plugin.mkdir(parents=True)
    (plugin / "meta.yaml").write_text("name: probe\nconfig_schema:\n  note:\n    default: old", encoding="utf-8")
    catalog = PluginCatalog(tmp_path / "builtin", tmp_path / "plugins")
    service = PluginManagementService(catalog, {}, ChatPluginRegistry(tmp_path / "chat.json"), manager=PluginConfigManager(tmp_path / "config"))
    monkeypatch.setattr(control_server, "PluginCatalog", lambda: catalog)
    monkeypatch.setattr(control_server, "_plugin_management_service", lambda *args: service)
    monkeypatch.setattr(control_server, "_read_plugin_global_config", lambda name, catalog: service.get_config(name))
    async def forbidden(**scope):
        raise AssertionError("仅保存不能协调运行实例")
    monkeypatch.setattr(control_server, "_apply_plugin_runtime", forbidden)
    original = service.get_config("probe")
    payload = {"config": {"note": "不得进入任务记录"}, "expected_revision": original["revision"], "apply_runtime": False}
    for invalid in (None, 0, "false"):
        assert (await _request("/config/plugins/probe/config", "PUT", json.dumps({**payload, "apply_runtime": invalid}).encode("utf-8"))).startswith(b"HTTP/1.1 400")
    assert service.get_config("probe")["revision"] == original["revision"]
    response = await _request("/config/plugins/probe/config", "PUT", json.dumps(payload).encode("utf-8"))
    assert response.startswith(b"HTTP/1.1 200")
    assert _json_body(response)["saved"] is True and "runtime" not in _json_body(response)
    assert service.get_config("probe")["overrides"] == payload["config"]
    assert (await _request("/config/plugins/probe/config", "PUT", json.dumps(payload).encode("utf-8"))).startswith(b"HTTP/1.1 409")
    usage = service.get_usages("probe")["locations"][0]
    state = {"present": True, "enabled": False, "capabilities": {}}
    body = {"kind": "chat", "location_id": "chat", "state": state, "expected_revision": usage["revision"], "apply_runtime": False}
    assert (await _request("/config/plugins/probe/usages", "PUT", json.dumps({**body, "apply_runtime": []}).encode("utf-8"))).startswith(b"HTTP/1.1 400")
    saved = _json_body(await _request("/config/plugins/probe/usages", "PUT", json.dumps(body).encode("utf-8")))
    assert saved["saved"] is True and "runtime" not in saved
    assert saved["locations"][0]["present"] is True
    assert not operations._operations

@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [("post_timeout", "applied"), ("post_refused", "next_activation"), ("post_503", "error"), ("lost_record", "error"), ("get_timeout", "applied"), ("wrong_id", "error")])
async def test_relay_ambiguity_never_replays_post_and_recovers_reads(monkeypatch, failure, expected):
    """接受响应丢失/查询暂时失败只读取原 ID, 接受前连接拒绝与丢失记录明确区分"""
    requests = []
    pool = BoundedAsyncWorker("test-relay-errors", workers=1, capacity=2)
    def request(url, method, body, factory):
        requests.append((method, url, body))
        child_id = body["operation_id"] if body else requests[0][2]["operation_id"]
        if method == "POST" and failure in {"post_timeout", "lost_record"}:
            raise relay_module.RuntimeRelayError("timeout")
        if method == "POST" and failure == "post_refused":
            raise relay_module.RuntimeRelayError("offline", refused=True)
        if method == "POST" and failure == "post_503":
            raise relay_module.RuntimeRelayError("queue full", status=503)
        if method == "GET" and failure == "lost_record":
            raise relay_module.RuntimeRelayError("lost", status=404)
        if method == "GET" and failure == "get_timeout" and len(requests) == 2:
            raise relay_module.RuntimeRelayError("temporary timeout")
        return 202 if method == "POST" else 200, {"operation_id": "wrong" if failure == "wrong_id" else child_id,
                "status": "running" if method == "POST" else "completed", "result": None if method == "POST" else {"ok": True, "edictum_sessions": [{"ok": True}]}}
    monkeypatch.setattr(relay_module, "_request", request)
    try:
        result = await relay_module.relay_plugin_application("Edictum", "http://test/api/apply", {}, "test-operation-id", urllib.request.Request, worker=pool, poll_interval=0.01)
        assert result["status"] == expected
        assert [method for method, _url, _body in requests].count("POST") == 1
        assert all(url.startswith("http://test/") for _method, url, _body in requests)
        if failure == "lost_record":
            assert result["confirmation"] == "unconfirmed"
        if failure == "post_503":
            assert result["confirmation"] == "not_accepted"
    finally:
        pool.close()

@pytest.mark.asyncio
async def test_remote_progress_is_bounded_atomic_and_ignores_obsolete_context():
    """只投影阶段白名单, 超限不丢实际结果, 终态或 ID 新代次均拒绝迟到观测"""
    registry = ApplicationOperations(max_instances=2, max_completed=1)
    captured = []
    entered, release = asyncio.Event(), asyncio.Event()
    root = {"phase": "applying", "status": "running", "phase_elapsed_ms": 1, "timings_ms": {}, "credential": "must-not-appear"}
    rows = [{**root, "phase": "install", "platform_id": "p", "session_id": str(index)} for index in range(3)]
    remote = {"stage": root, "instances": rows, "instances_limited": False, "secret": "must-not-appear"}
    async def action():
        captured.append(copy_context())
        progress_external("Edictum", remote)
        before = registry.get("remote-observe-task-001")
        with pytest.raises(ValueError, match="未知进度状态"):
            progress_external("Edictum", {"stage": {**root, "phase": "uninstall"}, "instances": [{**rows[0], "phase": "unknown"}]})
        after = registry.get("remote-observe-task-001")
        assert [(item["platform_id"], item["phase"]) for item in before["instances"]] == [(item["platform_id"], item["phase"]) for item in after["instances"]]
        entered.set()
        await release.wait()
        return {"ok": True, "runtime": [{"sessions": [{"ok": True}] * 3}]}
    key = "remote-observe-task-001"
    registry.submit(key, {}, action)
    await asyncio.wait_for(entered.wait(), 1)
    current = registry.get(key)
    assert len(current["instances"]) == 2 and current["instances_limited"] is True
    assert "must-not-appear" not in str(current)
    release.set()
    final = await _finished(registry, key)
    assert len(final["result"]["runtime"][0]["sessions"]) == 3
    captured[0].run(progress_external, "Edictum", remote)
    assert registry.get(key) == final
    async def done():
        return {"ok": True}
    registry.submit("evict-remote-task-002", {}, done)
    await _finished(registry, "evict-remote-task-002")
    entered.clear()
    release.clear()
    async def replacement():
        entered.set()
        await release.wait()
        return {"ok": True}
    registry.submit(key, {}, replacement)
    await asyncio.wait_for(entered.wait(), 1)
    captured[0].run(progress_external, "Edictum", remote)
    assert registry.get(key)["instances"] == [] and not registry.get(key)["instances_limited"]
    release.set()
    await _finished(registry, key)

def test_selected_runtime_target_does_not_read_other_service_config(monkeypatch):
    """Chat 应用不读取后端地址, 平台应用不依赖 Chat 端口有效"""
    monkeypatch.setattr(control_server, "_configured_backend_address", lambda: (_ for _ in ()).throw(AssertionError("无关后端地址不应读取")))
    monkeypatch.setenv("SATRAP_CHAT_PORT", "12345")
    assert control_server._plugin_runtime_targets("chat", "chat") == [("Chat", "http://127.0.0.1:12345/api/chat/plugins/reconcile")]
    monkeypatch.setattr(control_server, "_configured_backend_address", lambda: ("127.0.0.1", 12346))
    monkeypatch.setenv("SATRAP_CHAT_PORT", "invalid")
    assert control_server._plugin_runtime_targets("edictum", "agent") == [("Edictum", "http://127.0.0.1:12346/api/edictum/plugins/reconcile")]

@pytest.mark.parametrize("kind", ["refused", "http", "malformed", "oversized"])
def test_internal_request_classifies_failure_and_logs_stack(monkeypatch, kind):
    """内部鉴权和短截止保留, 拒绝/无效/过大响应记录堆栈而不打印请求凭据"""
    logs = []
    monkeypatch.setattr(relay_module.logger, "warning", logs.append)
    monkeypatch.setattr(relay_module.logger, "error", logs.append)
    def open_request(request, *, timeout):
        assert request.get_header("Authorization") == "Bearer " + _TOKEN
        assert timeout == 3 and json.loads(request.data.decode("utf-8"))["plugin_name"] == "插件"
        if kind == "refused":
            raise urllib.error.URLError(ConnectionRefusedError("offline"))
        if kind == "http":
            raise urllib.error.HTTPError("http://test", 503, "queue full", cast(Any, {}), None)
        raw = b"[]" if kind == "malformed" else b"x" * (relay_module._MAX_RESPONSE_BYTES + 1)
        return nullcontext(SimpleNamespace(status=200, read=lambda limit: raw))
    monkeypatch.setattr(relay_module.urllib.request, "urlopen", open_request)
    def factory(url, method):
        return urllib.request.Request(url, method=method, headers={"Authorization": "Bearer " + _TOKEN})
    with pytest.raises(relay_module.RuntimeRelayError) as captured:
        relay_module._request("http://test", "POST", {"plugin_name": "插件"}, factory)
    assert captured.value.refused is (kind == "refused")
    assert captured.value.status == (503 if kind == "http" else None)
    assert logs and all("Traceback" in line and _TOKEN not in line for line in logs)
