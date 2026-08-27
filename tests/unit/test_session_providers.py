from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.framework.SessionManager import SessionConfigStore, SessionManager, SessionRegistry
from satrap.core.framework.providers import (
    EdictumProvider,
    SessionClassProvider,
    SessionProviderDefinition,
    SessionProviderRegistry,
)
from satrap.core.type import CommandAction, SessionConfig, UserCall
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.registry import (
    EdictumTypeDefinition,
    EdictumTypeRegistry,
    create_default_edictum_type_registry,
)


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

    created = provider.create_session(config, llm=object())   # type: ignore[arg-type]

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

    created = provider.create_session(config, llm=object())   # type: ignore[arg-type]
    provider.prepare_session(created)
    metadata = provider.get_runtime_metadata(created)

    assert isinstance(created, _PluginSession)
    assert created.installed == ["session_commands"]
    assert metadata["plugin_summary"] == {"total": 2, "loaded": 1, "errors": 0, "pending": 0}
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

    created = provider.create_session(config, llm=object())   # type: ignore[arg-type]
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

    created = provider.create_session(config, llm=object())   # type: ignore[arg-type]
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
