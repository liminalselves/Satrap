from __future__ import annotations
from typing import Any, cast

from pathlib import Path
import pytest
import copy

from satrap.core.framework.Base import AsyncModelWorkflowFramework, ModelWorkflowFramework
from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap.core.type import LLMCallResponse
from support.casts import _as_async_llm, _as_async_tools_manager, _as_llm, _as_tools_manager


class _FakeTools:
    def get_tools_definitions(self) -> list[dict[str, Any]]:
        return []


class _FakeLLM:
    def __init__(self):
        self.messages: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] | None = []

    def call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, img_urls: list[str] | None = None):
        self.messages = copy.deepcopy(messages)
        self.tools = tools
        return LLMCallResponse(type="message", content="同步回复")


class _FailingLLM:
    def __init__(self):
        self.messages: list[dict[str, Any]] = []

    def call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, img_urls: list[str] | None = None):
        self.messages = copy.deepcopy(messages)
        return False


class _FakeAsyncLLM:
    def __init__(self):
        self.messages: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] | None = []

    async def call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, img_urls: list[str] | None = None):
        self.messages = copy.deepcopy(messages)
        self.tools = tools
        return LLMCallResponse(type="message", content="异步回复")


class _FailingAsyncLLM:
    def __init__(self):
        self.messages: list[dict[str, Any]] = []

    async def call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, img_urls: list[str] | None = None):
        self.messages = copy.deepcopy(messages)
        return False


class _SharedEngineWorkflow(ModelWorkflowFramework):
    def agent_executor(self, model_response: LLMCallResponse, callback: bool = False, max_iterations: int = 10, img_urls: list[str] | None = None):
        """覆写执行器以证明统一入口不经过它"""
        self.executor_options = (callback, max_iterations)
        return ([{"role": "assistant", "content": "自定义回复"}], True)


class _AsyncSharedEngineWorkflow(AsyncModelWorkflowFramework):
    async def agent_executor(self, model_response: LLMCallResponse, callback: bool = False, max_iterations: int = 10, img_urls: list[str] | None = None):
        """覆写执行器以证明统一入口不经过它"""
        self.executor_options = (callback, max_iterations)
        return ([{"role": "assistant", "content": "异步自定义回复"}], True)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_full_agent_runs_complete_agent_flow(tmp_path: Path, asynchronous: bool):
    """验证同步与异步 full_agent 完成整轮调用并传参"""
    llm = _FakeAsyncLLM() if asynchronous else _FakeLLM()
    if asynchronous:
        wf = AsyncModelWorkflowFramework(
            llm=_as_async_llm(llm),
            context_id="full-agent-async",
            tools_manager=_as_async_tools_manager(_FakeTools()),
        )
        wf.ctx = AsyncContextManager("full-agent-async", db_path=str(tmp_path / "async.db"))
        await wf.ctx.initialize()
        result = await wf.full_agent("你好")
    else:
        wf = ModelWorkflowFramework(
            llm=_as_llm(llm),
            context_id="full-agent-sync",
            tools_manager=_as_tools_manager(_FakeTools()),
        )
        wf.ctx = ContextManager("full-agent-sync", db_path=str(tmp_path / "sync.db"))
        result = wf.full_agent("你好")

    assert result == ("异步回复" if asynchronous else "同步回复")
    user_messages = [message for message in llm.messages if message["role"] == "user"]
    assert len(user_messages) == 1 and user_messages[0]["content"] == "你好"
    assert llm.tools == []


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("entry", ["full", "tools"])
async def test_agent_entry_points_use_shared_engine(tmp_path: Path, asynchronous: bool, entry: str):
    """验证统一入口不经过子类覆写的 agent_executor, 工具入口保持无系统上下文为空"""
    llm = _FakeAsyncLLM() if asynchronous else _FakeLLM()
    if asynchronous:
        wf = _AsyncSharedEngineWorkflow(
            llm=_as_async_llm(llm),
            context_id="shared-engine-async",
            tools_manager=_as_async_tools_manager(_FakeTools()),
        )
        wf.ctx = AsyncContextManager("shared-engine-async", db_path=str(tmp_path / "shared-async.db"))
        await wf.ctx.initialize()
    else:
        wf = _SharedEngineWorkflow(
            llm=_as_llm(llm),
            context_id="shared-engine-sync",
            tools_manager=_as_tools_manager(_FakeTools()),
        )
        wf.ctx = ContextManager("shared-engine-sync", db_path=str(tmp_path / "shared-sync.db"))

    if entry == "tools":
        if asynchronous:
            async_wf = cast(_AsyncSharedEngineWorkflow, wf)
            await async_wf.ctx.add_user_message("历史消息")
            result = await async_wf.tools_agent("本轮消息", callback=False, max_iterations=2)
        else:
            sync_wf = cast(_SharedEngineWorkflow, wf)
            sync_wf.ctx.add_user_message("历史消息")
            result = sync_wf.tools_agent("本轮消息", callback=False, max_iterations=2)
    else:
        if asynchronous:
            result = await cast(_AsyncSharedEngineWorkflow, wf).full_agent("你好", callback=False, max_iterations=4)
        else:
            result = cast(_SharedEngineWorkflow, wf).full_agent("你好", callback=False, max_iterations=3)

    assert result == ("异步回复" if asynchronous else "同步回复")
    assert not hasattr(wf, "executor_options")
    if entry == "tools":
        assert wf.ctx.get_context() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_tools_agent_cleans_context_after_model_failure(tmp_path: Path, asynchronous: bool):
    """验证模型调用失败时工具入口仍清理非系统上下文"""
    llm = _FailingAsyncLLM() if asynchronous else _FailingLLM()
    if asynchronous:
        wf = AsyncModelWorkflowFramework(
            llm=_as_async_llm(llm),
            context_id="tools-agent-async-fail",
            tools_manager=_as_async_tools_manager(_FakeTools()),
        )
        wf.ctx = AsyncContextManager("tools-agent-async-fail", db_path=str(tmp_path / "tools-async-fail.db"))
        await wf.ctx.initialize()
        await wf.ctx.reset_system_prompt("系统提示")
        result = await wf.tools_agent("本轮消息")
    else:
        wf = ModelWorkflowFramework(
            llm=_as_llm(llm),
            context_id="tools-agent-fail",
            tools_manager=_as_tools_manager(_FakeTools()),
        )
        wf.ctx = ContextManager("tools-agent-fail", db_path=str(tmp_path / "tools-fail.db"))
        wf.ctx.reset_system_prompt("系统提示")
        result = wf.tools_agent("本轮消息")

    assert result == "模型调用失败"
    assert [message["role"] for message in llm.messages] == ["system", "user"]
    assert wf.ctx.get_context() == [{"role": "system", "content": "系统提示"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_del_context_keeps_only_system_messages(tmp_path: Path, asynchronous: bool):
    """验证 del_context 清空上下文时保留系统消息"""
    if asynchronous:
        ctx = AsyncContextManager("del-context-async", db_path=str(tmp_path / "del-async.db"))
        await ctx.initialize()
        await ctx.add_user_message("第一条不是系统")
        await ctx.reset_system_prompt("系统提示")
        await ctx.add_bot_message("历史回复")
        await ctx.del_context()
    else:
        ctx = ContextManager("del-context-sync", db_path=str(tmp_path / "del-sync.db"))
        ctx.add_user_message("第一条不是系统")
        ctx.reset_system_prompt("系统提示")
        ctx.add_bot_message("历史回复")
        ctx.del_context()

    assert ctx.get_context() == [{"role": "system", "content": "系统提示"}]
