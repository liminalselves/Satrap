from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
from typing import Any, cast
import asyncio
import copy
import pytest
import yaml

from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.edictum.plugin_config import parse_config_schema
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.providers import BindingState, BindingStatus
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.components import PlatformComponentType
from satrap.core.platform.event import MessageEvent
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent
from satrap.edictum import AsyncSimpleSession, SimpleSession

from .test_group_chat_reply import setup, send_mock, COMPONENTS, restore_manager

PLUGIN = Path(__file__).resolve().parents[2] / "satrap/expend/plugins/group_chat"

class Script:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        self.observe = lambda: None

    def next(self, messages, kwargs):
        self.observe()
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(kwargs)))
        result = self.replies.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class Model:
    model = "offline"
    def __init__(self, script):
        self.script = script

    def call(self, messages, **kwargs):
        return self.script.next(messages, kwargs)

    def stream_call(self, messages, **kwargs):
        reply = self.call(messages, **kwargs)
        yield LLMCallStreamEvent("thinking_delta", delta="隐藏思考")
        yield LLMCallStreamEvent("content_delta", delta=reply.content)
        yield LLMCallStreamEvent("done", response=reply)


class AsyncModel:
    model = "offline"
    def __init__(self, script):
        self.script = script

    async def call(self, messages, **kwargs):
        return self.script.next(messages, kwargs)

    async def stream_call(self, messages, **kwargs):
        reply = await self.call(messages, **kwargs)
        yield LLMCallStreamEvent("thinking_delta", delta="隐藏思考")
        yield LLMCallStreamEvent("content_delta", delta=reply.content)
        yield LLMCallStreamEvent("done", response=reply)


async def session(tmp_path, script, asynchronous, stream):
    env = PluginEnvironment(session_type="platform", platform_type="onebot")
    instance: Any = (AsyncSimpleSession if asynchronous else SimpleSession)(
        "main", cast(Any, (AsyncModel if asynchronous else Model)(script)), system_prompt="基础提示词", stream=stream,
        enable_checkpoint=False, db_path=str(tmp_path / "ctx.db"), plugin_environment=env,
    )
    if asynchronous:
        await instance.initialize()
        await instance.install_plugin(str(PLUGIN))
    else:
        instance.install_plugin(str(PLUGIN))
    return instance


class Invoker:
    provider_registry = SimpleNamespace(binding_status=lambda *args: BindingStatus(BindingState.RUNNABLE))
    def __init__(self, instance, asynchronous):
        self.instance, self.asynchronous = instance, asynchronous

    async def handle_call_async(self, call):
        if self.asynchronous:
            return await SessionManager._invoke_async_session(self.instance, call)
        return await asyncio.to_thread(SessionManager._invoke_sync_session, self.instance, call)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_first_model_request_has_skill_tools_environment_and_no_early_output(tmp_path, asynchronous, stream):
    adapter, event, _ = setup(tmp_path)
    script = Script([LLMCallResponse("tools_call", "提前输出的正文", tool_calls=[{"id": "reply-1", "name": "group_chat_reply", "arguments": {"components": COMPONENTS}}]),
                     LLMCallResponse("message", "重复的最终文本")])
    instance = await session(tmp_path, script, asynchronous, stream)
    seen = []
    async def output_async(text):
        seen.append(text)
    def output_sync(text):
        seen.append(text)
    output = output_async if asynchronous else output_sync
    instance._wf.content_callback = output
    script.observe = lambda: send_mock(adapter).assert_not_awaited()
    await PipelineScheduler(cast(SessionManager, Invoker(instance, asynchronous))).execute(event)
    assert seen == []
    assert instance._wf.content_callback is output
    first, args = script.requests[0]
    assert first[0]["content"].startswith("基础提示词") and "<skill:group_chat>" in first[0]["content"]
    assert len([tool for tool in args["tools"] if tool["function"]["name"].startswith("group_chat_")]) == 6
    assert '"speaker_id": "123"' in str(first) and '"source_message_id": "77"' in str(first)
    assert '"nickname": "机器人"' in str(first)
    send_mock(adapter).assert_awaited_once()
    assert "回复甲" in "".join(c.text for c in send_mock(adapter).call_args.args[1].components if c.type == PlatformComponentType.Plain)
    assert event.last_business_receipt is not None and event.last_business_receipt.status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_failed_model_after_prepared_draft_never_sends(tmp_path, asynchronous):
    adapter, event, _ = setup(tmp_path)
    script = Script([LLMCallResponse("tools_call", "", tool_calls=[{"id": "r", "name": "group_chat_reply", "arguments": {"components": COMPONENTS}}]), RuntimeError("模型失败")])
    instance = await session(tmp_path, script, asynchronous, False)
    await PipelineScheduler(cast(SessionManager, Invoker(instance, asynchronous))).execute(event)
    send_mock(adapter).assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_plugin_disable_enable_and_private_tool_filter(tmp_path, asynchronous):
    adapter, event, _ = setup(tmp_path)
    script = Script([LLMCallResponse("message", "普通最终文本") for _ in range(3)])
    instance = await session(tmp_path, script, asynchronous, True)
    async def toggle(enabled):
        method = instance.enable_plugin if enabled else instance.disable_plugin
        if asynchronous:
            await method("group_chat")
        else:
            method("group_chat")
    await toggle(False)
    await PipelineScheduler(cast(SessionManager, Invoker(instance, asynchronous))).execute(event)
    assert "<skill:group_chat>" not in script.requests[0][0][0]["content"]
    assert not script.requests[0][1]["tools"]
    send_mock(adapter).reset_mock()
    await toggle(True)
    _, event, _ = setup(tmp_path)
    await PipelineScheduler(cast(SessionManager, Invoker(instance, asynchronous))).execute(event)
    assert script.requests[1][0][0]["content"].count("<skill:group_chat>") == 1
    adapter, private, _ = setup(tmp_path, private=True)
    await PipelineScheduler(cast(SessionManager, Invoker(instance, asynchronous))).execute(private)
    assert not script.requests[2][1]["tools"]
    assert "群聊环境资料" not in script.requests[2][0][-1]["content"]
    send_mock(adapter).assert_awaited_once()


def test_plugin_catalog_schema_and_declared_capabilities():
    from satrap.expend.plugins.group_chat.tools import DEFINITIONS
    meta = yaml.safe_load((PLUGIN / "meta.yaml").read_text(encoding="utf-8"))
    assert set(meta["tools"]) == set(DEFINITIONS)
    schema = parse_config_schema(meta)
    assert schema["member_limit"].validate_strict(10) == 10
    with pytest.raises(ValueError):
        schema["member_limit"].validate_strict(51)
    catalog = PluginCatalog(preset_dir=PLUGIN.parent, user_dir=PLUGIN / "absent")
    entry = catalog.get("group_chat")
    assert entry is not None and entry.capabilities["skills"] == meta["skills"]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_named_backend_agent_first_load_restart_and_plugin_disable(tmp_path, monkeypatch, asynchronous):
    import importlib
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.core.framework.providers import EdictumProvider
    from satrap.edictum.config import EdictumConfigManager
    from satrap.edictum.registry import create_default_edictum_type_registry
    adapter, event, _ = setup(tmp_path)
    script = Script([
        LLMCallResponse("tools_call", "查询前的文本", tool_calls=[{"id": "history", "name": "group_chat_recent_messages", "arguments": {"limit": 1}}]),
        LLMCallResponse("tools_call", "回复前的文本", tool_calls=[{"id": "reply", "name": "group_chat_reply", "arguments": {"components": COMPONENTS}}]),
        LLMCallResponse("message", "不再重复结构化正文"), LLMCallResponse("message", "重启后回复"), LLMCallResponse("message", "停用后回复"),
    ])
    registry = create_default_edictum_type_registry()
    catalog = PluginCatalog(preset_dir=PLUGIN.parent, user_dir=tmp_path / "user-plugins")
    configs = EdictumConfigManager(registry, tmp_path / "edictum.json")
    configs.plugin_catalog = catalog
    configs.create("group-agent", {"edictum_type": "async_simple" if asynchronous else "simple", "model_name": "test",
                                   "params": {"system_prompt": "群基础提示词", "stream": True}, "plugins": ["group_chat"]})
    manager = SessionManager(db_path=tmp_path / "agent.db", platform_id="ob")
    manager.plugin_environment = PluginEnvironment(session_type="platform", platform_type="onebot")
    provider = EdictumProvider(configs, registry, default_checkpoint_db=str(tmp_path / "agent.db"))
    provider.plugin_catalog = catalog
    manager.register_provider(provider)
    models = ModelConfigManager(storage_path=tmp_path / "models.json")
    models.update_llm_config("test", api_key="offline", model="test")
    manager.model_config_manager = models
    model = (AsyncModel if asynchronous else Model)(script)
    monkeypatch.setattr(importlib.import_module("satrap.core.framework.SessionManager"), "build_llm_from_config", lambda *args, **kwargs: model)
    manager.register_session_from_provider_config("edictum", "group-agent", session_id=event.session_id, extra_params={"adapter_id": "ob"})
    event.session_provider, event.session_type = "edictum", "group-agent"
    scheduler = PipelineScheduler(manager)
    await scheduler.execute(event)
    send_mock(adapter).assert_awaited_once()
    first, args = script.requests[0]
    assert first[0]["content"].count("<skill:group_chat>") == 1
    assert len(args["tools"]) == 6
    assert any(message["role"] == "tool" and '"message_id": "77"' in message["content"] for message in script.requests[1][0])
    await manager.unload_session_async(event.session_id)
    send_mock(adapter).reset_mock()
    event = MessageEvent("重启后检查", event.platform_message, adapter.meta(), event.session_id, adapter, "edictum", "group-agent")
    event.is_wake = True
    await scheduler.execute(event)
    assert script.requests[3][0][0]["content"].count("<skill:group_chat>") == 1
    send_mock(adapter).assert_awaited_once()
    configs.update("group-agent", {"plugins": []})
    results = await manager.reconcile_edictum_runtime_async(config_name="group-agent")
    assert results and results[0]["ok"]
    send_mock(adapter).reset_mock()
    event = MessageEvent("停用后检查", event.platform_message, adapter.meta(), event.session_id, adapter, "edictum", "group-agent")
    event.is_wake = True
    await scheduler.execute(event)
    assert "<skill:group_chat>" not in script.requests[4][0][0]["content"]
    assert script.requests[4][1]["tools"] == []
    send_mock(adapter).assert_awaited_once()
    await manager.unload_session_async(event.session_id)
