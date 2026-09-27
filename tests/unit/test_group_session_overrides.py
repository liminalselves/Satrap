"""逐群会话覆盖的结构, 继承语义和运行时优先级测试"""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
from typing import Any, cast

import pytest

from satrap.core.config.group_session import resolve_group_session, session_values
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.providers import EdictumProvider, SessionProviderRegistry
from satrap.core.type import SessionConfig, UserCall
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.simple_session import AsyncSimpleSession


def test_group_session_prompt_empty_and_plugin_merge() -> None:
    explicit = {
        "prompt": {"mode": "value", "value": ""},
        "plugins": {"mode": "value", "value": [
            {"name": "search", "mode": "disabled"},
            {"name": "extra", "mode": "enabled", "config": {"limit": 2}},
        ]},
    }
    effective, sources = resolve_group_session(
        {"session_provider": "edictum", "session_type": "simple"}, explicit,
        {"model": "base", "prompt": "原提示词", "plugins": [
            {"name": "search", "enabled": True, "config": {"limit": 1}},
        ]},
    )
    assert effective["prompt"] == ""
    assert sources["prompt"] == "group"
    assert effective["plugins"] == [
        {"name": "search", "enabled": False, "config": {"limit": 1}},
        {"name": "extra", "enabled": True, "config": {"limit": 2}},
    ]
    assert effective["model"] == "base"
    assert sources["model"] == "named_config"


def test_group_session_rejects_duplicate_or_untyped_plugin_override() -> None:
    with pytest.raises(ValueError, match="重复"):
        session_values({"plugins": {"mode": "value", "value": [
            {"name": "x", "mode": "enabled"}, {"name": "x", "mode": "disabled"},
        ]}})
    with pytest.raises(ValueError, match="插件覆盖项"):
        session_values({"plugins": {"mode": "value", "value": ["x"]}})


def test_instance_plugins_win_over_group_plugins() -> None:
    manager = object.__new__(SessionManager)
    cfg = SessionConfig(session_id="sid", session_type_name="simple", provider_name="edictum",
                        session_config={"plugins": [{"name": "instance", "enabled": True}]})
    call = UserCall(group_session_overrides={"plugins": [{"name": "group", "mode": "disabled"}]})
    assert manager._group_plugin_target(cfg, call) == [{"name": "instance", "enabled": True}]


@pytest.mark.asyncio
async def test_instance_prompt_wins_and_group_prompt_preserves_context_history() -> None:
    class Context:
        def __init__(self) -> None:
            self.messages = ["旧消息"]
            self.prompt = "基础"

        async def reset_system_prompt(self, value: str) -> None:
            self.prompt = value

    class SessionStub:
        def __init__(self) -> None:
            self.context = Context()

        def _all_contexts(self) -> dict[str, Context]:
            return {"main": self.context}

    provider = SimpleNamespace()
    definition = SimpleNamespace(params={"model_name": "base", "system_prompt": "基础"})
    manager = object.__new__(SessionManager)
    manager.provider_registry = cast(Any, SimpleNamespace(resolve_definition=lambda *_: (provider, definition)))
    manager._model_cfg_mgr = None
    session = SessionStub()
    cfg = SessionConfig(session_id="sid", session_type_name="simple", provider_name="edictum")
    call = UserCall(group_session_overrides={"prompt": ""})
    await manager._apply_group_session_overrides(cfg, cast(Any, session), call, None)
    assert session.context.prompt == ""
    assert session.context.messages == ["旧消息"]

    cfg.session_config["system_prompt"] = "实例优先"
    await manager._apply_group_session_overrides(cfg, cast(Any, session), call, None)
    assert session.context.prompt == "实例优先"
    assert session.context.messages == ["旧消息"]


@pytest.mark.asyncio
async def test_group_prompt_does_not_leak_into_another_legacy_context() -> None:
    class Context:
        prompt = ""

        def reset_system_prompt(self, value: str) -> None:
            self.prompt = value

    class SessionStub:
        def __init__(self) -> None:
            self.context = Context()

        def _all_contexts(self) -> dict[str, Context]:
            return {"main": self.context}

    manager = object.__new__(SessionManager)
    manager.provider_registry = cast(Any, SimpleNamespace(resolve_definition=lambda *_: (
        SimpleNamespace(), SimpleNamespace(params={"model_name": "base"}),
    )))
    manager._model_cfg_mgr = None
    cfg = SessionConfig(session_id="sid", session_type_name="simple", provider_name="edictum")
    session = SessionStub()
    await manager._apply_group_session_overrides(
        cfg, cast(Any, session), UserCall(group_session_overrides={"prompt": "群 A 的内容"}), None,
    )
    assert session.context.prompt == "群 A 的内容"
    await manager._apply_group_session_overrides(cfg, cast(Any, session), UserCall(), None)
    assert session.context.prompt == ""


@pytest.mark.asyncio
async def test_real_edictum_session_applies_prompt_and_plugin_overlay(tmp_path: Path) -> None:
    """真实 Edictum Provider 在已有工作流上应用覆盖并保留普通历史"""
    registry = create_default_edictum_type_registry()
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_manager.create("assistant", {
        "edictum_type": "async_simple", "model_name": "base", "params": {"system_prompt": "基础提示"},
        "plugins": ["session_commands"],
    })
    provider = EdictumProvider(config_manager, registry, default_checkpoint_db=str(tmp_path / "platform.db"))
    cfg = SessionConfig(session_id="group-runtime", session_type_name="assistant", provider_name="edictum")
    session = provider.create_session(cfg, llm=cast(Any, object()))
    assert isinstance(session, AsyncSimpleSession)
    session.plugin_environment = PluginEnvironment("platform", "onebot")
    manager = object.__new__(SessionManager)
    manager.provider_registry = SessionProviderRegistry()
    manager.provider_registry.register(provider)
    manager._model_cfg_mgr = None
    try:
        await provider.prepare_session_async(session)
        assert session._wf is not None
        context = session._wf.ctx
        await context.add_user_message("已有历史")
        call = UserCall(group_session_overrides={
            "prompt": "", "plugins": [{"name": "session_commands", "mode": "disabled"}],
        })
        target = manager._group_plugin_target(cfg, call)
        await manager._apply_group_session_overrides(cfg, session, call, target)
        assert context._messages[0] == {"role": "system", "content": ""}
        assert any("已有历史" in str(item.get("content")) for item in context._messages)
        assert session.list_plugins() == []
        setattr(session, "_satrap_group_plugins", None)
        await provider.prepare_session_async(session)
        await manager._apply_group_session_overrides(cfg, session, UserCall(), None)
        assert context._messages[0] == {"role": "system", "content": "基础提示"}
        assert any("已有历史" in str(item.get("content")) for item in context._messages)
        assert len(session.list_plugins()) == 1
    finally:
        await provider.release_session_async(session)
