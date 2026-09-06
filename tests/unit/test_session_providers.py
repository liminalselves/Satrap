from __future__ import annotations

import importlib
from pathlib import Path
import sqlite3
import pytest
from typing import Any, cast
from types import SimpleNamespace
import yaml

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.framework.SessionManager import SessionConfigStore, SessionManager, SessionRegistry
from satrap.core.framework.providers import (
    EdictumProvider,
    SessionClassProvider,
    SessionProviderDefinition,
    SessionProviderRegistry,
)
from satrap.edictum.simple_session import AsyncSimpleSession, SimpleSession
from satrap.edictum.plugin_config import PluginConfigManager, parse_config_schema
from satrap.core.APICall.LLMCall import LLM
from satrap.core.framework.Base import AsyncSession, Session
from satrap.edictum.registry import EdictumTypeDefinition, EdictumTypeRegistry, create_default_edictum_type_registry
from satrap.edictum.config import EdictumConfigManager
from satrap.core.storage import StorageLayout
from satrap.core.type import CommandAction, SessionConfig, UserCall, ReRankConfig


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
    session = provider.create_session(SessionConfig(session_id="one", session_type_name="assistant", provider_name="edictum"), llm=_placeholder_llm())
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


class _CapturingSession(Session):
    """记录 Provider 传入参数的测试会话"""

    def __init__(self, session_id: str, marker: str = "", **kwargs: Any) -> None:
        self.session_id = session_id
        self.marker = marker
        self.kwargs = kwargs


class _PluginSession(Session):
    """记录同步插件生命周期的测试会话"""

    def __init__(self, session_id: str, **kwargs: Any) -> None:
        self.session_id = session_id
        self.installed: list[str] = []
        self.uninstalled: list[str] = []


class _AsyncPluginSession(AsyncSession):
    """记录异步插件生命周期的测试会话"""

    def __init__(self, session_id: str, **kwargs: Any) -> None:
        self.session_id = session_id
        self.installed: list[str] = []
        self.uninstalled: list[str] = []


class _CapturingAsyncSession(AsyncSession):
    """记录热重启后有效配置的异步测试会话"""

    def __init__(self, session_id: str, marker: str = "", **kwargs: Any) -> None:
        if marker == "fail":
            raise RuntimeError("候选实例初始化失败")
        self.session_id = session_id
        self.marker = marker


class _FakeProvider:
    """用于验证统一 Provider 分发的测试实现"""

    def __init__(self, provider_name: str, definition_name: str) -> None:
        self.provider_name = provider_name
        self.definition_name = definition_name
        self.received: SessionConfig | None = None

    def has_definition(self, name: str) -> bool:
        return name == self.definition_name

    def get_definition(self, name: str) -> SessionProviderDefinition | None:
        if not self.has_definition(name):
            return None
        return SessionProviderDefinition(
            name=name,
            provider_name=self.provider_name,
            is_async=False,
            params={"marker": "provider-default"},
        )

    def create_session(self, session_config: SessionConfig, llm: object = None) -> Session:
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


def test_session_class_provider_preserves_constructor_compatibility() -> None:
    """SessionClassProvider 应合并 LLM 并兼容 session_id 加关键字参数构造器"""
    registry = SessionRegistry()
    registry.register("capturing", _CapturingSession)
    provider = SessionClassProvider(registry, default_checkpoint=True)
    config = SessionConfig(
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
    manager = SessionManager(db_path=tmp_path / "sessions.db")
    provider = _FakeProvider("custom", "assistant")
    manager.register_provider(provider)
    config = SessionConfig(
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
    """
    数据库连接离开事务作用域后立即关闭

    参数:
    - tmp_path: 临时目录
    """
    store = SessionConfigStore(tmp_path / "sessions.db")
    with store._connection() as connection:
        assert connection.execute("SELECT 1").fetchone() is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_user_call_resolves_same_name_by_explicit_provider(tmp_path: Path) -> None:
    """UserCall 应按显式 Provider 解析同名定义"""
    manager = SessionManager(default_session_type="assistant", db_path=tmp_path / "sessions.db")
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
    config = SessionConfig(
        session_id="edictum-1",
        session_type_name="assistant",
        provider_name="edictum",
        session_config={"marker": "instance"},
    )

    created = provider.create_session(config, llm=_placeholder_llm())

    assert isinstance(created, _CapturingSession)
    assert created.session_id == "edictum-1"
    assert created.marker == "instance"


def test_edictum_provider_manages_sync_plugin_lifecycle(tmp_path: Path) -> None:
    """EdictumProvider 应安装启用插件, 暴露状态并在释放时卸载"""
    def install(session: Session | AsyncSession, path: str, config: dict[str, Any] | None) -> object:
        assert isinstance(session, _PluginSession)
        session.installed.append(Path(path).name)
        return object()

    def uninstall(session: Session | AsyncSession, name: str) -> bool:
        assert isinstance(session, _PluginSession)
        session.uninstalled.append(name)
        return True

    registry = EdictumTypeRegistry()
    registry.register(
        EdictumTypeDefinition(
            name="plugin-sync",
            factory=_PluginSession,
            is_async=False,
            plugin_installer=install,
            plugin_uninstaller=uninstall,
        )
    )
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_payload: dict[str, Any] = {
        "edictum_type": "plugin-sync",
        "model_name": "demo",
        "plugins": ["session_commands", {"name": "disabled", "enabled": False}],
    }
    config_manager.create(
        "assistant",
        config_payload,
    )
    provider = EdictumProvider(config_manager, registry)
    config = SessionConfig(
        session_id="plugin-1",
        session_type_name="assistant",
        provider_name="edictum",
    )

    created = provider.create_session(config, llm=_placeholder_llm())
    provider.prepare_session(created)
    metadata = provider.get_runtime_metadata(created)

    assert isinstance(created, _PluginSession)
    assert created.installed == ["session_commands"]
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
    assert created.uninstalled == ["session_commands"]
    assert provider.get_runtime_metadata(created) == {}


@pytest.mark.asyncio
async def test_edictum_provider_manages_async_plugin_lifecycle(tmp_path: Path) -> None:
    """EdictumProvider 的异步生命周期应等待插件安装和卸载"""
    async def install(
        session: Session | AsyncSession,
        path: str,
        config: dict[str, Any] | None,
    ) -> object:
        assert isinstance(session, _AsyncPluginSession)
        session.installed.append(Path(path).name)
        return object()

    async def uninstall(session: Session | AsyncSession, name: str) -> bool:
        assert isinstance(session, _AsyncPluginSession)
        session.uninstalled.append(name)
        return True

    registry = EdictumTypeRegistry()
    registry.register(
        EdictumTypeDefinition(
            name="plugin-async",
            factory=_AsyncPluginSession,
            is_async=True,
            plugin_installer=install,
            plugin_uninstaller=uninstall,
        )
    )
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_manager.create(
        "assistant",
        {
            "edictum_type": "plugin-async",
            "model_name": "demo",
            "plugins": ["session_commands"],
        },
    )
    provider = EdictumProvider(config_manager, registry)
    config = SessionConfig(
        session_id="plugin-async-1",
        session_type_name="assistant",
        provider_name="edictum",
    )

    created = provider.create_session(config, llm=_placeholder_llm())
    await provider.prepare_session_async(created)
    assert isinstance(created, _AsyncPluginSession)
    assert created.installed == ["session_commands"]
    assert provider.get_runtime_metadata(created)["plugin_summary"]["loaded"] == 1
    await provider.release_session_async(created)
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
    config = SessionConfig(
        session_id="assistant:onebot:user-1:demo",
        session_type_name="assistant",
        provider_name="edictum",
    )

    created = provider.create_session(config, llm=_placeholder_llm())
    provider.prepare_session(created)
    assert isinstance(created, Session)
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
    """
    EdictumProvider 应在新会话和活跃会话中统一应用子能力状态

    参数:
    - tmp_path: 临时目录
    """
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
    config = SessionConfig(
        session_id="plugin-capability-1",
        session_type_name="assistant",
        provider_name="edictum",
    )

    created = provider.create_session(config, llm=_placeholder_llm())
    await provider.prepare_session_async(created)
    assert isinstance(created, AsyncSimpleSession)
    plugin = next(item for item in created.list_plugins() if item.name == "session_commands")
    assert plugin.commands["about"] is False

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
async def test_edictum_full_config_change_hot_restarts_with_latest_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Edictum 命名参数变化应原子热重启且实例只持久化覆盖项

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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
    manager = SessionManager(db_path=tmp_path / "platform.db", platform_id="onebot-main")
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

    config_manager.update(
        "onebot-assistant",
        {"params": {"marker": "after"}},
    )
    preview = manager.preview_edictum_runtime(config_name="onebot-assistant")
    result = await manager.reconcile_edictum_runtime_async(
        config_name="onebot-assistant"
    )

    assert preview[0]["action"] == "restart"
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

    config_manager.update(
        "onebot-assistant",
        {"params": {"marker": "fail"}},
    )
    failed = await manager.reconcile_edictum_runtime_async(
        config_name="onebot-assistant"
    )

    assert failed[0]["ok"] is False
    assert failed[0]["old_runtime_preserved"] is True
    preserved = manager.pool.get("onebot-session")
    assert preserved is new_entry
    assert preserved is not None
    assert isinstance(preserved.session, _CapturingAsyncSession)
    assert preserved.session.marker == "after"
