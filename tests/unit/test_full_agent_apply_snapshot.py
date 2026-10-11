"""完整 Agent 应用的固定来源, 操作锁复核及候选回滚验收"""
from __future__ import annotations

import asyncio
import importlib
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from satrap.core.backend.BackendManager import BackendManager
from satrap.core.config.model_snapshot import ModelConfigSnapshot
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.framework.SessionManager import SessionEntry, SessionManager
from satrap.core.framework.providers.edictum import EdictumProvider
from satrap.core.storage import StorageLayout
from satrap.core.type import ASRConfig, EmbeddingConfig, ReRankConfig
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
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_full_application_uses_fresh_shared_models_and_skips_independent_model(tmp_path: Path, monkeypatch: Any, asynchronous: bool) -> None:
    """
    同名模型参数变化进入重建判断, 同来源跨平台解析一次且不同模型实例不重建

    参数:
    - tmp_path: 隔离来源
    - monkeypatch: 来源和真实构建入口计数
    - asynchronous: 同步或异步会话
    """
    backend, _configs, _globals, managers, _providers, sessions, builds = await _setup(tmp_path, monkeypatch, asynchronous)
    models = managers[0].model_config_manager
    read = ModelConfigSnapshot.read
    source_reads = []
    received = []
    module = importlib.import_module("satrap.core.framework.SessionManager")
    build = module.build_llm_from_config

    def counted_read(manager):
        """
        记录实际文件解析

        参数:
        - manager: 模型来源

        返回:
        - 只读捕获结果
        """
        source_reads.append(manager.storage_path)
        return read(manager)

    def counted_build(config, **kwargs):
        """
        记录真正用于构建客户端的独立模型参数

        参数:
        - config: 本次模型参数
        - kwargs: 同步或异步选项

        返回:
        - 原模型占位对象
        """
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
        assert models.get_llm_config("demo").model == "demo"
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
@pytest.mark.parametrize("change", ["queued", "candidate", "corrupt"])
async def test_full_model_source_change_or_corruption_preserves_runtime(tmp_path: Path, monkeypatch: Any, change: str) -> None:
    """
    模型来源排队或准备期间变化以及损坏都不能提交旧候选, 保留上下文和插件

    参数:
    - tmp_path: 隔离来源
    - monkeypatch: 来源捕获与候选准备暂停
    - change: 排队变化, 候选变化或文件损坏
    """
    backend, configs, _globals, managers, providers, sessions, builds = await _setup(tmp_path, monkeypatch)
    models = managers[0].model_config_manager
    captured = threading.Event()
    entered = asyncio.Event()
    release = asyncio.Event()
    capture = EdictumPluginApplySnapshot.capture
    prepare = providers[0].prepare_session_async
    candidates = []

    def record(*args, **kwargs):
        """
        通知真实完整捕获完成

        参数:
        - args: 命名来源与范围
        - kwargs: 模型等额外来源

        返回:
        - 同代应用快照
        """
        snapshot = capture(*args, **kwargs)
        captured.set()
        return snapshot

    async def paused(session, **kwargs):
        """
        完成真实候选插件准备后暂停

        参数:
        - session: 候选实例
        - kwargs: 固定来源
        """
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
async def test_full_application_plugin_resources_use_captured_model_source(tmp_path: Path, monkeypatch: Any, asynchronous: bool) -> None:
    """
    实际插件安装读取同代四类模型资源, 运行期仍恢复原管理器且重试无重复安装

    参数:
    - tmp_path: 隔离来源与插件目录
    - monkeypatch: 真实实例夹具
    - asynchronous: 同步或异步实例
    """
    backend, configs, _globals, managers, _providers, _sessions, builds = await _setup(tmp_path, monkeypatch, asynchronous)
    models = managers[0].model_config_manager
    try:
        writer = ModelConfigManager(models.storage_path)
        writer.set_embedding_config(EmbeddingConfig(model="embedding-new", api_key="test"), "resource")
        writer.set_rerank_config(ReRankConfig(model="rerank-new", api_key="test", base_url="http://localhost:1"), "resource")
        writer.set_asr_config(ASRConfig(model="asr-new", api_key="test"), "resource")
        assert not models.has_config("embedding", "resource")
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
        result = await backend.reconcile_edictum_runtime_async(config_name="assistant")
        assert len(result) == 3 and all(item["ok"] and item["action"] == "reconcile_plugins" for item in result)
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
        repeated = await backend.reconcile_edictum_runtime_async(config_name="assistant")
        assert all(item["ok"] and item["action"] == "noop" for item in repeated) and len(builds) == 3
        assert [entry.session.list_plugins()[-1] for manager in managers for entry in manager.pool.list_entries().values()] == handles
    finally:
        await _close(managers)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_plugin_reinstall_rollback_restores_installed_model_resources(tmp_path: Path, monkeypatch: Any, asynchronous: bool) -> None:
    """
    真正安装失败后恢复四类旧资源, 不以最新模型来源冒充旧资源版本

    参数:
    - tmp_path: 隔离配置与插件目录
    - monkeypatch: 真实实例夹具
    - asynchronous: 同步或异步插件生命周期
    """
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
    """
    捕获模型中途变化时整体重读, 不把旧参数标成新来源版本

    参数:
    - tmp_path: 隔离来源
    - monkeypatch: 实际模型读取后写入夹具
    """
    backend, _configs, _globals, managers, _providers, _sessions, builds = await _setup(tmp_path, monkeypatch)
    models = managers[0].model_config_manager
    read = ModelConfigSnapshot.read
    reads = []

    def changed(manager):
        """
        首次真实读取后变更来源, 让捕获核验重新准备整个目标

        参数:
        - manager: 当前模型来源

        返回:
        - 变更前捕获结果
        """
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
    """
    禁用目标只卸载实例, 不为用不到的模型来源失败或回写

    参数:
    - tmp_path: 隔离来源
    - monkeypatch: 实际实例夹具
    """
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


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("change", ["binding", "generation", "source_check"])
async def test_plugin_application_keeps_frozen_target_and_rechecks_generation(tmp_path: Path, monkeypatch: Any, asynchronous: bool, change: str) -> None:
    """
    仅插件应用同样复核冻结绑定和运行代次, 来源核验让出循环后不能绕过

    参数:
    - tmp_path: 隔离存储
    - monkeypatch: 来源捕获通知与核验竞争夹具
    - asynchronous: 同步或异步会话
    - change: 排队改绑, 排队替换或来源核验期间替换
    """
    backend, configs, globals_, managers, _providers, sessions, builds = await _setup(tmp_path, monkeypatch, asynchronous)
    captured = threading.Event()
    capture = EdictumPluginApplySnapshot.capture

    def record(*args, **kwargs):
        """
        通知真实来源准备完成, 可在核验工作线程模拟运行代次变化

        参数:
        - args: 捕获范围
        - kwargs: 来源选项

        返回:
        - 真实来源快照
        """
        snapshot = capture(*args, **kwargs)
        if change == "source_check":
            current = snapshot.sources.is_current

            def replace_during_check():
                """真实来源核验成功后替换同一条目对象的代次"""
                valid = current()
                old = managers[0].pool.get("one-0")
                replacement = SessionEntry(session=sessions[0], session_type="assistant", created_at=0, last_used=0)
                if old is None:
                    raise AssertionError("测试原实例不存在")
                if not managers[0].pool.replace("one-0", old, replacement):
                    raise AssertionError("测试未替换运行代次")
                return valid

            monkeypatch.setattr(snapshot.sources, "is_current", replace_during_check)
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
