"""两种真实 Provider 创建的会话经模型工具循环验证逐次身份"""
from typing import Any

import pytest

from satrap.core.framework.providers import EdictumProvider, SessionClassProvider
from satrap.core.framework.SessionManager import SessionManager, SessionRegistry
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.simple_session import SimpleSession, AsyncSimpleSession
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.core.call_context import CallOrigin, current_call_origin, require_call_origin
from satrap.edictum.config import EdictumConfigManager
from satrap.core.type import LLMCallResponse, SessionConfig, UserCall


class ProbeLLM(LLM):
    """模型替身发送伪造身份参数, 下一步返回真实工具结果"""

    def __init__(self):
        self.count = 0

    def call(self, messages, **kwargs):
        self.count += 1
        if self.count % 2:
            return LLMCallResponse(type="tools_call", content="", tool_calls=[{
                "name": "identity_probe", "id": f"tool-{self.count}", "arguments": {"actor_id": "forged-admin"},
            }])
        result = next(item for item in reversed(messages) if item["role"] == "tool")
        return LLMCallResponse(type="answer", content=str(result["content"]))


class AsyncProbeLLM(AsyncLLM):
    """异步模型替身复用相同工具循环"""

    def __init__(self):
        self.delegate = ProbeLLM()

    async def call(self, messages, **kwargs):
        return self.delegate.call(messages, **kwargs)


class ProbeTool(Tool):
    """由真实工具注册表执行, 不信任模型主体参数"""

    tool_name = "identity_probe"
    description = "返回本轮身份"
    params_dict = {"actor_id": ("string", "模型参数")}

    def execute(self, actor_id: str):
        identity = require_call_origin()
        return f"{identity.adapter_id}/{identity.chat_id}/{identity.actor_id}"


class AsyncProbeTool(AsyncTool):
    """由异步工具注册表执行相同身份检查"""

    tool_name = ProbeTool.tool_name
    description = ProbeTool.description
    params_dict = ProbeTool.params_dict

    async def execute(self, actor_id: str):
        return ProbeTool().execute(actor_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["session_class", "edictum"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_provider_tool_loop_uses_each_shared_session_actor(tmp_path, provider_name, asynchronous):
    """实际 Provider, 会话工作流和工具注册表不得缓存上一位成员身份"""
    db_path = str(tmp_path / "sessions.db")
    if provider_name == "session_class":
        registry = SessionRegistry()
        registry.register("probe", AsyncSimpleSession if asynchronous else SimpleSession)
        provider: Any = SessionClassProvider(registry, default_checkpoint_db=db_path)
    else:
        types = create_default_edictum_type_registry()
        configs = EdictumConfigManager(types, tmp_path / "edictum.json")
        configs.create("probe", {"edictum_type": "async_simple" if asynchronous else "simple", "plugins": []})
        provider = EdictumProvider(configs, types, default_checkpoint_db=db_path)
    session = provider.create_session(
        SessionConfig(session_id="shared-group", session_type_name="probe", provider_name=provider_name),
        llm=AsyncProbeLLM() if asynchronous else ProbeLLM(),
    )
    if provider_name == "edictum":
        if asynchronous:
            await provider.prepare_session_async(session)
        else:
            provider.prepare_session(session)
    session.add_tool(AsyncProbeTool() if asynchronous else ProbeTool())
    for actor in ["admin", "member"]:
        origin = CallOrigin("bot", "10", "group_message", "20", actor, actor, f"request-{actor}")
        call = UserCall(message="报告本轮身份", origin=origin)
        result = (await SessionManager._invoke_async_session(session, call)) if asynchronous else SessionManager._invoke_sync_session(session, call)
        assert f"bot/20/{actor}" in result
        assert "forged-admin" not in result
        assert current_call_origin() is None
    if provider_name == "edictum":
        if asynchronous:
            await provider.release_session_async(session)
        else:
            provider.release_session(session)
