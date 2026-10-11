"""完整 Agent 应用的固定来源, 操作锁复核及候选回滚验收"""
from __future__ import annotations

import asyncio
import importlib
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from satrap.core.backend.BackendManager import BackendManager
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.framework.SessionManager import SessionEntry, SessionManager
from satrap.core.framework.providers.edictum import EdictumProvider
from satrap.core.storage import StorageLayout
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.plugin_apply import EdictumPluginApplySnapshot
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.edictum.registry import create_default_edictum_type_registry


async def _setup(tmp_path: Path, monkeypatch: Any, asynchronous: bool = True) -> tuple[Any, Any, Any, list[Any], list[Any], list[Any], list[Any]]:
    """
    两个实际平台管理器及三个 Simple 会话, 所有存储隔离且模型不联网

    参数:
    - tmp_path: 临时根目录
    - monkeypatch: 模型和参数来源替换夹具
    - asynchronous: 使用同步或异步 Simple 类型

    返回:
    - 后端, 命名配置, 参数, 管理器, Provider, 会话及构建记录
    """
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
        """
        记录真实构建入口, 返回无需联网的占位模型

        参数:
        - args: 模型配置
        - kwargs: 原构建选项

        返回:
        - 独立模型对象
        """
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
    """
    卸载所有仍活跃的隔离实例

    参数:
    - managers: 测试平台管理器
    """
    for manager in managers:
        for sid in list(manager.pool.list_entries()):
            await manager.unload_session_async(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_full_batch_uses_one_source_and_preserves_effective_override(tmp_path: Path, monkeypatch: Any, asynchronous: bool) -> None:
    """
    完整重建候选共享全局参数及命名文件, 有效目标不变实例不重建

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 来源计数夹具
    - asynchronous: 同步或异步实例
    """
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
            """
            记录实际命名文件刷新

            参数:
            - kwargs: 原读取选项
            """
            refreshed.append(threading.get_ident())
            reload(**kwargs)

        def counted_load(name, schema):
            """
            记录实际全局参数解析

            参数:
            - name: 插件名称
            - schema: 字段声明

            返回:
            - 全局显式参数
            """
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
async def test_unload_does_not_remove_a_replaced_generation(tmp_path: Path, monkeypatch: Any) -> None:
    """
    卸载同步持久化期间运行代次变化, 原子移除不得卸载新代次

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 池替换竞争夹具
    """
    backend, configs, _globals, managers, _providers, sessions, _builds = await _setup(tmp_path, monkeypatch)
    sync = managers[0]._sync_runtime_to_store
    replaced: list[str] = []

    def swap(sid, session):
        """
        真实同步后替换同一条目对象的运行代次

        参数:
        - sid: 实例 ID
        - session: 原会话
        """
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
    """
    重命名并迁移绑定后用新名称准备候选, 不从旧名称读取空默认配置

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 模型与参数来源替换夹具
    """
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
@pytest.mark.parametrize("change", ["named", "instance", "binding"])
async def test_full_application_rechecks_source_and_instance_override_after_lock(tmp_path: Path, monkeypatch: Any, change: str) -> None:
    """
    锁内复核来源及最新实例覆盖, 不沿用排队前重启判断

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 来源捕获通知夹具
    - change: 命名来源, 实例覆盖或绑定变化
    """
    backend, configs, _globals, managers, _providers, sessions, builds = await _setup(tmp_path, monkeypatch)
    captured = threading.Event()
    capture = EdictumPluginApplySnapshot.capture

    def record(*args, **kwargs):
        """
        通知测试实际捕获已结束

        参数:
        - args: 原捕获位置参数
        - kwargs: 原捕获选项

        返回:
        - 已捕获来源
        """
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
@pytest.mark.parametrize("change", ["source", "generation"])
async def test_candidate_source_change_preserves_old_session_and_context(tmp_path: Path, monkeypatch: Any, change: str) -> None:
    """
    候选实际插件准备后来源变化时撤销候选, 保留原上下文和插件

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 候选准备暂停夹具
    - change: 命名来源或同一池条目运行代次变化
    """
    backend, configs, _globals, managers, providers, sessions, _builds = await _setup(tmp_path, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    prepare = providers[0].prepare_session_async
    candidates: list[Any] = []

    async def paused(session, **kwargs):
        """
        完成真实插件安装后暂停候选提交

        参数:
        - session: 候选实例
        - kwargs: 固定应用来源
        """
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
            if change == "source":
                writer = EdictumConfigManager(configs.type_registry, configs.storage_path)
                writer.update("assistant", {"params": {"system_prompt": "latest"}})
            else:
                current = managers[0].pool.get("one-0")
                replacement = SessionEntry(session=sessions[0], session_type="assistant", created_at=0, last_used=0)
                assert managers[0].pool.replace("one-0", current, replacement)
        finally:
            release.set()
        result = await asyncio.wait_for(task, 5)
        assert not result[0]["ok"]
        if change == "source":
            assert result[0]["old_runtime_preserved"] and "候选准备期间已变化" in result[0]["error"]
        else:
            assert result[0]["runtime_changed"] and "会话运行时已变化" in result[0]["error"]
        assert managers[0].pool.get("one-0").session is sessions[0]
        assert sessions[0].list_plugins()[0] is old_handle and sessions[0].ctx.get_context() == before
        assert candidates[0].list_plugins() == []
    finally:
        release.set()
        await _close(managers)


@pytest.mark.asyncio
async def test_sync_candidate_failure_cleans_plugin_and_keeps_original(tmp_path: Path, monkeypatch: Any) -> None:
    """
    同步候选实际安装后准备失败, 清理已装插件并保留旧实例

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 同步候选准备失败夹具
    """
    backend, configs, _globals, managers, providers, sessions, _builds = await _setup(tmp_path, monkeypatch, False)
    candidates: list[Any] = []
    prepare = providers[0].prepare_session

    def failed(session, **kwargs):
        """
        完成真实插件安装后模拟初始化失败

        参数:
        - session: 候选会话
        - kwargs: 固定应用来源
        """
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
    """
    禁用或删除目标只能卸载, 临时目标真正进入候选但不改冷配置

    参数:
    - tmp_path: 隔离目录
    - monkeypatch: 模型与参数来源替换夹具
    - operation: 禁用, 删除或临时目标应用
    """
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
