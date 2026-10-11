"""跨平台共享应用来源, 实例覆盖与排队失效的实际插件生命周期验收"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from satrap.core.backend.BackendManager import BackendManager
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.framework.SessionManager import SessionEntry, SessionManager
from satrap.core.framework.providers.edictum import EdictumProvider
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.type import SessionConfig
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.plugin_apply import EdictumPluginApplySnapshot
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.edictum.registry import create_default_edictum_type_registry


async def _platforms(tmp_path: Path, monkeypatch: Any) -> tuple[Any, EdictumConfigManager, PluginConfigManager, list[Any], list[EdictumProvider]]:
    """
    创建两个隔离平台及三个实际 Edictum 实例, 不连接模型或适配器

    参数:
    - tmp_path: 临时存储根目录
    - monkeypatch: 全局配置来源替换夹具

    返回:
    - 后端, 命名配置, 全局参数, 实例及 Provider
    """
    globals_ = PluginConfigManager(tmp_path / "globals")
    monkeypatch.setattr("satrap.edictum.plugin_apply.PluginConfigManager", lambda: globals_)
    monkeypatch.setattr("satrap.edictum.plugin_settings.PluginConfigManager", lambda: globals_)
    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / "named.json")
    for name in ("agent-a", "agent-b"):
        configs.create(name, {"edictum_type": "async_simple", "model_name": "demo", "plugins": ["session_commands"]})
    backend = cast(Any, BackendManager.__new__(BackendManager))
    backend._edictum_cfg = configs
    backend._platform_runtimes = {}
    sessions: list[Any] = []
    providers: list[EdictumProvider] = []
    for platform_id, names in (("one", ["agent-a", "agent-a"]), ("two", ["agent-b"])):
        provider = EdictumProvider(configs, registry, default_checkpoint_db=str(tmp_path / f"{platform_id}-context.db"))
        providers.append(provider)
        entries = {}
        records = {}
        for index, name in enumerate(names):
            sid = f"{platform_id}-{index}"
            record = SessionConfig(session_id=sid, session_type_name=name, provider_name="edictum")
            session = provider.create_session(record, llm=cast(Any, object()))
            setattr(session, "_satrap_provider_name", "edictum")
            setattr(session, "plugin_environment", PluginEnvironment("platform", "onebot"))
            setattr(session, "plugin_override_store", SessionOverrideStore(tmp_path / f"{sid}.db"))
            await provider.prepare_session_async(session)
            entries[sid] = SessionEntry(session=session, session_type=name, created_at=0, last_used=0)
            records[sid] = record
            sessions.append(session)
        manager = cast(Any, SessionManager.__new__(SessionManager))
        manager._model_cfg_mgr = None
        manager.provider_registry = SimpleNamespace(get=lambda _name, provider=provider: provider)
        manager.pool = SimpleNamespace(list_entries=lambda entries=entries: entries)
        manager.store = SimpleNamespace(get=lambda sid, records=records: records.get(sid))
        manager.platform_id = platform_id
        backend._platform_runtimes[platform_id] = (manager, None)
    return backend, configs, globals_, sessions, providers


async def _release(sessions: list[Any], providers: list[EdictumProvider]) -> None:
    """
    卸载测试实例插件

    参数:
    - sessions: 三个实际会话
    - providers: 两个平台 Provider
    """
    for session, provider in zip(sessions, [providers[0], providers[0], providers[1]]):
        await provider.release_session_async(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["capture", "lock", "source_check"])
@pytest.mark.parametrize("removed", [False, True])
async def test_plugin_batch_rejects_platform_manager_replacement(tmp_path: Path, monkeypatch: Any, phase: str, removed: bool) -> None:
    """来源捕获/等待锁/线程核验期间所属平台被替换或移除, 旧管理器不执行插件生命周期"""
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
            if removed:
                backend._platform_runtimes.pop("one")
            else:
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
    """
    同次跨平台刷新命名配置及插件参数各一次, 覆盖不变时不重装

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 来源计数夹具
    """
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
            """
            计数实际全局 JSON 解析

            参数:
            - name: 插件名称
            - schema: 字段声明

            返回:
            - 原全局显式参数
            """
            parsed.append(name)
            return read(name, schema)

        def refreshed(**kwargs):
            """
            计数实际命名文件加载

            参数:
            - kwargs: 原重载选项
            """
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
    """
    等待锁期间命名或全局来源变化, 不使用旧目标且重试加载最新内容

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 来源捕获通知夹具
    - source: 全局参数或命名配置
    """
    backend, configs, globals_, sessions, providers = await _platforms(tmp_path, monkeypatch)
    captured = threading.Event()
    capture = EdictumPluginApplySnapshot.capture

    def record(*args, **kwargs):
        """
        通知测试真实捕获已完成

        参数:
        - args: 原捕获位置参数
        - kwargs: 原捕获范围参数

        返回:
        - 真实来源快照
        """
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
    """
    移除旧插件仍卸载, 不引用目标的配置不读取损坏全局参数

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 参数来源替换夹具
    """
    backend, configs, globals_, sessions, providers = await _platforms(tmp_path, monkeypatch)
    try:
        configs.update("agent-b", {"plugins": ["group_chat"]})
        await backend.reconcile_edictum_plugins_async(config_name="agent-b")
        configs.update("agent-a", {"plugins": []})
        globals_._dir.mkdir(parents=True, exist_ok=True)
        globals_._global_path("group_chat").write_text("broken", encoding="utf-8")
        result = await backend.reconcile_edictum_plugins_async(plugin_name="session_commands")
        assert len(result) == 2 and all(item["ok"] for item in result)
        assert all(item["plugins"][0]["action"] == "remove" for item in result)
        assert sessions[0].list_plugins() == [] and sessions[1].list_plugins() == []
        assert sessions[2].list_plugins()[0].name == "group_chat"
    finally:
        await _release(sessions, providers)


@pytest.mark.asyncio
async def test_new_reference_is_selected_from_independent_cold_write(tmp_path: Path, monkeypatch: Any) -> None:
    """
    范围在刷新后的新引用中判断, 后端旧内存配置不能遗漏新添加插件

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 参数来源替换夹具
    """
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
    """
    命名文件在刷新后变化时重新捕获全部范围, 不把旧内容与新版本配对

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 命名文件变化夹具
    """
    backend, configs, _globals, sessions, providers = await _platforms(tmp_path, monkeypatch)
    reload = configs.reload
    external = EdictumConfigManager(configs.type_registry, configs.storage_path)
    calls: list[int] = []

    def changing(**kwargs):
        """
        首次刷新结束后修改独立来源

        参数:
        - kwargs: 原重载选项
        """
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
    """
    损坏命名配置或已引用全局参数时不安装默认参数, 保留原句柄

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 参数来源替换夹具
    - source: 损坏来源类别
    """
    backend, configs, globals_, sessions, providers = await _platforms(tmp_path, monkeypatch)
    try:
        old = [session.list_plugins()[0] for session in sessions]
        path = configs.storage_path if source == "named" else globals_._global_path("session_commands")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("broken", encoding="utf-8")
        with pytest.raises(ValueError):
            await backend.reconcile_edictum_plugins_async(plugin_name="session_commands")
        assert all(session.list_plugins()[0] is handle for session, handle in zip(sessions, old))
    finally:
        await _release(sessions, providers)
