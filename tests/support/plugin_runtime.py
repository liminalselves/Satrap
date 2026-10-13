"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast
import asyncio
import pytest
from satrap.core.platform import current_adapter_manager
from satrap.edictum import SimpleSession, AsyncSimpleSession
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.expend.plugins.group_chat.tools import get_tools
from support.group_read import _setup

async def runtime(tmp_path: Path, asynchronous: bool, plugin_name: str, plugin_config: dict[str, Any] | None = None):
    from satrap.core.backend.BackendManager import BackendManager, BackendConfig
    from satrap.core.config.group_directory import GroupDirectoryStore
    from satrap.core.platform import PlatformAdapterManager
    from satrap.core.framework.SessionManager import SessionManager
    from satrap.core.framework.providers import EdictumProvider
    from satrap.core.type import SessionConfig
    from satrap.edictum.config import EdictumConfigManager
    from satrap.edictum.registry import create_default_edictum_type_registry

    _, adapter, origin = _setup(tmp_path)
    adapter._loop = asyncio.get_running_loop()
    settings = {"group_management_version": 1}
    adapter.config.settings = settings
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[{
        "id": "ob", "type": "onebot", "session_provider": "edictum", "session_type": "assistant", "settings": settings,
    }]))
    backend._adapter_mgr = cast(PlatformAdapterManager, current_adapter_manager())
    directory = GroupDirectoryStore(backend.platform_db_path("ob"))
    directory.adopt_legacy("10000", settings)
    directory.patch_account("10000", expected_revision=1, mode="all", approval_defaults={"set_group_card": "approval_required"})
    directory.confirm_membership("10000", "456", True)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("10000")
    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config = ({"self_nickname_enabled": True, "nickname_allowed_callers": "123"} if plugin_name == "group_chat"
              else {"write_tools_enabled": True, "allowed_callers": "123"})
    if plugin_config is not None:
        config = plugin_config
    configs.create("assistant", {"edictum_type": "async_simple" if asynchronous else "simple", "model_name": "base",
                                 "plugins": [{"name": plugin_name, "enabled": True, "config": config}]})
    provider = EdictumProvider(configs, registry, default_checkpoint_db=str(backend.platform_db_path("ob")))
    manager = SessionManager(db_path=backend.platform_db_path("ob"), platform_id="ob")
    manager.register_provider(provider)
    cfg = SessionConfig(session_id="nickname-agent", session_type_name="assistant", provider_name="edictum")
    manager.store.upsert(cfg)
    session = provider.create_session(cfg, llm=cast(Any, object()))
    session.plugin_environment = PluginEnvironment("platform", "onebot")
    manager.pool.put("nickname-agent", session, "assistant")
    backend._platform_runtimes["ob"] = cast(Any, (manager, None))
    await provider.prepare_session_async(session)
    ids = iter(f"nickname-action-{i}" for i in range(30))
    adapter.group_action_handler = lambda gid, action, params: backend.submit_group_action(
        "ob", "10000", gid, next(ids), action, params, actor_kind="model")
    return backend, adapter, origin, session, provider, configs
