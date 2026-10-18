"""业务链路场景与集中边界契约"""
from __future__ import annotations

from support.platform_application import _platforms, _release

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, cast
import pytest
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.framework.SessionManager import SessionEntry, SessionManager
from satrap.core.framework.providers.edictum import EdictumProvider
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.plugin_apply import EdictumPluginApplySnapshot
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.edictum.registry import create_default_edictum_type_registry
import importlib
from dataclasses import replace
from satrap.core.config.model_snapshot import ModelConfigSnapshot
from satrap.core.storage import StorageLayout
from satrap.core.type import ASRConfig, EmbeddingConfig, ReRankConfig



@pytest.mark.asyncio
async def test_actual_application_reports_capture_lock_uninstall_and_install(tmp_path: Path, monkeypatch: Any) -> None:
    """真实隔离平台生命周期按实际边界报告阶段, 查询不触发第二次应用"""
    backend, configs, globals_, sessions, providers = await _platforms(tmp_path, monkeypatch)
    monkeypatch.setenv("SATRAP_API_TOKEN", "test-application-progress-token-at-least-thirty-two-characters")
    server = BackendHTTPServer(backend, port=0, static_dir=tmp_path)
    captured, capture_resume = threading.Event(), threading.Event()
    uninstalling, uninstall_resume = asyncio.Event(), asyncio.Event()
    installing, install_resume = asyncio.Event(), asyncio.Event()
    original_capture = EdictumPluginApplySnapshot.capture
    original_install = sessions[0].install_plugin
    original_uninstall = sessions[0].uninstall_plugin
    entry = backend._platform_runtimes["one"][0].pool.list_entries()["one-0"]
    key = "actual-platform-operation-001"

    def paused_capture(*args, **kwargs):
        """完成真实文件捕获后暂停来源工作线程"""
        result = original_capture(*args, **kwargs)
        captured.set()
        if not capture_resume.wait(3):
            raise RuntimeError("测试未恢复来源读取")
        return result

    async def paused_install(*args, **kwargs):
        """暂停真实安装入口, 恢复后仍执行原安装"""
        installing.set()
        await install_resume.wait()
        return await original_install(*args, **kwargs)

    async def paused_uninstall(*args, **kwargs):
        """暂停真实卸载入口, 恢复后仍执行原卸载"""
        uninstalling.set()
        await uninstall_resume.wait()
        return await original_uninstall(*args, **kwargs)

    async def query_phase(phase: str, instance: bool = False):
        """重复读取已接受任务, 不发送新的协调请求"""
        for _attempt in range(100):
            status, result = await server._route("GET", f"/api/runtime-operations/{key}", b"")
            assert status == 200
            stages = result["instances"] if instance else [result["stage"]]
            if any(stage["phase"] == phase for stage in stages):
                return result
            await asyncio.sleep(0.01)
        raise AssertionError(f"实际阶段未出现: {phase}")

    try:
        plugin = configs.plugin_catalog.get("session_commands")
        if plugin is None:
            raise AssertionError("测试插件不存在")
        old = sessions[0].list_plugins()[0]
        globals_.save_global("session_commands", plugin.config_schema, {"about_text": "实际进度验收"})
        monkeypatch.setattr(EdictumPluginApplySnapshot, "capture", paused_capture)
        monkeypatch.setattr(sessions[0], "install_plugin", paused_install)
        monkeypatch.setattr(sessions[0], "uninstall_plugin", paused_uninstall)
        await entry.async_operation_lock.acquire()
        body = json.dumps({"operation_id": key, "session_refs": [{"platform_id": "one", "session_id": "one-0"}]}).encode("utf-8")
        status, result = await server._route("POST", "/api/edictum/plugins/reconcile", body)
        assert status == 202
        assert await asyncio.to_thread(captured.wait, 2)
        await query_phase("config_read")
        capture_resume.set()
        await query_phase("waiting_lock", True)
        assert sessions[0].list_plugins()[0] is old
        entry.async_operation_lock.release()
        await asyncio.wait_for(uninstalling.wait(), 2)
        await query_phase("uninstall", True)
        uninstall_resume.set()
        await asyncio.wait_for(installing.wait(), 2)
        await query_phase("install", True)
        install_resume.set()
        result = await query_phase("finished")
        assert result["status"] == "completed" and result["result"]["edictum_sessions"][0]["ok"]
        assert result["instances"][0]["status"] == "completed"
        assert all(result["instances"][0]["timings_ms"][name] > 0 for name in ("waiting_lock", "install", "uninstall"))
        assert await sessions[0].run("/about") == "实际进度验收"
    finally:
        capture_resume.set()
        install_resume.set()
        uninstall_resume.set()
        if entry.async_operation_lock.locked():
            entry.async_operation_lock.release()
        task = server.applications._operations.get(key)
        if task is not None and task.task is not None:
            await asyncio.wait_for(asyncio.shield(task.task), 4)
        await _release(sessions, providers)

@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["capture", "lock", "source_check"])
async def test_plugin_batch_rejects_platform_manager_replacement(tmp_path: Path, monkeypatch: Any, phase: str) -> None:
    """来源捕获/等待锁/线程核验期间所属平台被替换, 旧管理器不执行插件生命周期"""
    backend, configs, globals_, sessions, providers = await _platforms(tmp_path, monkeypatch)
    original_runtime = backend._platform_runtimes["one"]
    replacement = backend._platform_runtimes["two"]
    entry = original_runtime[0].pool.list_entries()["one-0"]
    old = sessions[0].list_plugins()[0]
    entered = threading.Event()
    resume = threading.Event()
    capture = EdictumPluginApplySnapshot.capture

    def blocked_capture(*args, **kwargs):
        """以实际快照作为测试来源, 只在指定交界暂停工作线程"""
        snapshot = capture(*args, **kwargs)
        if phase == "source_check":
            original_check = type(snapshot.sources).is_current

            def blocked_check(sources):
                """来源核验返回前允许所属平台变化"""
                entered.set()
                if not resume.wait(3):
                    raise RuntimeError("测试未恢复来源核验")
                return original_check(sources)

            monkeypatch.setattr(type(snapshot.sources), "is_current", blocked_check)
        else:
            entered.set()
            if phase == "capture" and not resume.wait(3):
                raise RuntimeError("测试未恢复捕获")
        return snapshot

    try:
        plugin = configs.plugin_catalog.get("session_commands")
        if plugin is None:
            raise AssertionError("测试插件不存在")
        globals_.save_global("session_commands", plugin.config_schema, {"about_text": "替换后才允许应用"})
        monkeypatch.setattr(EdictumPluginApplySnapshot, "capture", blocked_capture)
        if phase == "lock":
            await entry.async_operation_lock.acquire()
        task = asyncio.create_task(backend.reconcile_edictum_plugins_async(session_refs=[{"platform_id": "one", "session_id": "one-0"}]))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            backend._platform_runtimes["one"] = replacement
        finally:
            resume.set()
            if phase == "lock":
                entry.async_operation_lock.release()
        results = await asyncio.wait_for(task, 4)
        assert len(results) == 1 and not results[0]["ok"] and "平台运行管理器已变化" in results[0]["error"]
        assert sessions[0].list_plugins()[0] is old
        backend._platform_runtimes["one"] = original_runtime
        retry = await backend.reconcile_edictum_plugins_async(session_refs=[{"platform_id": "one", "session_id": "one-0"}])
        assert retry[0]["ok"] and await sessions[0].run("/about") == "替换后才允许应用"
    finally:
        resume.set()
        await _release(sessions, providers)

def test_plugin_models_exclude_unrelated_platform_file(tmp_path: Path) -> None:
    """按插件引用过滤后, 不读取其它 Agent 所属平台的无关损坏模型文件"""
    plugin = tmp_path / "plugins" / "resource_probe"
    plugin.mkdir(parents=True)
    (plugin / "meta.yaml").write_text(
        "name: resource_probe\nconfig_schema:\n  model:\n    type: llm\n    default: resource\n", encoding="utf-8",
    )
    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / "named.json")
    configs.plugin_catalog.user_dir = plugin.parent
    configs.create("related", {"edictum_type": "async_simple", "plugins": ["resource_probe"]})
    configs.create("other", {"edictum_type": "async_simple", "plugins": ["session_commands"]})
    related = ModelConfigManager(tmp_path / "related-models.json")
    related.update_llm_config("resource", model="expected", api_key="test")
    other = ModelConfigManager(tmp_path / "other-models.json")
    other.storage_path.write_text("broken", encoding="utf-8")
    snapshot = EdictumPluginApplySnapshot.capture(
        configs, {"related", "other"}, plugin_name="resource_probe", global_manager=PluginConfigManager(tmp_path / "globals"),
        model_managers=(related, other), include_main_models=False,
        model_config_scopes={related.storage_path.absolute(): {"related"}, other.storage_path.absolute(): {"other"}},
    )
    assert snapshot.includes_config("related") and not snapshot.includes_config("other")
    captured = snapshot.models_for(related)
    if captured is None:
        raise AssertionError("相关平台模型未捕获")
    assert captured.get_llm_config("resource").model == "expected"
    with pytest.raises(KeyError):
        snapshot.models_for(other)
    assert other.storage_path.read_text(encoding="utf-8") == "broken"

@pytest.mark.asyncio
async def test_replaced_platform_failure_does_not_stop_other_platform(tmp_path: Path, monkeypatch: Any) -> None:
    """同批一个平台变化只拒绝其两个旧实例, 其它平台仍按自己的身份应用"""
    backend, configs, globals_, sessions, providers = await _platforms(tmp_path, monkeypatch)
    capture = EdictumPluginApplySnapshot.capture

    def replaced_after_capture(*args, **kwargs):
        """取得同批真实来源后模拟平台移除"""
        result = capture(*args, **kwargs)
        backend._platform_runtimes.pop("one")
        return result

    try:
        plugin = configs.plugin_catalog.get("session_commands")
        if plugin is None:
            raise AssertionError("测试插件不存在")
        old_handles = [session.list_plugins()[0] for session in sessions]
        globals_.save_global("session_commands", plugin.config_schema, {"about_text": "其它平台正常应用"})
        monkeypatch.setattr(EdictumPluginApplySnapshot, "capture", replaced_after_capture)
        results = await backend.reconcile_edictum_plugins_async()
        assert len(results) == 3
        assert all(not row["ok"] and "平台运行管理器已变化" in row["error"] for row in results[:2])
        assert results[2]["ok"] and results[2]["platform_id"] == "two"
        assert sessions[0].list_plugins()[0] is old_handles[0] and sessions[1].list_plugins()[0] is old_handles[1]
        assert await sessions[2].run("/about") == "其它平台正常应用"
    finally:
        await _release(sessions, providers)

@pytest.mark.asyncio
async def test_cross_platform_application_parses_once_and_keeps_override_handle(tmp_path: Path, monkeypatch: Any) -> None:
    """同次跨平台刷新命名配置及插件参数各一次, 覆盖不变时不重装"""
    backend, configs, globals_, sessions, providers = await _platforms(tmp_path, monkeypatch)
    try:
        sessions[1].plugin_override_store.replace(sessions[1].session_id, "plugins.session_commands", {"about_text": "实例专用"}, expected_revision=0)
        await backend.reconcile_edictum_plugins_async(plugin_name="session_commands")
        handles = [session.list_plugins()[0] for session in sessions]
        entry = configs.plugin_catalog.get("session_commands")
        if entry is None:
            raise AssertionError("测试插件未加载")
        globals_.save_global("session_commands", entry.config_schema, {"about_text": "跨平台新参数"})
        read = globals_.load_global_explicit
        reload = configs.reload
        parsed: list[str] = []
        reloads: list[int] = []

        def counted(name, schema):
            """计数实际全局 JSON 解析"""
            parsed.append(name)
            return read(name, schema)

        def refreshed(**kwargs):
            """计数实际命名文件加载"""
            reloads.append(threading.get_ident())
            reload(**kwargs)

        monkeypatch.setattr(globals_, "load_global_explicit", counted)
        monkeypatch.setattr(configs, "reload", refreshed)
        result = await backend.reconcile_edictum_plugins_async(plugin_name="session_commands")
        assert len(result) == 3 and all(item["ok"] for item in result)
        assert parsed == ["session_commands"] and len(reloads) == 1
        assert reloads[0] != threading.get_ident()
        assert [await session.run("/about") for session in sessions] == ["跨平台新参数", "实例专用", "跨平台新参数"]
        assert sessions[1].list_plugins()[0] is handles[1] and result[1]["plugins"] == []
        assert sessions[0].list_plugins()[0] is not handles[0]
        assert sessions[2].list_plugins()[0] is not handles[2]
        assert all(item["timings_ms"]["shared_config_read_ms"] >= 0 for item in result)
    finally:
        await _release(sessions, providers)

@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["global", "named"])
async def test_platform_queued_application_rejects_old_source(tmp_path: Path, monkeypatch: Any, source: str) -> None:
    """等待锁期间命名或全局来源变化, 不使用旧目标且重试加载最新内容"""
    backend, configs, globals_, sessions, providers = await _platforms(tmp_path, monkeypatch)
    captured = threading.Event()
    capture = EdictumPluginApplySnapshot.capture

    def record(*args, **kwargs):
        """通知测试真实捕获已完成"""
        snapshot = capture(*args, **kwargs)
        captured.set()
        return snapshot

    try:
        entry = backend._platform_runtimes["one"][0].pool.list_entries()["one-0"]
        old = sessions[0].list_plugins()[0]
        plugin = configs.plugin_catalog.get("session_commands")
        if plugin is None:
            raise AssertionError("测试插件未加载")
        globals_.save_global("session_commands", plugin.config_schema, {"about_text": "捕获旧值"})
        monkeypatch.setattr(EdictumPluginApplySnapshot, "capture", record)
        await entry.async_operation_lock.acquire()
        task = asyncio.create_task(backend.reconcile_edictum_plugins_async(session_refs=[{"platform_id": "one", "session_id": "one-0"}]))
        try:
            assert await asyncio.to_thread(captured.wait, 2)
            if source == "global":
                globals_.save_global("session_commands", plugin.config_schema, {"about_text": "重试新值"})
            else:
                configs.update("agent-a", {"plugins": [{"name": "session_commands", "config": {"about_text": "重试新值"}}]})
        finally:
            entry.async_operation_lock.release()
        result = await asyncio.wait_for(task, 3)
        assert len(result) == 1 and not result[0]["ok"] and "等待期间已变化" in result[0]["error"]
        assert sessions[0].list_plugins()[0] is old
        retried = await backend.reconcile_edictum_plugins_async(session_refs=[{"platform_id": "one", "session_id": "one-0"}])
        assert retried[0]["ok"] and await sessions[0].run("/about") == "重试新值"
    finally:
        await _release(sessions, providers)

@pytest.mark.asyncio
async def test_removed_reference_unloads_and_unrelated_corrupt_global_is_not_read(tmp_path: Path, monkeypatch: Any) -> None:
    """移除旧插件仍卸载, 不引用目标的配置不读取损坏全局参数"""
    backend, configs, globals_, sessions, providers = await _platforms(tmp_path, monkeypatch)
    try:
        configs.update("agent-b", {"plugins": ["group_chat"]})
        await backend.reconcile_edictum_plugins_async(config_name="agent-b")
        configs.update("agent-a", {"plugins": []})
        globals_._dir.mkdir(parents=True, exist_ok=True)
        globals_.global_path("group_chat").write_text("broken", encoding="utf-8")
        result = await backend.reconcile_edictum_plugins_async(plugin_name="session_commands")
        assert len(result) == 2 and all(item["ok"] for item in result)
        assert all(item["plugins"][0]["action"] == "remove" for item in result)
        assert sessions[0].list_plugins() == [] and sessions[1].list_plugins() == []
        assert sessions[2].list_plugins()[0].name == "group_chat"
    finally:
        await _release(sessions, providers)

@pytest.mark.asyncio
async def test_new_reference_is_selected_from_independent_cold_write(tmp_path: Path, monkeypatch: Any) -> None:
    """范围在刷新后的新引用中判断, 后端旧内存配置不能遗漏新添加插件"""
    backend, configs, _globals, sessions, providers = await _platforms(tmp_path, monkeypatch)
    try:
        external = EdictumConfigManager(configs.type_registry, configs.storage_path)
        external.update("agent-b", {"plugins": ["session_commands", "group_chat"]})
        before = configs.get_config("agent-b")
        if before is None:
            raise AssertionError("测试配置未创建")
        assert [item["name"] for item in before["plugins"]] == ["session_commands"]
        result = await backend.reconcile_edictum_plugins_async(plugin_name="group_chat")
        assert len(result) == 1 and result[0]["platform_id"] == "two" and result[0]["ok"]
        assert {plugin.name for plugin in sessions[2].list_plugins()} == {"session_commands", "group_chat"}
        assert all(len(session.list_plugins()) == 1 for session in sessions[:2])
    finally:
        await _release(sessions, providers)

@pytest.mark.asyncio
async def test_named_source_change_during_capture_retries_complete_target(tmp_path: Path, monkeypatch: Any) -> None:
    """命名文件在刷新后变化时重新捕获全部范围, 不把旧内容与新版本配对"""
    backend, configs, _globals, sessions, providers = await _platforms(tmp_path, monkeypatch)
    reload = configs.reload
    external = EdictumConfigManager(configs.type_registry, configs.storage_path)
    calls: list[int] = []

    def changing(**kwargs):
        """首次刷新结束后修改独立来源"""
        reload(**kwargs)
        calls.append(1)
        if len(calls) == 1:
            external.update("agent-b", {"plugins": ["session_commands", "group_chat"]})

    try:
        monkeypatch.setattr(configs, "reload", changing)
        result = await backend.reconcile_edictum_plugins_async(plugin_name="group_chat")
        assert len(calls) == 2
        assert len(result) == 1 and result[0]["platform_id"] == "two" and result[0]["ok"]
        assert {plugin.name for plugin in sessions[2].list_plugins()} == {"session_commands", "group_chat"}
    finally:
        await _release(sessions, providers)

@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["global", "named"])
async def test_corrupt_platform_source_preserves_all_runtime_plugins(tmp_path: Path, monkeypatch: Any, source: str) -> None:
    """损坏命名配置或已引用全局参数时不安装默认参数, 保留原句柄"""
    backend, configs, globals_, sessions, providers = await _platforms(tmp_path, monkeypatch)
    try:
        old = [session.list_plugins()[0] for session in sessions]
        path = configs.storage_path if source == "named" else globals_.global_path("session_commands")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("broken", encoding="utf-8")
        with pytest.raises(ValueError):
            await backend.reconcile_edictum_plugins_async(plugin_name="session_commands")
        assert all(session.list_plugins()[0] is handle for session, handle in zip(sessions, old))
    finally:
        await _release(sessions, providers)

async def _setup(tmp_path: Path, monkeypatch: Any, asynchronous: bool = True) -> tuple[Any, Any, Any, list[Any], list[Any], list[Any], list[Any]]:
    """两个实际平台管理器及三个 Simple 会话, 所有存储隔离且模型不联网"""
    globals_ = PluginConfigManager(tmp_path / "globals")
    monkeypatch.setattr("satrap.edictum.plugin_apply.PluginConfigManager", lambda: globals_)
    monkeypatch.setattr("satrap.edictum.plugin_settings.PluginConfigManager", lambda: globals_)
    types = create_default_edictum_type_registry()
    configs = EdictumConfigManager(types, tmp_path / "agents.json")
    configs.create("assistant", {"edictum_type": "async_simple" if asynchronous else "simple", "model_name": "demo",
                                 "params": {"system_prompt": "before"}, "plugins": ["session_commands"]})
    models = ModelConfigManager(tmp_path / "models.json")
    models.update_llm_config("demo", api_key="test-key", model="demo")
    builds: list[Any] = []

    def build(*args, **kwargs):
        """记录真实构建入口, 返回无需联网的占位模型"""
        value = object()
        builds.append(value)
        return value

    monkeypatch.setattr(importlib.import_module("satrap.core.framework.SessionManager"), "build_llm_from_config", build)
    backend = cast(Any, BackendManager.__new__(BackendManager))
    backend._edictum_cfg = configs
    backend._platform_runtimes = {}
    managers: list[Any] = []
    providers: list[Any] = []
    sessions: list[Any] = []
    layout = StorageLayout(tmp_path / "data")
    for pid, count in (("one", 2), ("two", 1)):
        layout.ensure_platform(pid, platform_type="onebot")
        manager = SessionManager(db_path=tmp_path / f"{pid}.db", platform_id=pid, storage_layout=layout)
        manager.plugin_environment = PluginEnvironment("platform", "onebot")
        provider = EdictumProvider(configs, types, default_checkpoint_db=str(tmp_path / f"{pid}-context.db"))
        manager.register_provider(provider)
        manager.model_config_manager = models
        managers.append(manager)
        providers.append(provider)
        backend._platform_runtimes[pid] = (manager, None)
        for index in range(count):
            sid = f"{pid}-{index}"
            manager.register_session_from_provider_config("edictum", "assistant", session_id=sid,
                                                          extra_params={"system_prompt": "local"} if index == 1 else {})
            assert await manager.activate_session_async(sid)
            entry = manager.pool.get(sid)
            if entry is None:
                raise AssertionError("测试实例未激活")
            sessions.append(entry.session)
    return backend, configs, globals_, managers, providers, sessions, builds

async def _close(managers: list[Any]) -> None:
    """卸载所有仍活跃的隔离实例"""
    for manager in managers:
        for sid in list(manager.pool.list_entries()):
            await manager.unload_session_async(sid)

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_full_application_model_build_does_not_block_progress_queries(tmp_path: Path, monkeypatch: Any, asynchronous: bool) -> None:
    """实际完整候选构建在工作线程, 同步/异步会话均可查询模型构建阶段且旧实例未提前切换"""
    backend, configs, _globals, managers, _providers, sessions, builds = await _setup(tmp_path, monkeypatch, asynchronous)
    monkeypatch.setenv("SATRAP_API_TOKEN", "test-full-progress-token-at-least-thirty-two-characters")
    server = BackendHTTPServer(backend, port=0, static_dir=tmp_path)
    module = importlib.import_module("satrap.core.framework.SessionManager")
    original_build = module.build_llm_from_config
    entered, release = threading.Event(), threading.Event()
    construction_threads = []
    key = "full-model-build-operation-001"

    def paused_build(*args, **kwargs):
        """在真实模型构建入口暂停, 放行后仍由原夹具记录候选模型"""
        construction_threads.append(threading.get_ident())
        entered.set()
        if not release.wait(3):
            raise RuntimeError("测试未恢复模型构建")
        return original_build(*args, **kwargs)

    try:
        monkeypatch.setattr(module, "build_llm_from_config", paused_build)
        configs.update("assistant", {"params": {"system_prompt": "new prompt"}})
        body = json.dumps({"operation_id": key, "session_refs": [{"platform_id": "one", "session_id": "one-0"}]}).encode("utf-8")
        status, _result = await server._route("POST", "/api/edictum/runtime/apply", body)
        assert status == 202 and await asyncio.to_thread(entered.wait, 2)
        status, progress = await asyncio.wait_for(server._route("GET", f"/api/runtime-operations/{key}", b""), 1)
        assert status == 200 and progress["instances"][0]["phase"] == "model_build"
        assert construction_threads == [construction_threads[0]] and construction_threads[0] != threading.get_ident()
        assert managers[0].pool.get("one-0").session is sessions[0] and len(builds) == 3
        release.set()
        for _attempt in range(300):
            _status, result = await server._route("GET", f"/api/runtime-operations/{key}", b"")
            if result["status"] != "running":
                break
            await asyncio.sleep(0.01)
        assert result["status"] == "completed" and result["result"]["edictum_sessions"][0]["action"] == "restart"
        assert result["instances"][0]["timings_ms"]["model_build"] > 0
        assert managers[0].pool.get("one-0").session is not sessions[0] and len(builds) == 4
    finally:
        release.set()
        operation = server.applications._operations.get(key)
        if operation is not None and operation.task is not None:
            await asyncio.wait_for(asyncio.shield(operation.task), 5)
        await _close(managers)

@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["lock", "source_check", "candidate"])
async def test_full_batch_rejects_platform_manager_replacement(tmp_path: Path, monkeypatch: Any, phase: str) -> None:
    """来源准备/等待锁/来源复核/候选准备期间平台管理器变化, 不提交旧批次"""
    backend, configs, _globals, managers, providers, sessions, builds = await _setup(tmp_path, monkeypatch)
    original_runtime = backend._platform_runtimes["one"]
    entered = threading.Event()
    resume = threading.Event()
    candidate_entered = asyncio.Event()
    candidate_resume = asyncio.Event()
    candidates: list[Any] = []
    capture = EdictumPluginApplySnapshot.capture
    prepare = providers[0].prepare_session_async

    def paused_capture(*args, **kwargs):
        """保留真实捕获结果, 指定阶段用有界屏障暂停"""
        snapshot = capture(*args, **kwargs)
        if phase == "source_check":
            original_check = type(snapshot.sources).is_current

            def paused_check(sources):
                """来源读取后尚未返回到事件循环时暂停"""
                entered.set()
                if not resume.wait(3):
                    raise RuntimeError("测试未恢复来源检查")
                return original_check(sources)

            monkeypatch.setattr(type(snapshot.sources), "is_current", paused_check)
        else:
            entered.set()
            if phase == "capture" and not resume.wait(3):
                raise RuntimeError("测试未恢复捕获")
        return snapshot

    async def paused_prepare(session, **kwargs):
        """完成实际插件安装后暂停候选, 确认失效时会清理候选资源"""
        await prepare(session, **kwargs)
        candidates.append(session)
        candidate_entered.set()
        await candidate_resume.wait()

    try:
        entry = managers[0].pool.get("one-0")
        old_handle = sessions[0].list_plugins()[0]
        await sessions[0].ctx.add_user_message("原运行时上下文保留")
        old_context = sessions[0].ctx.get_context()
        configs.update("assistant", {"params": {"system_prompt": "after"}})
        monkeypatch.setattr(EdictumPluginApplySnapshot, "capture", paused_capture)
        if phase == "lock":
            await entry.async_operation_lock.acquire()
        if phase == "candidate":
            monkeypatch.setattr(providers[0], "prepare_session_async", paused_prepare)
        task = asyncio.create_task(backend.reconcile_edictum_runtime_async(session_refs=[{"platform_id": "one", "session_id": "one-0"}]))
        try:
            if phase == "candidate":
                await asyncio.wait_for(candidate_entered.wait(), 4)
            else:
                assert await asyncio.to_thread(entered.wait, 2)
            backend._platform_runtimes["one"] = backend._platform_runtimes["two"]
        finally:
            resume.set()
            candidate_resume.set()
            if phase == "lock":
                entry.async_operation_lock.release()
        result = await asyncio.wait_for(task, 5)
        assert len(result) == 1 and not result[0]["ok"] and "平台运行管理器已变化" in result[0]["error"]
        assert managers[0].pool.get("one-0").session is sessions[0]
        assert sessions[0].list_plugins()[0] is old_handle and sessions[0].ctx.get_context() == old_context
        assert managers[1].pool.get("two-0").session is sessions[2]
        assert len(builds) == (4 if phase == "candidate" else 3)
        if candidates:
            assert candidates[0].list_plugins() == []
        backend._platform_runtimes["one"] = original_runtime
        retry = await backend.reconcile_edictum_runtime_async(session_refs=[{"platform_id": "one", "session_id": "one-0"}])
        assert retry[0]["ok"] and managers[0].pool.get("one-0").session is not sessions[0]
    finally:
        resume.set()
        candidate_resume.set()
        await _close(managers)

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_full_batch_uses_one_source_and_preserves_effective_override(tmp_path: Path, monkeypatch: Any, asynchronous: bool) -> None:
    """完整重建候选共享全局参数及命名文件, 有效目标不变实例不重建"""
    backend, configs, globals_, managers, _providers, sessions, builds = await _setup(tmp_path, monkeypatch, asynchronous)
    try:
        sessions[1].plugin_override_store.replace(sessions[1].session_id, "plugins.session_commands", {"about_text": "local"}, expected_revision=0)
        await backend.reconcile_edictum_plugins_async()
        old_handles = [session.list_plugins()[0] for session in sessions]
        plugin = configs.plugin_catalog.get("session_commands")
        if plugin is None:
            raise AssertionError("测试插件未加载")
        globals_.save_global("session_commands", plugin.config_schema, {"about_text": "new global"})
        configs.update("assistant", {"params": {"system_prompt": "after"}})
        reload = configs.reload
        load = globals_.load_global_explicit
        refreshed: list[int] = []
        parsed: list[str] = []

        def counted_reload(**kwargs):
            """记录实际命名文件刷新"""
            refreshed.append(threading.get_ident())
            reload(**kwargs)

        def counted_load(name, schema):
            """记录实际全局参数解析"""
            parsed.append(name)
            return load(name, schema)

        monkeypatch.setattr(configs, "reload", counted_reload)
        monkeypatch.setattr(globals_, "load_global_explicit", counted_load)
        result = await backend.reconcile_edictum_runtime_async(config_name="assistant")
        assert [item["action"] for item in result] == ["restart", "noop", "restart"]
        assert all(item["ok"] for item in result)
        assert len(refreshed) == 1 and refreshed[0] != threading.get_ident()
        assert parsed == ["session_commands"] and len(builds) == 5
        assert managers[0].pool.get("one-1").session is sessions[1]
        assert sessions[1].list_plugins()[0] is old_handles[1]
        for manager, sid in ((managers[0], "one-0"), (managers[1], "two-0")):
            session = manager.pool.get(sid).session
            about = await session.run("/about") if asynchronous else session.run("/about")
            assert about == "new global"
            meta = manager.get_session_runtime_metadata(sid)
            assert meta["config"]["applied"]["params"]["system_prompt"] == "after"
        repeated = await backend.reconcile_edictum_runtime_async(config_name="assistant")
        assert all(item["ok"] and item["action"] == "noop" for item in repeated) and len(builds) == 5
    finally:
        await _close(managers)

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_full_application_uses_fresh_shared_models_and_skips_independent_model(tmp_path: Path, monkeypatch: Any, asynchronous: bool) -> None:
    """同名模型参数变化进入重建判断, 同来源跨平台解析一次且不同模型实例不重建"""
    backend, _configs, _globals, managers, _providers, sessions, builds = await _setup(tmp_path, monkeypatch, asynchronous)
    models = managers[0].model_config_manager
    read = ModelConfigSnapshot.read
    source_reads = []
    received = []
    module = importlib.import_module("satrap.core.framework.SessionManager")
    build = module.build_llm_from_config

    def counted_read(manager):
        """记录实际文件解析"""
        source_reads.append(manager.storage_path)
        return read(manager)

    def counted_build(config, **kwargs):
        """记录真正用于构建客户端的独立模型参数"""
        received.append(replace(config))
        return build(config, **kwargs)

    try:
        models.update_llm_config("isolated", model="isolated", api_key="test")
        managers[0].update_session_config("one-1", {"system_prompt": "local", "model_name": "isolated"})
        prepared = await backend.reconcile_edictum_runtime_async(session_refs=[{"platform_id": "one", "session_id": "one-1"}])
        assert prepared[0]["ok"] and prepared[0]["action"] == "restart"
        protected = managers[0].pool.get("one-1").session
        protected_handle = protected.list_plugins()[0]
        external = ModelConfigManager(models.storage_path)
        external.update_llm_config("demo", model="new-model", temperature=0.7, context_window=64000, history_ratio=0.6)
        before = models.storage_path.read_bytes()
        modified_at = models.storage_path.stat().st_mtime_ns
        assert models.get_llm_config("demo").model == "new-model"
        monkeypatch.setattr(ModelConfigSnapshot, "read", counted_read)
        monkeypatch.setattr(module, "build_llm_from_config", counted_build)
        result = await backend.reconcile_edictum_runtime_async(config_name="assistant")
        assert [item["action"] for item in result] == ["restart", "noop", "restart"]
        assert all(item["ok"] for item in result) and len(builds) == 6
        assert source_reads == [models.storage_path]
        assert [config.model for config in received] == ["new-model", "new-model"]
        assert all(config.temperature == 0.7 and config.context_window == 64000 and config.history_ratio == 0.6 for config in received)
        for manager, sid in ((managers[0], "one-0"), (managers[1], "two-0")):
            context = manager.pool.get(sid).session.ctx
            assert context.max_context == 64000 and context.history_ratio == 0.6
        assert managers[0].pool.get("one-1").session is protected and protected.list_plugins()[0] is protected_handle
        assert all(manager.pool.get(sid).session.plugin_model_manager is models for manager, sid in ((managers[0], "one-0"), (managers[1], "two-0")))
        assert models.storage_path.read_bytes() == before and models.storage_path.stat().st_mtime_ns == modified_at
        repeated = await backend.reconcile_edictum_runtime_async(config_name="assistant")
        assert all(item["ok"] and item["action"] == "noop" for item in repeated) and len(builds) == 6
    finally:
        await _close(managers)

@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["candidate", "corrupt"])
async def test_full_model_source_change_or_corruption_preserves_runtime(tmp_path: Path, monkeypatch: Any, change: str) -> None:
    """模型来源排队或准备期间变化以及损坏都不能提交旧候选, 保留上下文和插件"""
    backend, configs, _globals, managers, providers, sessions, builds = await _setup(tmp_path, monkeypatch)
    models = managers[0].model_config_manager
    captured = threading.Event()
    entered = asyncio.Event()
    release = asyncio.Event()
    capture = EdictumPluginApplySnapshot.capture
    prepare = providers[0].prepare_session_async
    candidates = []

    def record(*args, **kwargs):
        """通知真实完整捕获完成"""
        snapshot = capture(*args, **kwargs)
        captured.set()
        return snapshot

    async def paused(session, **kwargs):
        """完成真实候选插件准备后暂停"""
        await prepare(session, **kwargs)
        candidates.append(session)
        entered.set()
        await release.wait()

    try:
        old_handle = sessions[0].list_plugins()[0]
        await sessions[0].ctx.add_user_message("保留模型变更前上下文")
        before = sessions[0].ctx.get_context()
        configs.update("assistant", {"params": {"system_prompt": "after"}})
        refs = [{"platform_id": "one", "session_id": "one-0"}]
        if change == "corrupt":
            models.storage_path.write_text("broken", encoding="utf-8")
            with pytest.raises(ValueError):
                await backend.reconcile_edictum_runtime_async(session_refs=refs)
            assert models.storage_path.read_text(encoding="utf-8") == "broken" and len(builds) == 3
        else:
            monkeypatch.setattr(EdictumPluginApplySnapshot, "capture", record)
            entry = managers[0].pool.get("one-0")
            if change == "queued":
                await entry.async_operation_lock.acquire()
            else:
                monkeypatch.setattr(providers[0], "prepare_session_async", paused)
            task = asyncio.create_task(backend.reconcile_edictum_runtime_async(session_refs=refs))
            try:
                if change == "queued":
                    assert await asyncio.to_thread(captured.wait, 2)
                else:
                    await asyncio.wait_for(entered.wait(), 5)
                writer = ModelConfigManager(models.storage_path)
                writer.update_llm_config("demo", temperature=0.9)
            finally:
                if change == "queued":
                    entry.async_operation_lock.release()
                release.set()
            result = await asyncio.wait_for(task, 5)
            assert not result[0]["ok"] and "已变化" in result[0]["error"]
            if candidates:
                assert candidates[0].list_plugins() == [] and candidates[0].plugin_model_manager is models
        assert managers[0].pool.get("one-0").session is sessions[0]
        assert sessions[0].list_plugins()[0] is old_handle and sessions[0].ctx.get_context() == before
    finally:
        release.set()
        await _close(managers)

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("application", ["full", "plugins", "direct"])
async def test_application_plugin_resources_use_captured_model_source(tmp_path: Path, monkeypatch: Any, asynchronous: bool, application: str) -> None:
    """实际插件安装读取同代四类模型资源, 运行期仍恢复原管理器且重试无重复安装"""
    backend, configs, _globals, managers, providers, sessions, builds = await _setup(tmp_path, monkeypatch, asynchronous)
    models = managers[0].model_config_manager
    try:
        writer = ModelConfigManager(models.storage_path)
        writer.set_embedding_config(EmbeddingConfig(model="embedding-new", api_key="test"), "resource")
        writer.set_rerank_config(ReRankConfig(model="rerank-new", api_key="test", base_url="http://localhost:1"), "resource")
        writer.set_asr_config(ASRConfig(model="asr-new", api_key="test"), "resource")
        assert models.has_config("embedding", "resource")
        plugin = tmp_path / "plugins" / "model_probe"
        plugin.mkdir(parents=True)
        (plugin / "meta.yaml").write_text(
            "name: model_probe\nversion: 0.1.0\nconfig_schema:\n"
            "  llm:\n    type: llm\n    default: demo\n"
            "  embed:\n    type: embed\n    default: resource\n"
            "  rerank:\n    type: rerank\n    default: resource\n"
            "  asr:\n    type: asr\n    default: resource\n", encoding="utf-8",
        )
        (plugin / "tools.py").write_text(
            "def get_tools(session, config, *, resources):\n"
            "    session._probe_resources = resources\n"
            "    return []\n", encoding="utf-8",
        )
        configs.plugin_catalog.user_dir = plugin.parent
        configs.update("assistant", {"plugins": ["session_commands", "model_probe"]})
        async def apply():
            """通过实际入口更新模型资源, 不重建主模型"""
            if application == "full":
                return await backend.reconcile_edictum_runtime_async(config_name="assistant")
            if application == "plugins":
                return await backend.reconcile_edictum_plugins_async(config_name="assistant")
            return [item for manager in managers for item in await manager.reconcile_edictum_plugins_async(config_name="assistant")]

        result = await apply()
        assert len(result) == 3 and all(item["ok"] for item in result)
        if application == "full":
            assert all(item["action"] == "reconcile_plugins" for item in result)
        handles = []
        for manager in managers:
            for entry in manager.pool.list_entries().values():
                session = entry.session
                assert session.plugin_model_manager is models
                resources = session._probe_resources
                assert {key: value[1].model for key, value in resources._configs.items()} == {
                    "llm": "demo", "embed": "embedding-new", "rerank": "rerank-new", "asr": "asr-new",
                }
                assert resources._clients == {}
                handles.append(session.list_plugins()[-1])
        repeated = await apply()
        assert all(item["ok"] for item in repeated) and len(builds) == 3
        if application == "full":
            assert all(item["action"] == "noop" for item in repeated)
        assert [entry.session.list_plugins()[-1] for manager in managers for entry in manager.pool.list_entries().values()] == handles
        writer.set_embedding_config(EmbeddingConfig(model="embedding-latest", api_key="test", dimensions=7), "resource")
        updated = await apply()
        assert all(item["ok"] for item in updated) and len(builds) == 3
        for manager in managers:
            for entry in manager.pool.list_entries().values():
                assert entry.session._probe_resources._configs["embed"][1].model == "embedding-latest"
                assert entry.session._probe_resources._configs["embed"][1].dimensions == 7
                assert entry.session.plugin_model_manager is models
        if application != "full":
            old_handle = sessions[0].list_plugins()[-1]
            ready = asyncio.Event()
            reconcile = providers[0].reconcile_session_plugins_async

            async def observed(session, **kwargs):
                """其它实例进入真实协调后再改写来源, 明确区分排队变化与捕获重试"""
                if session is sessions[1]:
                    ready.set()
                return await reconcile(session, **kwargs)

            monkeypatch.setattr(providers[0], "reconcile_session_plugins_async", observed)
            entry = managers[0].pool.get("one-0")
            await entry.async_operation_lock.acquire()
            task = asyncio.create_task(apply())
            try:
                await asyncio.wait_for(ready.wait(), 2)
                writer.set_embedding_config(EmbeddingConfig(model="embedding-final", api_key="test"), "resource")
            finally:
                entry.async_operation_lock.release()
            stale = await asyncio.wait_for(task, 2)
            assert not stale[0]["ok"] and "等待期间已变化" in stale[0]["error"]
            assert sessions[0].list_plugins()[-1] is old_handle
            assert sessions[0].plugin_model_manager is models
            retried = await apply()
            assert all(item["ok"] for item in retried) and len(builds) == 3
            assert sessions[0]._probe_resources._configs["embed"][1].model == "embedding-final"
            writer.storage_path.write_text("broken", encoding="utf-8")
            handles = [session.list_plugins()[-1] for session in sessions]
            with pytest.raises(ValueError):
                await apply()
            assert [session.list_plugins()[-1] for session in sessions] == handles
            configs.update("assistant", {"plugins": ["session_commands"]})
            removed = await apply()
            assert all(item["ok"] for item in removed)
            assert all([handle.name for handle in session.list_plugins()] == ["session_commands"] for session in sessions)
            assert writer.storage_path.read_text(encoding="utf-8") == "broken"
    finally:
        await _close(managers)

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_plugin_reinstall_rollback_restores_installed_model_resources(tmp_path: Path, monkeypatch: Any, asynchronous: bool) -> None:
    """真正安装失败后恢复四类旧资源, 不以最新模型来源冒充旧资源版本"""
    backend, configs, _globals, managers, providers, sessions, builds = await _setup(tmp_path, monkeypatch, asynchronous)
    models = managers[0].model_config_manager
    try:
        models.set_embedding_config(EmbeddingConfig(model="embedding-old", api_key="test", dimensions=3), "resource")
        models.set_rerank_config(ReRankConfig(model="rerank-old", api_key="test", base_url="http://localhost:1"), "resource")
        models.set_asr_config(ASRConfig(model="asr-old", api_key="test"), "resource")
        plugin = tmp_path / "plugins" / "rollback_probe"
        plugin.mkdir(parents=True)
        (plugin / "meta.yaml").write_text(
            "name: rollback_probe\nversion: 0.1.0\nconfig_schema:\n"
            "  fail:\n    type: bool\n    default: false\n"
            "  llm:\n    type: llm\n    default: demo\n"
            "  embed:\n    type: embed\n    default: resource\n"
            "  rerank:\n    type: rerank\n    default: resource\n"
            "  asr:\n    type: asr\n    default: resource\n", encoding="utf-8",
        )
        (plugin / "tools.py").write_text(
            "def get_tools(session, config, *, resources):\n"
            "    session._rollback_resources = resources\n"
            "    if config['fail']:\n"
            "        raise ValueError('candidate install failed')\n"
            "    return []\n", encoding="utf-8",
        )
        configs.plugin_catalog.user_dir = plugin.parent
        configs.update("assistant", {"plugins": ["session_commands", "rollback_probe"]})
        initial = await backend.reconcile_edictum_plugins_async(config_name="assistant")
        assert all(item["ok"] for item in initial)
        old_handles = [next(handle for handle in session.list_plugins() if handle.name == "rollback_probe") for session in sessions]
        old_specs = [next(state.applied_spec for state in provider._get_runtime_state(session).plugins if state.name == "rollback_probe")
                     for provider, session in ((providers[0], sessions[0]), (providers[0], sessions[1]), (providers[1], sessions[2]))]
        old_values = {key: value[1] for key, value in sessions[0]._rollback_resources._configs.items()}
        models.update_llm_config("demo", model="llm-new")
        models.set_embedding_config(EmbeddingConfig(model="embedding-new", api_key="test", dimensions=9), "resource")
        models.set_rerank_config(ReRankConfig(model="rerank-new", api_key="test", base_url="http://localhost:2"), "resource")
        models.set_asr_config(ASRConfig(model="asr-new", api_key="test"), "resource")
        configs.update("assistant", {"plugins": ["session_commands", {"name": "rollback_probe", "config": {"fail": True}}]})
        failed = await backend.reconcile_edictum_plugins_async(config_name="assistant")
        assert len(failed) == 3 and all(not item["ok"] for item in failed)
        for index, (provider, session) in enumerate(((providers[0], sessions[0]), (providers[0], sessions[1]), (providers[1], sessions[2]))):
            result = next(item for item in failed[index]["plugins"] if item["plugin"] == "rollback_probe")
            assert result["status"] == "rolled_back"
            state = next(state for state in provider._get_runtime_state(session).plugins if state.name == "rollback_probe")
            assert state.applied_spec == old_specs[index] and state.drift
            assert state.handle is not old_handles[index]
            assert {key: value[1] for key, value in session._rollback_resources._configs.items()} == old_values
            assert session._rollback_resources._clients == {} and session.plugin_model_manager is models
        configs.update("assistant", {"plugins": ["session_commands", "rollback_probe"]})
        recovered = await backend.reconcile_edictum_plugins_async(config_name="assistant")
        assert all(item["ok"] for item in recovered)
        for session in sessions:
            assert {key: value[1].model for key, value in session._rollback_resources._configs.items()} == {
                "llm": "llm-new", "embed": "embedding-new", "rerank": "rerank-new", "asr": "asr-new",
            }
            assert session.plugin_model_manager is models
        assert len(builds) == 3
    finally:
        await _close(managers)

@pytest.mark.asyncio
async def test_model_change_during_capture_retries_all_sources(tmp_path: Path, monkeypatch: Any) -> None:
    """捕获模型中途变化时整体重读, 不把旧参数标成新来源版本"""
    backend, _configs, _globals, managers, _providers, _sessions, builds = await _setup(tmp_path, monkeypatch)
    models = managers[0].model_config_manager
    read = ModelConfigSnapshot.read
    reads = []

    def changed(manager):
        """首次真实读取后变更来源, 让捕获核验重新准备整个目标"""
        result = read(manager)
        reads.append(True)
        if len(reads) == 1:
            writer = ModelConfigManager(models.storage_path)
            writer.update_llm_config("demo", temperature=0.9)
        return result

    try:
        monkeypatch.setattr(ModelConfigSnapshot, "read", changed)
        result = await backend.reconcile_edictum_runtime_async(config_name="assistant")
        assert len(reads) == 2 and len(builds) == 6
        assert len(result) == 3 and all(item["ok"] and item["action"] == "restart" for item in result)
        repeated = await backend.reconcile_edictum_runtime_async(config_name="assistant")
        assert all(item["ok"] and item["action"] == "noop" for item in repeated) and len(builds) == 6
    finally:
        await _close(managers)

@pytest.mark.asyncio
async def test_disabling_agent_does_not_need_or_repair_model_source(tmp_path: Path, monkeypatch: Any) -> None:
    """禁用目标只卸载实例, 不为用不到的模型来源失败或回写"""
    backend, configs, _globals, managers, _providers, _sessions, builds = await _setup(tmp_path, monkeypatch)
    models = managers[0].model_config_manager
    try:
        configs.update("assistant", {"enabled": False})
        models.storage_path.write_text("broken", encoding="utf-8")
        result = await backend.reconcile_edictum_runtime_async(config_name="assistant")
        assert len(result) == 3 and all(item["ok"] and item["action"] == "unload" for item in result)
        assert len(builds) == 3 and models.storage_path.read_text(encoding="utf-8") == "broken"
    finally:
        await _close(managers)

@pytest.mark.asyncio
async def test_unload_does_not_remove_a_replaced_generation(tmp_path: Path, monkeypatch: Any) -> None:
    """卸载同步持久化期间运行代次变化, 原子移除不得卸载新代次"""
    backend, configs, _globals, managers, _providers, sessions, _builds = await _setup(tmp_path, monkeypatch)
    sync = managers[0]._sync_runtime_to_store
    replaced: list[str] = []

    def swap(sid, session):
        """真实同步后替换同一条目对象的运行代次"""
        sync(sid, session)
        if not replaced:
            current = managers[0].pool.get(sid)
            replacement = SessionEntry(session=session, session_type="assistant", created_at=0, last_used=0)
            assert managers[0].pool.replace(sid, current, replacement)
            replaced.append(replacement.instance_generation)

    try:
        handle = sessions[0].list_plugins()[0]
        configs.update("assistant", {"enabled": False})
        monkeypatch.setattr(managers[0], "_sync_runtime_to_store", swap)
        result = await backend.reconcile_edictum_runtime_async(session_refs=[{"platform_id": "one", "session_id": "one-0"}])
        assert not result[0]["ok"] and result[0]["action"] == "unload"
        current = managers[0].pool.get("one-0")
        assert current.session is sessions[0] and current.instance_generation == replaced[0]
        assert sessions[0].list_plugins()[0] is handle
    finally:
        await _close(managers)

@pytest.mark.asyncio
async def test_full_snapshot_renamed_binding_uses_new_target(tmp_path: Path, monkeypatch: Any) -> None:
    """重命名并迁移绑定后用新名称准备候选, 不从旧名称读取空默认配置"""
    backend, configs, _globals, managers, _providers, _sessions, _builds = await _setup(tmp_path, monkeypatch)
    try:
        configs.update("assistant", {}, new_name="renamed")
        backend.rename_edictum_config_references("assistant", "renamed")
        result = await backend.reconcile_edictum_runtime_async(config_name="renamed")
        assert len(result) == 3 and all(item["ok"] and item["action"] == "restart" for item in result)
        assert all(manager.provider_registry.get("edictum").plugin_config_name(entry.session) == "renamed"
                   for manager in managers for entry in manager.pool.list_entries().values())
        assert all(entry.session.list_plugins()[0].name == "session_commands"
                   for manager in managers for entry in manager.pool.list_entries().values())
    finally:
        await _close(managers)

@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["named", "instance"])
async def test_full_application_rechecks_source_and_instance_override_after_lock(tmp_path: Path, monkeypatch: Any, change: str) -> None:
    """锁内复核来源及最新实例覆盖, 不沿用排队前重启判断"""
    backend, configs, _globals, managers, _providers, sessions, builds = await _setup(tmp_path, monkeypatch)
    captured = threading.Event()
    capture = EdictumPluginApplySnapshot.capture

    def record(*args, **kwargs):
        """通知测试实际捕获已结束"""
        snapshot = capture(*args, **kwargs)
        captured.set()
        return snapshot

    try:
        configs.update("assistant", {"params": {"system_prompt": "after"}})
        if change == "binding":
            configs.create("other", {"edictum_type": "async_simple", "model_name": "demo", "plugins": ["session_commands"]})
        monkeypatch.setattr(EdictumPluginApplySnapshot, "capture", record)
        entry = managers[0].pool.get("one-0")
        await entry.async_operation_lock.acquire()
        refs = [{"platform_id": "one", "session_id": "one-0"}]
        task = asyncio.create_task(backend.reconcile_edictum_runtime_async(session_refs=refs))
        try:
            assert await asyncio.to_thread(captured.wait, 2)
            if change == "named":
                writer = EdictumConfigManager(configs.type_registry, configs.storage_path)
                writer.update("assistant", {"params": {"system_prompt": "latest"}})
            elif change == "instance":
                managers[0].update_session_config("one-0", {"system_prompt": "before"})
            else:
                managers[0].update_session_config("one-0", session_type_name="other")
        finally:
            entry.async_operation_lock.release()
        result = await asyncio.wait_for(task, 5)
        assert managers[0].pool.get("one-0").session is sessions[0] and len(builds) == 3
        if change == "named":
            assert not result[0]["ok"] and "等待期间已变化" in result[0]["error"]
            retried = await backend.reconcile_edictum_runtime_async(session_refs=refs)
            assert retried[0]["ok"] and retried[0]["action"] == "restart"
        elif change == "instance":
            assert result[0]["ok"] and result[0]["action"] == "noop"
        else:
            assert not result[0]["ok"] and "Agent 绑定" in result[0]["error"]
    finally:
        await _close(managers)

@pytest.mark.asyncio
async def test_candidate_source_change_preserves_old_session_and_context(tmp_path: Path, monkeypatch: Any) -> None:
    """候选实际插件准备后来源变化时撤销候选, 保留原上下文和插件"""
    backend, configs, _globals, managers, providers, sessions, _builds = await _setup(tmp_path, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    prepare = providers[0].prepare_session_async
    candidates: list[Any] = []

    async def paused(session, **kwargs):
        """完成真实插件安装后暂停候选提交"""
        await prepare(session, **kwargs)
        candidates.append(session)
        entered.set()
        await release.wait()

    try:
        old_handle = sessions[0].list_plugins()[0]
        await sessions[0].ctx.add_user_message("保留的原上下文")
        before = sessions[0].ctx.get_context()
        configs.update("assistant", {"params": {"system_prompt": "after"}})
        monkeypatch.setattr(providers[0], "prepare_session_async", paused)
        task = asyncio.create_task(backend.reconcile_edictum_runtime_async(session_refs=[{"platform_id": "one", "session_id": "one-0"}]))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            writer = EdictumConfigManager(configs.type_registry, configs.storage_path)
            writer.update("assistant", {"params": {"system_prompt": "latest"}})
        finally:
            release.set()
        result = await asyncio.wait_for(task, 5)
        assert not result[0]["ok"]
        assert result[0]["old_runtime_preserved"] and "候选准备期间已变化" in result[0]["error"]
        assert managers[0].pool.get("one-0").session is sessions[0]
        assert sessions[0].list_plugins()[0] is old_handle and sessions[0].ctx.get_context() == before
        assert candidates[0].list_plugins() == []
    finally:
        release.set()
        await _close(managers)

@pytest.mark.asyncio
async def test_sync_candidate_failure_cleans_plugin_and_keeps_original(tmp_path: Path, monkeypatch: Any) -> None:
    """同步候选实际安装后准备失败, 清理已装插件并保留旧实例"""
    backend, configs, _globals, managers, providers, sessions, _builds = await _setup(tmp_path, monkeypatch, False)
    candidates: list[Any] = []
    prepare = providers[0].prepare_session

    def failed(session, **kwargs):
        """完成真实插件安装后模拟初始化失败"""
        prepare(session, **kwargs)
        candidates.append(session)
        raise RuntimeError("候选准备失败")

    try:
        handle = sessions[0].list_plugins()[0]
        sessions[0].ctx.add_user_message("同步原上下文")
        before = sessions[0].ctx.get_context()
        configs.update("assistant", {"params": {"system_prompt": "after"}})
        monkeypatch.setattr(providers[0], "prepare_session", failed)
        result = await backend.reconcile_edictum_runtime_async(session_refs=[{"platform_id": "one", "session_id": "one-0"}])
        assert not result[0]["ok"] and result[0]["old_runtime_preserved"]
        assert managers[0].pool.get("one-0").session is sessions[0]
        assert sessions[0].list_plugins()[0] is handle and sessions[0].ctx.get_context() == before
        assert candidates[0].list_plugins() == [] and providers[0].get_runtime_metadata(candidates[0]) == {}
    finally:
        await _close(managers)

@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["disable", "delete", "temporary"])
async def test_full_snapshot_handles_disabled_deleted_and_temporary_targets(tmp_path: Path, monkeypatch: Any, operation: str) -> None:
    """禁用或删除目标只能卸载, 临时目标真正进入候选但不改冷配置"""
    backend, configs, _globals, managers, _providers, _sessions, builds = await _setup(tmp_path, monkeypatch)
    try:
        target = configs.get_config("assistant")
        if target is None:
            raise AssertionError("测试配置未创建")
        if operation == "disable":
            configs.update("assistant", {"enabled": False})
        elif operation == "delete":
            configs.delete("assistant")
        else:
            target["params"]["system_prompt"] = "temporary"
        result = await backend.reconcile_edictum_runtime_async(config_name="assistant", desired_config=target if operation == "temporary" else None)
        assert len(result) == 3 and all(item["ok"] for item in result)
        if operation == "temporary":
            assert [item["action"] for item in result] == ["restart", "noop", "restart"]
            assert len(builds) == 5
            persisted = configs.get_config("assistant")
            assert persisted is not None and persisted["params"]["system_prompt"] == "before"
            assert managers[0].get_session_runtime_metadata("one-0")["config"]["applied"]["params"]["system_prompt"] == "temporary"
        else:
            assert all(item["action"] == "unload" for item in result) and len(builds) == 3
            assert all(manager.pool.list_entries() == {} for manager in managers)
    finally:
        await _close(managers)

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("change", ["binding", "generation"])
async def test_plugin_application_keeps_frozen_target_and_rechecks_generation(tmp_path: Path, monkeypatch: Any, asynchronous: bool, change: str) -> None:
    """仅插件应用同样复核冻结绑定和运行代次, 来源核验让出循环后不能绕过"""
    backend, configs, globals_, managers, _providers, sessions, builds = await _setup(tmp_path, monkeypatch, asynchronous)
    captured = threading.Event()
    capture = EdictumPluginApplySnapshot.capture

    def record(*args, **kwargs):
        """通知真实来源准备完成"""
        snapshot = capture(*args, **kwargs)
        captured.set()
        return snapshot

    try:
        plugin = configs.plugin_catalog.get("session_commands")
        if plugin is None:
            raise AssertionError("测试插件不存在")
        globals_.save_global("session_commands", plugin.config_schema, {"about_text": "must not apply"})
        configs.create("other", {"edictum_type": "async_simple" if asynchronous else "simple", "model_name": "demo", "plugins": ["session_commands"]})
        monkeypatch.setattr(EdictumPluginApplySnapshot, "capture", record)
        old_handle = sessions[0].list_plugins()[0]
        entry = managers[0].pool.get("one-0")
        await entry.async_operation_lock.acquire()
        task = asyncio.create_task(backend.reconcile_edictum_plugins_async(
            config_name="assistant", session_refs=[{"platform_id": "one", "session_id": "one-0"}],
        ))
        try:
            assert await asyncio.to_thread(captured.wait, 2)
            if change == "binding":
                managers[0].update_session_config("one-0", session_type_name="other")
            elif change == "generation":
                replacement = SessionEntry(session=sessions[0], session_type="assistant", created_at=0, last_used=0)
                assert managers[0].pool.replace("one-0", entry, replacement)
        finally:
            entry.async_operation_lock.release()
        result = await asyncio.wait_for(task, 5)
        assert len(result) == 1 and not result[0]["ok"]
        assert "Agent 绑定" in result[0]["error"] if change == "binding" else "运行时已变化" in result[0]["error"]
        assert managers[0].pool.get("one-0").session is sessions[0]
        assert sessions[0].list_plugins()[0] is old_handle and len(builds) == 3
        about = await sessions[0].run("/about") if asynchronous else sessions[0].run("/about")
        assert about != "must not apply"
    finally:
        await _close(managers)
