"""跨平台插件应用的真实实例共享夹具"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.framework.SessionManager import SessionEntry, SessionManager
from satrap.core.framework.providers.edictum import EdictumProvider
from satrap.core.type import SessionConfig
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.edictum.registry import create_default_edictum_type_registry

async def _platforms(tmp_path: Path, monkeypatch: Any, *, plugin_name: str = "session_commands", globals_manager: PluginConfigManager | None = None) -> tuple[Any, EdictumConfigManager, PluginConfigManager, list[Any], list[EdictumProvider]]:
    """创建两个隔离平台及三个实际 Edictum 实例, 不连接模型或适配器"""
    globals_ = globals_manager or PluginConfigManager(tmp_path / "globals")
    monkeypatch.setattr("satrap.edictum.plugin_apply.PluginConfigManager", lambda: globals_)
    monkeypatch.setattr("satrap.edictum.plugin_settings.PluginConfigManager", lambda: globals_)
    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / "named.json")
    for name in ("agent-a", "agent-b"):
        configs.create(name, {"edictum_type": "async_simple", "model_name": "demo", "plugins": [plugin_name]})
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
    """卸载测试实例插件"""
    for session, provider in zip(sessions, [providers[0], providers[0], providers[1]]):
        await provider.release_session_async(session)
