"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
from typing import Any, cast
import asyncio
import copy
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.providers import BindingState, BindingStatus
from satrap.core.type import LLMCallStreamEvent
from satrap.edictum import AsyncSimpleSession, SimpleSession
from support.group_reply import restore_manager

PLUGIN = Path(__file__).resolve().parents[2] / "satrap/expend/plugins/group_chat"

class Script:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        self.observe = lambda: None

    def next(self, messages, kwargs):
        self.observe()
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(kwargs)))
        result = self.replies.pop(0)
        if callable(result):
            result = result(messages, kwargs)
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
        await instance.install_plugin(str(PLUGIN), config={"history_scope": "group_archive"})
    else:
        instance.install_plugin(str(PLUGIN), config={"history_scope": "group_archive"})
    return instance

class Invoker:
    provider_registry = SimpleNamespace(binding_status=lambda *args: BindingStatus(BindingState.RUNNABLE))
    def __init__(self, instance, asynchronous):
        self.instance, self.asynchronous = instance, asynchronous

    async def handle_call_async(self, call):
        if self.asynchronous:
            return await SessionManager._invoke_async_session(self.instance, call)
        return await asyncio.to_thread(SessionManager._invoke_sync_session, self.instance, call)
