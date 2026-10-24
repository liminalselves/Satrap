"""业务链路场景与集中边界契约"""
from __future__ import annotations

from unittest.mock import MagicMock
from pathlib import Path
import pytest
from typing import Any
import time
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionManager as _session_manager_config_SessionManager
from satrap.core.framework.Base import Session as _session_manager_config_Session
from satrap.core.type import LLMConfig
from satrap.edictum.plugin_compatibility import PluginEnvironment
import importlib
import sqlite3
from typing import cast
import yaml
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.framework.SessionManager import SessionConfigStore
from satrap.core.framework.SessionManager import SessionManager as _session_providers_SessionManager, SessionRegistry
from satrap.core.framework.providers import BindingState, BindingStatus, EdictumProvider, SessionClassProvider
from satrap.core.framework.providers import SessionProviderDefinition, SessionProviderRegistry
from satrap.edictum.simple_session import AsyncSimpleSession, SimpleSession
from satrap.edictum.plugin_config import PluginConfigManager, parse_config_schema
from satrap.core.APICall.LLMCall import LLM
from satrap.core.framework.Base import AsyncSession as _session_providers_AsyncSession
from satrap.core.framework.Base import Session as _session_providers_Session
from satrap.edictum.registry import EdictumTypeDefinition, EdictumTypeRegistry, create_default_edictum_type_registry
from satrap.edictum.config import EdictumConfigManager
from satrap.core.storage import StorageLayout
from satrap.core.type import CommandAction, SessionConfig as _session_providers_SessionConfig, UserCall, ReRankConfig
from satrap.core.framework.session_discovery import SessionClassDiscoveryService, create_default_session_dir
from satrap.core.framework.session_discovery import discover_session_classes
from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.config.loader import ConfigLoader
from concurrent.futures import ThreadPoolExecutor
import threading
from satrap.core.config.session_overrides import OverrideConflictError
from satrap.edictum.plugin_settings import PluginSettingsService
from satrap.core.storage.database import delete_session_domain_rows, restore_session_domain, snapshot_session_domain
from satrap.edictum.plugin_config import ConfigField
from satrap.core.storage import StorageMaintenanceService, maintenance as module
import asyncio
from satrap.core.framework import AsyncSession as _multisession_AsyncSession, Session as _multisession_Session
from satrap.core.framework import SessionManager as _multisession_SessionManager

class _EchoSession(_session_manager_config_Session):
    """测试用最小会话子类"""

    def __init__(self, session_id: str, **kw: Any):
        super().__init__(session_id, **kw)

def _make_sm(tmp_path: Path) -> _session_manager_config_SessionManager:
    return _session_manager_config_SessionManager(
        default_session_type="echo",
        default_session_class=_EchoSession,
        db_path=tmp_path / "session_config.db",
    )

def test_class_config_crud_and_persistence(tmp_path: Path):
    """类级配置: 注册可列出, 参数写入 storage_path 可重建, remove_config 后消失"""
    storage = tmp_path / "classes.json"
    mgr = SessionClassConfigManager(storage_path=storage)
    mgr.register("dummy", _EchoSession)
    assert list(mgr.list_configs().keys()) == ["dummy"]
    assert mgr.get_class("dummy") is _EchoSession
    assert isinstance(mgr.get_params("dummy"), dict)
    assert mgr.has_config("dummy") is True

    mgr.set_config("dummy", {"model": "m1"})
    mgr2 = SessionClassConfigManager(storage_path=storage)
    assert list(mgr2.list_configs().keys()) == ["dummy"]
    assert mgr2.get_params("dummy") == {"model": "m1"}

    assert mgr.remove_config("dummy") is True
    assert mgr.has_config("dummy") is False

def test_session_config_register_update_and_list(tmp_path: Path):
    """实例配置: 注册落库, 更新/缺失语义, 列表按最后使用倒序, 类型注册表可列出"""
    sm = _make_sm(tmp_path)
    cfg = sm.register_session(_EchoSession, "echo", session_config={"model_name": "m1"}, session_id="s-1")
    assert cfg.session_id == "s-1"
    assert cfg.session_type_name == "echo"
    assert cfg.session_config == {"model_name": "m1"}
    assert sm.get_session_config("s-1") is not None
    assert sm.get_session_config("nope") is None

    updated = sm.update_session_config("s-1", session_config={"a": 2})
    assert updated is not None
    assert updated.session_config == {"a": 2}
    assert sm.update_session_config("missing", session_config={"a": 3}) is None

    sm.store.update_runtime_fields("s-1", last_used_at=time.time() - 1000, message_count=0)
    sm.register_session(_EchoSession, "echo", session_id="s-new")
    configs = sm.list_session_configs()
    assert [c.session_id for c in configs] == ["s-new", "s-1"]
    assert len(sm.list_session_configs(limit=1)) == 1

    sm.register_session_type("extra", _EchoSession)
    types = sm.list_registered_session_types()
    assert "echo" in types
    assert "extra" in types


@pytest.mark.parametrize("async_entry", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("failure", ["initialization", "route_changed"])
async def test_new_failure_keeps_original_route_and_never_publishes_reset(tmp_path, monkeypatch, async_entry, failure):
    """目标初始化失败或并发路由变化时, 不发布成功回执或清窗信号"""
    from satrap.core.conversation import ConversationRoute
    from satrap.core.framework.UserManager import UserManager
    from satrap.core.type import CommandAction, UserCall
    sm = _make_sm(tmp_path)
    users = UserManager(sm, db_path=tmp_path / "users.db")
    sm._user_mgr = users
    route = ConversationRoute("user", "test", "echo", scope="group", self_id="bot", group_id="group")
    initial = users.resolve_session("user", "test", "echo", route=route)
    target = initial + ":new"
    other = initial + ":concurrent"
    monkeypatch.setattr(sm, "_invoke_sync_session", lambda *args: CommandAction("new_session", target, "不应出现的成功回执"))
    prepare_sync, prepare_async = sm._prepare_session_sync, sm._prepare_session_async

    def check(instance):
        if instance.session_id == target:
            if failure == "initialization":
                raise ValueError("模拟初始化失败")
            sm._resolve_or_create_session_config(UserCall(session_id=other, session_type="echo"))
            users.store.upsert_context_session(route.owner, route.platform, route.session_type, other, route.provider, context_key=route.key)

    def prepare(instance, **kwargs):
        check(instance)
        return prepare_sync(instance, **kwargs)

    async def prepare_later(instance, **kwargs):
        check(instance)
        return await prepare_async(instance, **kwargs)

    monkeypatch.setattr(sm, "_prepare_session_sync", prepare)
    monkeypatch.setattr(sm, "_prepare_session_async", prepare_later)
    call = UserCall(session_id=initial, session_type="echo", route=route, message="/new")
    result = await sm.handle_call_async(call) if async_entry else sm.handle_call(call)
    assert "切换失败" in result and "不应出现" not in result
    assert call.context_reset_at is None and call.session_id == initial
    assert users.resolve_session("user", "test", "echo", route=route) == (initial if failure == "initialization" else other)
    assert target not in users.store.list_context_history(route)
    assert sm.store.get(target) is None
    assert target not in sm.pool.list_entries()


def test_register_session_from_class_config(tmp_path: Path):
    """从类级配置派生实例配置 (extra_params 覆盖)"""
    sm = _make_sm(tmp_path)
    mgr = SessionClassConfigManager(storage_path=tmp_path / "classes.json")
    mgr.register("dummy", _EchoSession)
    mgr.set_config("dummy", {"model": "m1", "k": "v"})
    cfg = sm.register_session_from_class_config(
        "dummy", mgr, session_id="s-cls", extra_params={"k": "override"}
    )
    assert cfg.session_type_name == "dummy"
    assert cfg.session_config["model"] == "m1"
    assert cfg.session_config["k"] == "override"

def _register_active_session(sm: _session_manager_config_SessionManager, session_id: str) -> None:
    """注册一个会话并放入活跃池"""
    cfg = sm.register_session(_EchoSession, "echo", session_id=session_id)
    session = _EchoSession(session_id)
    sm.pool.put(session_id, session, session_type=cfg.session_type_name or "echo")

def test_remove_session_keeps_config_by_default(tmp_path: Path):
    """remove_session 默认仅移除内存活跃实例, 保留 SQL 配置"""
    sm = _make_sm(tmp_path)
    _register_active_session(sm, "s-1")
    assert len(sm.list_sessions()) == 1
    sm.remove_session("s-1")
    assert len(sm.list_sessions()) == 0
    assert sm.get_session_config("s-1") is not None   # 配置保留

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_remove_session_with_config_deletes_both(tmp_path: Path, asynchronous: bool):
    """remove_session(remove_config=True) 同时删除内存实例, SQL 配置与沙箱目录 (同步与异步镜像)"""
    sm = _make_sm(tmp_path)
    _register_active_session(sm, "s-1")
    sandbox_path = sm.storage_layout.session_sandbox("local", "s-1")
    sandbox_path.mkdir(parents=True)
    (sandbox_path / "result.txt").write_text("data", encoding="utf-8")

    if asynchronous:
        await sm.remove_session_async("s-1", remove_config=True)
    else:
        sm.remove_session("s-1", remove_config=True)

    assert sm.get_session_config("s-1") is None
    assert not sandbox_path.exists()
    assert any((sm.storage_layout.trash_root("local") / "sessions").iterdir())

def test_remove_session_missing_id_no_error(tmp_path: Path):
    """移除不存在的会话不报错"""
    sm = _make_sm(tmp_path)
    sm.remove_session("missing")
    sm.remove_session("missing", remove_config=True)

def test_reload_model_configs_skips_without_manager(tmp_path: Path):
    """无 ModelConfigManager 时跳过, 不报错"""
    sm = _make_sm(tmp_path)
    sm.reload_model_configs()

def test_reload_model_configs_refreshes_active_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """有 manager 时刷新活跃会话的 LLM 实例"""
    sm = _make_sm(tmp_path)
    _register_active_session(sm, "s-1")

    fake_session = MagicMock()
    fake_entry = sm.pool.get("s-1")
    assert fake_entry is not None
    fake_entry.session = fake_session

    mgr = MagicMock()
    mgr.get_llm_config.return_value = LLMConfig(
        api_key="key", base_url="https://x", model="m",
        temperature=0.5, max_tokens=1024,
    )
    sm._model_cfg_mgr = mgr
    sm.reload_model_configs()
    fake_session.reload_llm.assert_called_once()
    fake_session.apply_context_config.assert_called_once_with(mgr.get_llm_config.return_value)

def _placeholder_llm() -> LLM:
    """占位 LLM: 被测 Provider 不触发真实模型调用, cast 集中在此工厂"""
    return cast(LLM, object())

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_provider_refreshes_inherited_plugin_values_and_model_references(tmp_path, monkeypatch, asynchronous):
    """同步与异步入口读取最新覆盖, 下层配置更新和模型引用变更按需重装"""
    layout = StorageLayout(tmp_path / "data")
    models = ModelConfigManager(tmp_path / "models.json")
    models.set_rerank_config(ReRankConfig(model="rank", api_key="first-key", base_url="http://localhost:1234"), "rank")
    meta = yaml.safe_load(Path("satrap/expend/plugins/rag/meta.yaml").read_text(encoding="utf-8"))
    assert isinstance(meta, dict)
    schema = parse_config_schema(meta)
    global_config = PluginConfigManager(tmp_path / "plugin-config")
    global_config.save_global("rag", schema, {"candidate_k": 21, "top_k": 4})
    monkeypatch.setattr("satrap.edictum.plugin_settings.PluginConfigManager", lambda: global_config)
    monkeypatch.setattr("satrap.edictum.simple_session.PluginConfigManager", lambda: global_config)
    registry = create_default_edictum_type_registry()
    manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    manager.create("assistant", {"edictum_type": "async_simple" if asynchronous else "simple", "plugins": [{"name": "rag", "config": {"top_k": 7, "rerank": "rank"}}]})
    provider = EdictumProvider(manager, registry, default_checkpoint_db=str(layout.platform_db("local")))
    session = provider.create_session(_session_providers_SessionConfig(session_id="one", session_type_name="assistant", provider_name="edictum"), llm=_placeholder_llm())
    assert isinstance(session, (SimpleSession, AsyncSimpleSession))
    session.storage_layout, session.storage_platform_id, session.plugin_model_manager = layout, "local", models
    session.plugin_override_store = SessionOverrideStore(layout.platform_db("local"))
    session.plugin_override_store.replace("one", "plugins.rag", {"top_k": 3}, expected_revision=0)
    async def prepare():
        if asynchronous:
            await provider.prepare_session_async(session)
        else:
            provider.prepare_session(session)
        return next(item for item in session.list_plugins() if item.name == "rag")
    try:
        plugin = await prepare()
        assert session._wf is not None
        tool = session._wf.tools_manager.tools["rag_search"]
        config = getattr(tool, "config")
        assert isinstance(config, dict)
        assert config["top_k"] == 3 and config["candidate_k"] == 21
        assert await prepare() is plugin
        session.plugin_override_store.replace("one", "plugins.rag", {}, expected_revision=1)
        plugin = await prepare()
        assert getattr(session._wf.tools_manager.tools["rag_search"], "config")["top_k"] == 7
        other_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
        other_manager.update("assistant", {"plugins": [{"name": "rag", "config": {"top_k": 8, "rerank": "rank"}}]})
        plugin = await prepare()
        assert getattr(session._wf.tools_manager.tools["rag_search"], "config")["top_k"] == 8
        external_models = ModelConfigManager(tmp_path / "models.json")
        external_models.set_rerank_config(ReRankConfig(model="rank", api_key="second-key", base_url="http://localhost:1234"), "rank")
        replacement = await prepare()
        assert replacement is not plugin
        assert replacement.resources is not None
        assert replacement.resources._configs["rerank"][1].api_key == "second-key"
    finally:
        if asynchronous:
            await provider.release_session_async(session)
        else:
            provider.release_session(session)

class _CapturingSession(_session_providers_Session):
    """记录 Provider 传入参数的测试会话"""

    def __init__(self, session_id: str, marker: str = "", **kwargs: Any) -> None:
        self.session_id = session_id
        self.marker = marker
        self.kwargs = kwargs

class _PluginSession(_session_providers_Session):
    """记录同步插件生命周期的测试会话"""

    def __init__(self, session_id: str, **kwargs: Any) -> None:
        self.session_id = session_id
        self.installed: list[str] = []
        self.uninstalled: list[str] = []

class _AsyncPluginSession(_session_providers_AsyncSession):
    """记录异步插件生命周期的测试会话"""

    def __init__(self, session_id: str, **kwargs: Any) -> None:
        self.session_id = session_id
        self.installed: list[str] = []
        self.uninstalled: list[str] = []

class _CapturingAsyncSession(_session_providers_AsyncSession):
    """记录热重启后有效配置的异步测试会话"""

    def __init__(self, session_id: str, marker: str = "", **kwargs: Any) -> None:
        if marker == "fail":
            raise RuntimeError("候选实例初始化失败")
        self.session_id = session_id
        self.marker = marker

class _FakeProvider:
    """用于验证统一 Provider 分发的测试实现"""

    def __init__(self, provider_name: str, definition_name: str, *, enabled: bool = True) -> None:
        self.provider_name = provider_name
        self.definition_name = definition_name
        self.enabled = enabled
        self.received: _session_providers_SessionConfig | None = None

    def has_definition(self, name: str) -> bool:
        return name == self.definition_name

    def get_definition(self, name: str) -> SessionProviderDefinition | None:
        if not self.has_definition(name):
            return None
        return SessionProviderDefinition(
            name=name,
            provider_name=self.provider_name,
            is_async=False,
            enabled=self.enabled,
            params={"marker": "provider-default"},
        )

    def create_session(self, session_config: _session_providers_SessionConfig, llm: object = None) -> _session_providers_Session:
        self.received = session_config
        return _CapturingSession(
            session_config.session_id or "",
            marker=str(session_config.session_config.get("marker", "")),
        )

def test_provider_registry_resolves_explicit_and_unique_definitions() -> None:
    """Provider 注册表应支持显式分发并拒绝未限定的歧义名称"""
    registry = SessionProviderRegistry()
    first = _FakeProvider("first", "assistant")
    second = _FakeProvider("second", "other")
    registry.register(first)
    registry.register(second)

    assert registry.resolve_definition("assistant", "first") is not None
    assert registry.resolve_definition("assistant") is not None
    assert registry.resolve_definition("missing") is None

    third = _FakeProvider("third", "assistant")
    registry.register(third)
    with pytest.raises(ValueError, match="歧义"):
        registry.resolve_definition("assistant")

def test_binding_status_distinguishes_runnable_disabled_and_invalid() -> None:
    """三态判定区分可运行, 已禁用与失效, 并保留既有 Provider 与定义诊断文本"""
    registry = SessionProviderRegistry()
    registry.register(_FakeProvider("first", "runnable"))
    registry.register(_FakeProvider("second", "disabled", enabled=False))

    assert registry.binding_status("runnable", "first") == BindingStatus(BindingState.RUNNABLE)

    disabled = registry.binding_status("disabled", "second")
    assert disabled.state is BindingState.DISABLED
    assert disabled.reason == "会话定义已禁用: provider=second, name=disabled"

    missing = registry.binding_status("missing", "first")
    assert missing.state is BindingState.INVALID
    assert missing.reason == "会话定义不可用 provider=first, name=missing"

    # Provider 未注册与名称歧义都归入失效, 但原始诊断文本原样保留
    unknown = registry.binding_status("runnable", "nope")
    assert unknown.state is BindingState.INVALID and unknown.reason == "未知会话 Provider: nope"
    registry.register(_FakeProvider("third", "runnable"))
    ambiguous = registry.binding_status("runnable")
    assert ambiguous.state is BindingState.INVALID
    assert ambiguous.reason.startswith("会话定义名称存在歧义: runnable")

def test_session_class_provider_preserves_constructor_compatibility() -> None:
    """SessionClassProvider 应合并 LLM 并兼容 session_id 加关键字参数构造器"""
    registry = SessionRegistry()
    registry.register("capturing", _CapturingSession)
    provider = SessionClassProvider(registry, default_checkpoint=True)
    config = _session_providers_SessionConfig(
        session_id="provider-1",
        session_type_name="capturing",
        session_config={"marker": "configured"},
    )

    created = provider.create_session(config)

    assert isinstance(created, _CapturingSession)
    assert created.session_id == "provider-1"
    assert created.marker == "configured"
    assert created.kwargs["enable_checkpoint"] is True

def test_session_manager_dispatches_creation_to_configured_provider(tmp_path: Path) -> None:
    """SessionManager 应根据持久化 provider_name 分发运行时创建"""
    manager = _session_providers_SessionManager(db_path=tmp_path / "sessions.db")
    provider = _FakeProvider("custom", "assistant")
    manager.register_provider(provider)
    config = _session_providers_SessionConfig(
        session_id="custom-1",
        session_type_name="assistant",
        provider_name="custom",
        session_config={"marker": "instance-value"},
    )

    entry = manager._create_entry(config)

    assert entry is not None
    assert isinstance(entry.session, _CapturingSession)
    assert entry.session.marker == "instance-value"
    assert provider.received is not None
    assert provider.received.provider_name == "custom"

def test_session_config_store_migrates_legacy_provider_column(tmp_path: Path) -> None:
    """旧数据库应自动补充 provider_name 并保留默认 SessionClassProvider 行为"""
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE session_configs (
                session_id TEXT PRIMARY KEY,
                session_type_name TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_used_at REAL NOT NULL,
                message_count INTEGER NOT NULL,
                session_config TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO session_configs VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-1", "default", 1.0, 2.0, 3, "{}"),
        )
        connection.commit()

    store = SessionConfigStore(database)
    loaded = store.get("legacy-1")

    assert loaded is not None
    assert loaded.provider_name == "session_class"

def test_session_config_store_connection_scope_closes_connection(tmp_path: Path) -> None:
    """数据库连接离开事务作用域后立即关闭"""
    store = SessionConfigStore(tmp_path / "sessions.db")
    with store._connection() as connection:
        assert connection.execute("SELECT 1").fetchone() is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")

def test_user_call_resolves_same_name_by_explicit_provider(tmp_path: Path) -> None:
    """UserCall 应按显式 Provider 解析同名定义"""
    manager = _session_providers_SessionManager(default_session_type="assistant", db_path=tmp_path / "sessions.db")
    custom = _FakeProvider("custom", "assistant")
    manager.register_provider(custom)

    config = manager._resolve_or_create_session_config(
        UserCall(session_provider="custom", session_type="assistant")
    )

    assert config.provider_name == "custom"
    assert config.session_type_name == "assistant"

def test_edictum_provider_creates_registered_factory_session(tmp_path: Path) -> None:
    """EdictumProvider 应通过类型注册表创建命名配置对应的会话"""
    registry = EdictumTypeRegistry()
    registry.register(
        EdictumTypeDefinition(
            name="capturing",
            factory=_CapturingSession,
            is_async=False,
        )
    )
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_manager.create(
        "assistant",
        {
            "edictum_type": "capturing",
            "model_name": "demo",
            "params": {"marker": "edictum"},
        },
    )
    provider = EdictumProvider(config_manager, registry)
    config = _session_providers_SessionConfig(
        session_id="edictum-1",
        session_type_name="assistant",
        provider_name="edictum",
        session_config={"marker": "instance"},
    )

    created = provider.create_session(config, llm=_placeholder_llm())

    assert isinstance(created, _CapturingSession)
    assert created.session_id == "edictum-1"
    assert created.marker == "instance"

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_edictum_provider_manages_plugin_lifecycle(tmp_path: Path, asynchronous: bool) -> None:
    """EdictumProvider 安装启用插件, 暴露状态并在释放时卸载 (同步与异步镜像)"""
    if asynchronous:
        async def install_async(session: _session_providers_Session | _session_providers_AsyncSession, path: str, config: dict[str, Any] | None) -> object:
            assert isinstance(session, _AsyncPluginSession)
            session.installed.append(Path(path).name)
            return object()

        async def uninstall_async(session: _session_providers_Session | _session_providers_AsyncSession, name: str) -> bool:
            assert isinstance(session, _AsyncPluginSession)
            session.uninstalled.append(name)
            return True

        install = install_async
        uninstall = uninstall_async
        session_cls: type = _AsyncPluginSession
        type_name = "plugin-async"
        plugins_payload: list[Any] = ["session_commands"]
    else:
        def install_sync(session: _session_providers_Session | _session_providers_AsyncSession, path: str, config: dict[str, Any] | None) -> object:
            assert isinstance(session, _PluginSession)
            session.installed.append(Path(path).name)
            return object()

        def uninstall_sync(session: _session_providers_Session | _session_providers_AsyncSession, name: str) -> bool:
            assert isinstance(session, _PluginSession)
            session.uninstalled.append(name)
            return True

        install = install_sync
        uninstall = uninstall_sync
        session_cls = _PluginSession
        type_name = "plugin-sync"
        plugins_payload = ["session_commands", {"name": "disabled", "enabled": False}]

    registry = EdictumTypeRegistry()
    registry.register(
        EdictumTypeDefinition(
            name=type_name,
            factory=session_cls,
            is_async=asynchronous,
            plugin_installer=install,
            plugin_uninstaller=uninstall,
        )
    )
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_manager.create(
        "assistant",
        {"edictum_type": type_name, "model_name": "demo", "plugins": plugins_payload},
    )
    provider = EdictumProvider(config_manager, registry)
    config = _session_providers_SessionConfig(
        session_id="plugin-1",
        session_type_name="assistant",
        provider_name="edictum",
    )

    created = cast("_PluginSession | _AsyncPluginSession", provider.create_session(config, llm=_placeholder_llm()))
    setattr(created, "plugin_environment", PluginEnvironment("platform", "onebot"))
    if asynchronous:
        await provider.prepare_session_async(created)
    else:
        provider.prepare_session(created)
    metadata = provider.get_runtime_metadata(created)

    assert created.installed == ["session_commands"]
    if asynchronous:
        assert metadata["plugin_summary"]["loaded"] == 1
        await provider.release_session_async(created)
    else:
        assert metadata["plugin_summary"] == {
            "total": 2,
            "loaded": 1,
            "errors": 0,
            "pending": 0,
            "drift": 0,
            "restart_required": 0,
        }
        assert metadata["plugins"][1]["status"] == "disabled"
        provider.release_session(created)
        assert provider.get_runtime_metadata(created) == {}
    assert created.uninstalled == ["session_commands"]

def test_default_edictum_type_loads_platform_command_plugin(tmp_path: Path) -> None:
    """内置 Edictum 类型应实际安装平台命令插件并扩展帮助命令"""
    registry = create_default_edictum_type_registry()
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_payload: dict[str, Any] = {
        "edictum_type": "simple",
        "model_name": "demo",
        "plugins": [
            {
                "name": "session_commands",
                "config": {"about_text": "当前平台的自定义说明"},
            }
        ],
    }
    config_manager.create(
        "assistant",
        config_payload,
    )
    provider = EdictumProvider(config_manager, registry)
    config = _session_providers_SessionConfig(
        session_id="assistant:onebot:user-1:demo",
        session_type_name="assistant",
        provider_name="edictum",
    )

    created = provider.create_session(config, llm=_placeholder_llm())
    setattr(created, "plugin_environment", PluginEnvironment("platform", "onebot"))
    provider.prepare_session(created)
    assert isinstance(created, _session_providers_Session)
    help_result = created.run("/help")
    about_result = created.run("/about")
    new_result = created.run("/new")

    assert "/new" in str(help_result)
    assert "/history" in str(help_result)
    assert "/switch" in str(help_result)
    assert "/about" in str(help_result)
    assert about_result == "当前平台的自定义说明"
    assert isinstance(new_result, CommandAction)
    assert provider.get_runtime_metadata(created)["plugin_summary"]["loaded"] == 1
    provider.release_session(created)

@pytest.mark.asyncio
async def test_edictum_provider_applies_and_hot_updates_plugin_capabilities(
    tmp_path: Path,
) -> None:
    """EdictumProvider 应在新会话和活跃会话中统一应用子能力状态"""
    registry = create_default_edictum_type_registry()
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    initial_config: dict[str, Any] = {
        "edictum_type": "async_simple",
        "model_name": "demo",
        "plugins": [
            {
                "name": "session_commands",
                "capabilities": {"commands": {"about": False}},
            }
        ],
    }
    config_manager.create(
        "assistant",
        initial_config,
    )
    provider = EdictumProvider(config_manager, registry)
    config = _session_providers_SessionConfig(
        session_id="plugin-capability-1",
        session_type_name="assistant",
        provider_name="edictum",
    )

    created = provider.create_session(config, llm=_placeholder_llm())
    setattr(created, "plugin_environment", PluginEnvironment("platform", "onebot"))
    await provider.prepare_session_async(created)
    assert isinstance(created, AsyncSimpleSession)
    plugin = next(item for item in created.list_plugins() if item.name == "session_commands")
    assert plugin.commands["about"] is False
    actual = provider.get_runtime_metadata(created)["plugins"][0]
    assert actual["capabilities"]["loaded"]["commands"]["about"] is False

    updated_plugins: dict[str, Any] = {
        "plugins": [
            {
                "name": "session_commands",
                "capabilities": {"commands": {"about": True}},
            }
        ]
    }
    config_manager.update(
        "assistant",
        updated_plugins,
    )
    result = await provider.reconcile_session_plugins_async(created)

    assert result["ok"] is True
    assert result["restart_required"] is False
    assert plugin.commands["about"] is True
    assert provider.get_runtime_metadata(created)["plugins"][0]["capabilities"]["loaded"]["commands"]["about"] is True

    reconfigure_payload: dict[str, Any] = {
        "plugins": [
            {
                "name": "session_commands",
                "config": {"about_text": "OneBot 热重装说明"},
            }
        ]
    }
    config_manager.update(
        "assistant",
        reconfigure_payload,
    )
    preview = provider.preview_session_plugins(created)
    reinstalled = await provider.reconcile_session_plugins_async(created)

    assert preview["plugins"][0]["action"] == "reinstall"
    assert reinstalled["ok"] is True
    assert reinstalled["plugins"][0]["status"] == "applied"
    assert await created.run("/about") == "OneBot 热重装说明"

    disable_payload: dict[str, Any] = {
        "plugins": [{"name": "session_commands", "enabled": False}]
    }
    config_manager.update(
        "assistant",
        disable_payload,
    )
    disabled = await provider.reconcile_session_plugins_async(created)

    assert disabled["ok"] is True
    assert disabled["plugins"][0]["action"] == "disable"
    assert created.list_plugins() == []
    assert provider.get_runtime_metadata(created)["plugins"][0]["drift"] is False
    await provider.release_session_async(created)

@pytest.mark.asyncio
@pytest.mark.parametrize("external_write", [False, True])
async def test_edictum_full_config_change_hot_restarts_with_latest_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    external_write: bool,
) -> None:
    """Edictum 命名参数变化应原子热重启且实例只持久化覆盖项"""
    registry = EdictumTypeRegistry()
    registry.register(
        EdictumTypeDefinition(
            name="capturing_async",
            factory=_CapturingAsyncSession,
            is_async=True,
        )
    )
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_manager.create(
        "onebot-assistant",
        {
            "edictum_type": "capturing_async",
            "model_name": "demo",
            "params": {"marker": "before"},
        },
    )
    manager = _session_providers_SessionManager(db_path=tmp_path / "platform.db", platform_id="onebot-main")
    provider = EdictumProvider(config_manager, registry)
    manager.register_provider(provider)
    model_manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    model_manager.update_llm_config("demo", api_key="key", model="demo")
    manager.model_config_manager = model_manager
    session_manager_module = importlib.import_module("satrap.core.framework.SessionManager")

    def fake_build_llm(*_args: object, **_kwargs: object) -> object:
        """返回无需网络连接的模型占位对象"""
        return object()

    monkeypatch.setattr(
        session_manager_module,
        "build_llm_from_config",
        fake_build_llm,
    )
    stored = manager.register_session_from_provider_config(
        "edictum",
        "onebot-assistant",
        session_id="onebot-session",
    )

    assert stored.session_config == {}
    assert await manager.activate_session_async("onebot-session") is True
    old_entry = manager.pool.get("onebot-session")
    assert old_entry is not None
    old_session = old_entry.session
    assert isinstance(old_session, _CapturingAsyncSession)
    assert old_session.marker == "before"

    writer = EdictumConfigManager(registry, tmp_path / "edictum.json") if external_write else config_manager
    writer.update(
        "onebot-assistant",
        {"params": {"marker": "after"}},
    )
    if external_write:
        stale = config_manager.get_config("onebot-assistant")
        assert stale is not None and stale["params"]["marker"] == "before"
        backend = cast(Any, BackendManager.__new__(BackendManager))
        backend._edictum_cfg = config_manager
        backend._platform_runtimes = {"onebot-main": (manager, None)}
        result = await backend.reconcile_edictum_runtime_async(config_name="onebot-assistant")
    else:
        preview = manager.preview_edictum_runtime(config_name="onebot-assistant")
        assert preview[0]["action"] == "restart"
        result = await manager.reconcile_edictum_runtime_async(config_name="onebot-assistant")
    assert result[0]["ok"] is True
    assert result[0]["action"] == "restart"
    new_entry = manager.pool.get("onebot-session")
    assert new_entry is old_entry
    assert new_entry is not None
    assert new_entry.session is not old_session
    assert isinstance(new_entry.session, _CapturingAsyncSession)
    assert new_entry.session.marker == "after"
    onebot_cfg = manager.get_session_config("onebot-session")
    assert onebot_cfg is not None
    assert onebot_cfg.session_config == {}

    writer.update(
        "onebot-assistant",
        {"params": {"marker": "fail"}},
    )
    failed = await (backend if external_write else manager).reconcile_edictum_runtime_async(config_name="onebot-assistant")

    assert failed[0]["ok"] is False
    assert failed[0]["old_runtime_preserved"] is True
    preserved = manager.pool.get("onebot-session")
    assert preserved is new_entry
    assert preserved is not None
    assert isinstance(preserved.session, _CapturingAsyncSession)
    assert preserved.session.marker == "after"

@pytest.mark.asyncio
async def test_full_agent_apply_does_not_build_models_for_unchanged_effective_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整保存只重建有效参数变化实例, 实例覆盖和无关 Agent 保留原模型及会话"""
    registry = EdictumTypeRegistry()
    registry.register(EdictumTypeDefinition(name="capturing_async", factory=_CapturingAsyncSession, is_async=True))
    configs = EdictumConfigManager(registry, tmp_path / "edictum.json")
    for name in ("target", "unrelated"):
        configs.create(name, {"edictum_type": "capturing_async", "model_name": "demo", "params": {"marker": "before"}})
    manager = _session_providers_SessionManager(db_path=tmp_path / "platform.db", platform_id="test")
    manager.register_provider(EdictumProvider(configs, registry))
    models = ModelConfigManager(storage_path=tmp_path / "models.json")
    models.update_llm_config("demo", api_key="test-key", model="demo")
    manager.model_config_manager = models
    builds: list[object] = []

    def build(*args: object, **kwargs: object) -> object:
        """计数实际经过模型构建入口的调用"""
        model = object()
        builds.append(model)
        return model

    monkeypatch.setattr(importlib.import_module("satrap.core.framework.SessionManager"), "build_llm_from_config", build)
    for sid, name, params in (("inherited", "target", {}), ("overridden", "target", {"marker": "local"}), ("other", "unrelated", {})):
        manager.register_session_from_provider_config("edictum", name, session_id=sid, extra_params=params)
        assert await manager.activate_session_async(sid)
    old_entries = {sid: manager.pool.get(sid) for sid in ("inherited", "overridden", "other")}
    if any(entry is None for entry in old_entries.values()):
        raise AssertionError("测试实例未激活")
    old_sessions = {sid: entry.session for sid, entry in old_entries.items() if entry is not None}
    assert len(builds) == 3
    backend = cast(Any, BackendManager.__new__(BackendManager))
    backend._edictum_cfg = configs
    backend._platform_runtimes = {"test": (manager, None)}
    writer = EdictumConfigManager(registry, configs.storage_path)
    writer.update("target", {"params": {"marker": "after"}})
    result = await backend.reconcile_edictum_runtime_async(config_name="target")
    assert {item["session_id"]: item["action"] for item in result} == {"inherited": "restart", "overridden": "noop"}
    assert all(item["ok"] for item in result) and len(builds) == 4
    current = {sid: manager.pool.get(sid) for sid in old_sessions}
    if any(entry is None for entry in current.values()):
        raise AssertionError("应用后测试实例丢失")
    active = {sid: entry.session for sid, entry in current.items() if entry is not None}
    assert active["inherited"] is not old_sessions["inherited"]
    assert active["overridden"] is old_sessions["overridden"]
    assert active["other"] is old_sessions["other"]
    if not isinstance(active["inherited"], _CapturingAsyncSession) or not isinstance(active["overridden"], _CapturingAsyncSession):
        raise AssertionError("测试会话类型发生变化")
    assert active["inherited"].marker == "after" and active["overridden"].marker == "local"
    repeated = await backend.reconcile_edictum_runtime_async(config_name="target")
    assert all(item["ok"] and item["action"] == "noop" for item in repeated)
    assert len(builds) == 4

def _write_session_file(root: Path, name: str, content: str) -> Path:
    """写入测试用 session 文件"""
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(content, encoding="utf-8")
    return path

def test_discover_multiple_session_classes_in_one_file(tmp_path: Path):
    """扫描应发现同一文件中的多个 Session/AsyncSession 子类"""
    scan_dir = tmp_path / "session_src"
    _write_session_file(
        scan_dir,
        "demo_sessions.py",
        """
from satrap.core.framework import AsyncSession, Session

class PlainObject:
    pass

class SyncDemo(Session):
    def __init__(self, session_id: str, topic: str, count: int = 1):
        super().__init__(session_id)

class AsyncDemo(AsyncSession):
    def __init__(self, session_id: str, enabled: bool = True):
        super().__init__(session_id)
""".strip(),
    )

    results = discover_session_classes([str(scan_dir)])
    classes = {item.class_name: item for item in results if not item.error}

    assert sorted(classes) == ["AsyncDemo", "SyncDemo"]
    assert classes["AsyncDemo"].is_async is True
    assert classes["SyncDemo"].is_async is False
    assert classes["SyncDemo"].init_params == {"topic": "", "count": 0}

def test_discovery_service_matches_compatibility_function(tmp_path: Path):
    """发现服务与原兼容函数应返回相同结果"""
    scan_dir = tmp_path / "service_src"
    _write_session_file(
        scan_dir,
        "service_demo.py",
        """
from satrap.core.framework import Session

class ServiceDemo(Session):
    pass
""".strip(),
    )

    service_results = SessionClassDiscoveryService([str(scan_dir)]).discover()
    compatibility_results = discover_session_classes([str(scan_dir)])

    assert [item.to_dict() for item in service_results] == [
        item.to_dict() for item in compatibility_results
    ]

def test_discover_reports_import_errors_without_stopping(tmp_path: Path):
    """单个文件导入错误不应影响其它文件扫描"""
    scan_dir = tmp_path / "broken_src"
    _write_session_file(scan_dir, "broken.py", "raise RuntimeError('boom')\n")
    _write_session_file(
        scan_dir,
        "ok.py",
        """
from satrap.core.framework import Session

class OkSession(Session):
    pass
""".strip(),
    )

    results = discover_session_classes([str(scan_dir)])

    assert any(item.error and "boom" in item.error for item in results)
    assert any(item.class_name == "OkSession" for item in results)

def test_register_by_discovered_class_path(tmp_path: Path):
    """扫描得到的 class_path 可直接注册并生成参数模板"""
    scan_dir = tmp_path / "register_src"
    _write_session_file(
        scan_dir,
        "custom.py",
        """
from satrap.core.framework import Session

class CustomSession(Session):
    def __init__(self, session_id: str, label: str):
        super().__init__(session_id)
""".strip(),
    )
    discovered = [item for item in discover_session_classes([str(scan_dir)]) if item.class_name == "CustomSession"][0]
    manager = SessionClassConfigManager(
        storage_path=tmp_path / "session_classes.json",
        session_scan_paths=[str(scan_dir)],
    )

    manager.register_by_class_path("custom", discovered.class_path)

    config = manager.get_config("custom")
    assert config is not None
    assert config["class_path"] == discovered.class_path
    assert manager.get_params("custom") == {"label": ""}

def test_session_scan_paths_config_defaults_and_loading():
    """BackendConfig 应包含默认 session_scan_paths 并可从配置覆盖"""
    default_config = BackendConfig()
    loaded = ConfigLoader.from_dict({"session_scan_paths": ["custom_sessions"], "platforms": []})

    assert default_config.session_scan_paths == [".satrap/session"]
    assert loaded.session_scan_paths == ["custom_sessions"]
    assert ConfigLoader.default_config_document(default_config)["session_scan_paths"] == [".satrap/session"]

def test_create_default_session_dir(tmp_path: Path):
    """创建默认 Session 目录时应补齐 __init__.py"""
    target = create_default_session_dir([str(tmp_path / "sessions")])

    assert target.exists()
    assert (target / "__init__.py").exists()

def test_stdlib_named_session_file_loads_via_synthetic_module(tmp_path: Path, monkeypatch):
    """与标准库同名的会话文件 (如 json.py) 应经合成模块名从文件路径显式加载,;     既不遮蔽标准库也不会静默加载到标准库模块"""
    import json as stdlib_json
    import sys

    scan_dir = tmp_path / ".satrap" / "session"
    _write_session_file(
        scan_dir,
        "json.py",
        """
from satrap.core.framework import Session

class JsonSession(Session):
    def __init__(self, session_id: str):
        super().__init__(session_id)
""".strip(),
    )
    monkeypatch.chdir(tmp_path)

    results = discover_session_classes([str(scan_dir)])
    found = [item for item in results if item.class_name == "JsonSession"]
    assert len(found) == 1 and not found[0].error
    assert found[0].module_name.startswith("satrap_user_sessions_json_")
    # 标准库 json 不被遮蔽, 合成模块与标准库同时可用
    assert sys.modules["json"] is stdlib_json
    assert stdlib_json.loads('{"ok": true}') == {"ok": True}

def test_cwd_named_session_does_not_replace_stdlib(tmp_path, monkeypatch):
    import json
    import sys
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    source = _write_session_file(tmp_path, "json.py", "from satrap.core.framework import Session\nclass LocalSession(Session): pass\n")
    results = discover_session_classes([str(tmp_path)])
    found = next(item for item in results if item.class_name == "LocalSession")
    assert found.module_name.startswith("satrap_user_sessions_")
    assert sys.modules["json"] is json
    assert json.loads("{}") == {}
    assert found.file_path == str(source)
    sys.modules.pop(found.module_name, None)

def test_failed_session_execution_once_and_registration_restored(tmp_path, monkeypatch):
    import sys
    import pytest
    from satrap.core.framework.session_discovery import load_session_module
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    marker = tmp_path / "count.txt"
    source = _write_session_file(tmp_path, "test.py", "value = 1\n")
    name = "satrap_user_sessions_once_test"
    original = load_session_module(name, source)
    source.write_text(
        f"from pathlib import Path\np = Path({str(marker)!r})\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        "raise ImportError('dependency unavailable')\n", encoding="utf-8",
    )
    with pytest.raises(ImportError):
        load_session_module(name, source)
    assert marker.read_text() == "1"
    assert sys.modules[name] is original and original.value == 1
    with pytest.raises(ImportError):
        load_session_module("satrap_user_sessions_first_failure", source)
    assert marker.read_text() == "2"
    assert "satrap_user_sessions_first_failure" not in sys.modules

def test_package_relative_import_survives_discovery_and_registration(tmp_path, monkeypatch):
    import sys
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    package = tmp_path / "relative_sessions"
    _write_session_file(package, "__init__.py", "")
    _write_session_file(package, "_helper.py", "VALUE = 'ok'\n")
    _write_session_file(package, "custom.py", "from ._helper import VALUE\nfrom satrap.core.framework import Session\nclass RelativeSession(Session): pass\n")
    found = next(item for item in discover_session_classes([str(package)]) if item.class_name == "RelativeSession")
    assert not found.error and found.module_name == "relative_sessions.custom"
    assert sys.modules[found.module_name].VALUE == "ok"
    manager = SessionClassConfigManager(storage_path=tmp_path / "classes.json", session_scan_paths=[str(package)])
    manager.register_by_class_path("relative", found.class_path)
    stored_config = cast(dict[str, Any], manager.get_config("relative"))
    assert stored_config["class_path"] == found.class_path

def test_parent_import_does_not_execute_session_twice(tmp_path, monkeypatch):
    import sys
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    package = tmp_path / "eager_sessions"
    marker = tmp_path / "count.txt"
    _write_session_file(package, "__init__.py", "from .custom import EagerSession\n")
    _write_session_file(package, "custom.py",
        f"from pathlib import Path\np = Path({str(marker)!r})\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        "from satrap.core.framework import Session\nclass EagerSession(Session): pass\n")
    found = next(item for item in discover_session_classes([str(package)]) if item.class_name == "EagerSession")
    assert not found.error and marker.read_text() == "1"
    assert sys.modules["eager_sessions"].EagerSession is sys.modules[found.module_name].EagerSession

def test_manager_does_not_retry_user_import_error(tmp_path, monkeypatch):
    import sys
    import pytest
    from satrap.core.framework.session_discovery import build_session_module_catalog
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    marker = tmp_path / "count.txt"
    _write_session_file(tmp_path, "broken.py",
        f"from pathlib import Path\np = Path({str(marker)!r})\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        "raise ImportError('dependency unavailable')\n")
    module_name = next(iter(build_session_module_catalog([str(tmp_path)])))
    manager = SessionClassConfigManager(storage_path=tmp_path / "classes.json", session_scan_paths=[str(tmp_path)])
    with pytest.raises(ValueError, match="dependency unavailable"):
        manager.register_by_class_path("broken", module_name + ".BrokenSession")
    assert marker.read_text() == "1"
    assert module_name not in sys.modules

def test_override_isolation_and_restart(tmp_path):
    database = tmp_path / "platform.db"
    store = SessionOverrideStore(database)
    values = {"flag": False, "zero": 0, "empty": "", "array": [], "nullable": None}
    store.replace("one", "plugins.example", values, expected_revision=0)
    assert SessionOverrideStore(database).read("one", "plugins.example")["overrides"] == values
    assert store.read("two", "plugins.example")["overrides"] == {}
    assert store.read("one", "plugins.other")["overrides"] == {}
    assert not (tmp_path / "sessions").exists()

def test_concurrent_save_rejects_stale_revision(tmp_path):
    database = tmp_path / "platform.db"
    stores = [SessionOverrideStore(database), SessionOverrideStore(database)]
    barrier = threading.Barrier(2)

    def save(index):
        barrier.wait()
        try:
            stores[index].replace("one", "plugins.example", {"top_k": index + 1}, expected_revision=0)
            return "saved"
        except OverrideConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(save, [0, 1])) == ["conflict", "saved"]

def test_archive_and_override_save_cannot_lose_an_acknowledged_revision(tmp_path, monkeypatch):
    """归档快照与删除期间阻止覆盖写入, 已回收会话的旧修订号不能继续保存"""
    layout = StorageLayout(tmp_path / "data")
    store = SessionOverrideStore(layout.platform_db("chat"))
    store.replace("one", "example", {"value": 1}, expected_revision=0)
    entered, release = threading.Event(), threading.Event()
    original = module.snapshot_session_domain
    def snapshot(*args):
        result = original(*args)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(module, "snapshot_session_domain", snapshot)
    maintenance = StorageMaintenanceService(layout)
    with ThreadPoolExecutor(max_workers=2) as executor:
        archive = executor.submit(maintenance.archive_session, "chat", "one")
        assert entered.wait(5)
        save = executor.submit(store.replace, "one", "example", {"value": 2}, expected_revision=1)
        try:
            with pytest.raises(TimeoutError):
                save.result(timeout=0.1)
        finally:
            release.set()
        archived = archive.result(timeout=5)
        with pytest.raises(OverrideConflictError):
            save.result(timeout=5)
    maintenance.restore_archive("chat", archived["archive_id"])
    assert store.read("one", "example")["overrides"] == {"value": 1}

def test_reset_retains_revision_and_blocks_old_editor(tmp_path):
    store = SessionOverrideStore(tmp_path / "platform.db")
    store.replace("one", "plugins.example", {"top_k": 10}, expected_revision=0)
    record = store.replace("one", "plugins.example", {}, expected_revision=1)
    assert record["overrides"] == {}
    assert record["revision"] == 2
    with pytest.raises(OverrideConflictError):
        store.replace("one", "plugins.example", {"top_k": 3}, expected_revision=0)

def test_layers_and_restore_inheritance(tmp_path):
    manager = PluginConfigManager(tmp_path / "config")
    schema = {
        "top_k": ConfigField("top_k", "number", 5),
        "enabled": ConfigField("enabled", "bool", True),
        "ids": ConfigField("ids", "knowledge_bases", ["base"]),
    }
    manager.save_global("example", schema, {"top_k": 6})
    service = PluginSettingsService(tmp_path / "platform.db", manager)
    record = service.save("one", "example", schema, {"enabled": False, "ids": []}, expected_revision=0, named={"top_k": 7})
    assert record["config"] == {"top_k": 7, "enabled": False, "ids": []}
    assert record["sources"] == {"top_k": "named", "enabled": "session", "ids": "session"}
    assert record["inherited_sources"] == {"top_k": "named", "enabled": "default", "ids": "default"}
    manager.save_global("example", schema, {"top_k": 9})
    assert service.get("one", "example", schema)["config"]["top_k"] == 9
    record = service.save("one", "example", schema, {}, expected_revision=1)
    assert record["config"] == {"top_k": 9, "enabled": True, "ids": ["base"]}
    assert record["sources"]["enabled"] == "default"

def test_optional_model_without_default_can_remain_unconfigured(tmp_path):
    schema = parse_config_schema({"config_schema": {"embed": {"type": "embed"}, "enabled": {"type": "bool", "default": True}}})
    service = PluginSettingsService(tmp_path / "platform.db", PluginConfigManager(tmp_path / "config"))
    saved = service.save("one", "example", schema, {"enabled": False}, expected_revision=0)
    assert saved["config"] == {"enabled": False, "embed": None}
    with pytest.raises(ValueError, match="null"):
        service.save("one", "example", schema, {"embed": None}, expected_revision=1)

def test_invalid_overrides_are_not_saved(tmp_path):
    schema = parse_config_schema({"config_schema": {
        "top_k": {"type": "number", "integer": True, "minimum": 1},
        "private": {"type": "string", "session_overridable": False},
    }})
    service = PluginSettingsService(tmp_path / "platform.db", PluginConfigManager(tmp_path / "config"))
    for values in ({"top_k": 0}, {"top_k": True}, {"top_k": float("nan")}, {"private": "x"}, {"unknown": 2}):
        with pytest.raises(ValueError):
            service.save("one", "example", schema, values, expected_revision=0)
    assert service.overrides.store.read("one", "plugins.example")["revision"] == 0

def test_archive_roundtrip_and_identity_validation(tmp_path):
    database = tmp_path / "platform.db"
    store = SessionOverrideStore(database)
    store.replace("one", "plugins.example", {"top_k": 8}, expected_revision=0)
    store.replace("two", "plugins.example", {"top_k": 9}, expected_revision=0)
    records = snapshot_session_domain(database, "one")
    delete_session_domain_rows(database, "one")
    assert store.read("one", "plugins.example")["overrides"] == {}
    assert store.read("two", "plugins.example")["overrides"] == {"top_k": 9}
    restore_session_domain(database, "one", records)
    assert store.read("one", "plugins.example")["overrides"] == {"top_k": 8}
    with pytest.raises(ValueError, match="身份不匹配"):
        restore_session_domain(tmp_path / "other.db", "wrong", records)

def test_null_is_a_value_and_not_reset(tmp_path):
    schema = {"threshold": ConfigField("threshold", "number", 0.5, nullable=True)}
    service = PluginSettingsService(tmp_path / "platform.db", PluginConfigManager(tmp_path / "config"))
    record = service.save("one", "example", schema, {"threshold": None}, expected_revision=0)
    assert record["config"]["threshold"] is None
    assert record["sources"]["threshold"] == "session"

class EchoSession(_multisession_Session):
    def __init__(self, session_id: str, *, db_path: str):
        super().__init__(session_id=session_id, db_path=db_path)
        self.call_count = 0

    def run(self, query: str) -> str:
        self.call_count += 1
        self.session_ctx.add_user_message(query)
        return f"echo:{query}"

class AsyncEchoSession(_multisession_AsyncSession):
    def __init__(self, session_id: str, *, db_path: str):
        super().__init__(session_id=session_id, db_path=db_path)
        self.call_count = 0

    async def run(self, query: str) -> str:
        self.call_count += 1
        await self.session_ctx.add_user_message(query)
        return f"async-echo:{query}"

class AsyncInitCountingSession(_multisession_AsyncSession):
    """记录异步初始化次数的测试会话"""

    def __init__(self, session_id: str, *, db_path: str):
        super().__init__(session_id=session_id, db_path=db_path)
        self.initialize_count = 0

    async def _async_init(self):
        await asyncio.sleep(0.01)
        self.initialize_count += 1

    async def run(self, query: str) -> str:
        return query

def _manager(tmp_path: Path, default_session_type: str) -> _multisession_SessionManager:
    """创建完全隔离的测试会话管理器"""
    database = tmp_path / "platform.db"
    return _multisession_SessionManager(
        default_session_type=default_session_type,
        db_path=database,
        default_checkpoint_db=str(database),
        platform_id="test",
    )

def test_sync_handle_call_with_auto_create_and_reuse(tmp_path: Path):
    manager = _manager(tmp_path, "echo")
    manager.register_session_type("echo", EchoSession)

    first_call = UserCall(session_id=None, session_type="echo", message="hello")
    first_result = manager.handle_call(first_call)

    assert first_result == "echo:hello"
    assert first_call.session_id is not None

    second_call = UserCall(session_id=first_call.session_id, message="again")
    second_result = manager.handle_call(second_call)

    assert second_result == "echo:again"
    active = manager.pool.list_entries()[first_call.session_id]
    assert isinstance(active.session, EchoSession)
    assert active.session.call_count == 2

@pytest.mark.asyncio
async def test_async_handle_call_with_async_session(tmp_path: Path):
    manager = _manager(tmp_path, "aecho")
    manager.register_session_type("aecho", AsyncEchoSession)

    call = UserCall(session_id=None, session_type="aecho", message="hello")
    result = await manager.handle_call_async(call)

    assert result == "async-echo:hello"
    assert call.session_id is not None

@pytest.mark.asyncio
async def test_async_session_initializes_once(tmp_path: Path):
    """异步会话初始化只执行一次: 显式 initialize 后连续 run, 并发首次调用亦然"""
    explicit = AsyncInitCountingSession(
        "explicit-initialize",
        db_path=str(tmp_path / "platform.db"),
    )
    await explicit.initialize()
    await explicit.run("first")
    await explicit.run("second")
    assert explicit.initialize_count == 1

    concurrent = AsyncInitCountingSession(
        "concurrent-initialize",
        db_path=str(tmp_path / "platform.db"),
    )
    results = await asyncio.gather(
        concurrent.run("first"),
        concurrent.run("second"),
        concurrent.run("third"),
    )
    assert results == ["first", "second", "third"]
    assert concurrent.initialize_count == 1

def test_session_message_count_includes_unique_workflow_contexts(tmp_path: Path):
    db_path = str(tmp_path / "chat_history.db")
    session = EchoSession("message-count", db_path=db_path)
    session.session_ctx.add_user_message("session-message")
    workflow_ctx = type(session.session_ctx)("message-count_main", db_path=db_path)
    workflow_ctx.add_user_message("workflow-message")
    session._track_workflow_context("main", workflow_ctx)
    session._track_workflow_context("duplicate", workflow_ctx)

    assert _multisession_SessionManager._session_message_count(session) == 2

def test_cleanup_idle_sessions(tmp_path: Path):
    manager = _manager(tmp_path, "echo")
    manager.register_session_type("echo", EchoSession)
    call = UserCall(session_id=None, session_type="echo", message="idle")
    manager.handle_call(call)

    assert call.session_id is not None
    entry = manager.pool.list_entries()[call.session_id]
    entry.last_used = 0.0

    manager.cleanup_idle_sessions(max_idle_seconds=1)
    assert call.session_id not in manager.pool.list_entries()


def test_legacy_history_migrates_verified_bindings_and_batches_existence(tmp_path, monkeypatch):
    """旧用户绑定补齐到同 Provider 和类型的历史, 大量悬空关联只使用一个会话连接"""
    from satrap.core.framework.UserManager import UserManager
    from satrap.core.conversation import ConversationRoute, bind_conversation_route
    from satrap.expend.command.session_commands import _get_user_contexts_list
    from types import SimpleNamespace
    sm = _make_sm(tmp_path)
    users = UserManager(sm, db_path=tmp_path / "users.db")
    route = ConversationRoute("user", "test", "echo")
    current = users.resolve_session("user", "test", "echo", route=route)
    old = sm.register_session(_EchoSession, "echo", session_id="previous").session_id
    assert old is not None
    users.bind_session("user", old)
    assert users.store.list_context_history(route) == [current]
    assert set(users.context_history(route)) == {current, old}
    with users.store._connect() as conn:
        conn.executemany("INSERT OR IGNORE INTO context_session_history VALUES(?, ?, ?, ?)",
                         [(users.store._context_key(route.owner, route.platform, route.session_type, route.provider), f"missing-{index}", 1, 1) for index in range(1200)])
    session = SimpleNamespace(_user_manager=users)
    calls = []
    connect = sm.store._connect
    def tracked_connection():
        calls.append(True)
        return connect()
    monkeypatch.setattr(sm.store, "_connect", tracked_connection)
    with bind_conversation_route(route):
        assert set(_get_user_contexts_list(session)) == {current, old}
    assert len(calls) == 1


def test_user_managed_transition_requires_explicit_route_without_owner_guessing(tmp_path, monkeypatch):
    """独立宿主绑定 UserManager 后也必须传路由, 不能从旧 ID 猜测归属"""
    from satrap.core.framework.UserManager import UserManager
    from satrap.core.conversation import ConversationRoute
    sm = _make_sm(tmp_path)
    users = UserManager(sm, db_path=tmp_path / "users.db")
    sm.user_manager = users
    route = ConversationRoute("user", "test", "echo")
    initial = users.resolve_session("user", "test", "echo", route=route)
    monkeypatch.setattr(sm, "_invoke_sync_session", lambda *args: CommandAction("new_session", "must-not-exist", "不应成功"))
    result = sm.handle_call(UserCall(session_id=initial, session_type="echo", message="/new"))
    assert "缺少可信路由" in result and sm.store.get("must-not-exist") is None


@pytest.mark.parametrize("async_entry", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("failure", ["upsert", "directory", "history_start", "reference", "archive"])
async def test_new_persistence_and_cleanup_faults_keep_original_receipt(tmp_path, monkeypatch, async_entry, failure):
    """覆盖配置提交后异常与回收异常, 原路由不变且失败原因不被 finally 覆盖"""
    from satrap.core.conversation import ConversationRoute
    from satrap.core.framework.UserManager import UserManager
    sm = _make_sm(tmp_path)
    users = UserManager(sm, db_path=tmp_path / "users.db")
    sm._user_mgr = users
    route = ConversationRoute("user", "test", "echo")
    initial = users.resolve_session("user", "test", "echo", route=route)
    source = sm.store.get(initial)
    target = initial + ":failed-new"
    action = CommandAction("new_session", target, "不应成功")
    fired = False
    original_bind, original_start = sm.storage_layout.bind_session, sm.store.ensure_history_start
    def bind(scope):
        nonlocal fired
        if failure == "directory" and scope.session_id == target and not fired:
            fired = True
            raise OSError("模拟目录失败")
        return original_bind(scope)
    def history(session_id, *args):
        nonlocal fired
        if failure == "history_start" and session_id == target and not fired:
            fired = True
            raise OSError("模拟历史起点失败")
        return original_start(session_id, *args)
    monkeypatch.setattr(sm.storage_layout, "bind_session", bind)
    monkeypatch.setattr(sm.store, "ensure_history_start", history)
    if failure == "upsert":
        def fail_upsert(*args):
            raise sqlite3.OperationalError("模拟提交失败")
        monkeypatch.setattr(sm.store, "upsert", fail_upsert)
    if failure in {"reference", "archive"}:
        def broken(*args):
            raise ValueError("原始初始化失败")
        async def broken_async(*args):
            raise ValueError("原始初始化失败")
        monkeypatch.setattr(sm, "_acquire_or_create_entry", broken)
        monkeypatch.setattr(sm, "_acquire_or_create_entry_async", broken_async)
        if failure == "reference":
            monkeypatch.setattr(users.store, "has_context_reference", lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError("模拟引用读取失败")))
        else:
            monkeypatch.setattr(sm, "remove_session", lambda *_, **kw: False)
            async def not_removed(*args, **kwargs):
                return False
            monkeypatch.setattr(sm, "remove_session_async", not_removed)
    call = UserCall(session_id=initial, session_type="echo", route=route, message="/new")
    result = await sm._handle_transition_async(call, source, action) if async_entry else sm._handle_transition_sync(call, source, action)
    assert "切换失败" in result and "不应成功" not in result
    assert call.session_id == initial and call.context_reset_at is None
    assert users.resolve_session("user", "test", "echo", route=route) == initial
    if failure in {"upsert", "directory", "history_start"}:
        assert sm.store.get(target) is None
        assert "新会话清理失败" not in result
    else:
        assert "原始初始化失败" in result and "新会话清理失败" in result
        assert sm.store.get(target) is not None
