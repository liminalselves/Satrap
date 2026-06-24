from __future__ import annotations

import asyncio

from satrap.core.framework.Base import AsyncModelWorkflowFramework, ModelWorkflowFramework
from satrap.core.type import LLMCallResponse
from satrap.core.utils.context import AsyncContextManager, ContextManager


class _FakeTools:
    def get_tools_definitions(self):
        return []


class _FakeLLM:
    def __init__(self):
        self.messages = None
        self.tools = None

    def call(self, messages, tools=None):
        self.messages = messages
        self.tools = tools
        return LLMCallResponse(type="message", content="同步回复")


class _FakeAsyncLLM:
    def __init__(self):
        self.messages = None
        self.tools = None

    async def call(self, messages, tools=None):
        self.messages = messages
        self.tools = tools
        return LLMCallResponse(type="message", content="异步回复")


def test_model_workflow_full_agent_runs_complete_agent_flow(tmp_path):
    llm = _FakeLLM()
    wf = ModelWorkflowFramework(
        llm=llm,   # type: ignore[arg-type]
        context_id="full-agent-sync",
        tools_manager=_FakeTools(),   # type: ignore[arg-type]
    )
    wf.ctx = ContextManager("full-agent-sync", db_path=str(tmp_path / "sync.db"))

    result = wf.full_agent("你好")

    assert result == "同步回复"
    assert llm.messages[0]["content"] == "你好"
    assert llm.tools == []


def test_model_workflow_full_agent_accepts_executor_options(tmp_path):
    class _Workflow(ModelWorkflowFramework):
        def agent_executor(self, model_response, callback=False, max_iterations=10):
            self.executor_options = (callback, max_iterations)
            return ([{"role": "assistant", "content": "自定义回复"}], True)

    wf = _Workflow(
        llm=_FakeLLM(),   # type: ignore[arg-type]
        context_id="full-agent-options",
        tools_manager=_FakeTools(),   # type: ignore[arg-type]
    )
    wf.ctx = ContextManager("full-agent-options", db_path=str(tmp_path / "options.db"))

    result = wf.full_agent("你好", callback=False, max_iterations=3)

    assert result == "自定义回复"
    assert wf.executor_options == (False, 3)


def test_async_model_workflow_full_agent_runs_complete_agent_flow(tmp_path):
    async def _run():
        llm = _FakeAsyncLLM()
        wf = AsyncModelWorkflowFramework(
            llm=llm,   # type: ignore[arg-type]
            context_id="full-agent-async",
            tools_manager=_FakeTools(),   # type: ignore[arg-type]
        )
        wf.ctx = AsyncContextManager("full-agent-async", db_path=str(tmp_path / "async.db"))
        await wf.ctx.initialize()

        result = await wf.full_agent("你好")

        assert result == "异步回复"
        assert llm.messages[0]["content"] == "你好"
        assert llm.tools == []

    asyncio.run(_run())


def test_async_model_workflow_full_agent_accepts_executor_options(tmp_path):
    class _Workflow(AsyncModelWorkflowFramework):
        async def agent_executor(self, model_response, callback=False, max_iterations=10):
            self.executor_options = (callback, max_iterations)
            return ([{"role": "assistant", "content": "异步自定义回复"}], True)

    async def _run():
        wf = _Workflow(
            llm=_FakeAsyncLLM(),   # type: ignore[arg-type]
            context_id="full-agent-async-options",
            tools_manager=_FakeTools(),   # type: ignore[arg-type]
        )
        wf.ctx = AsyncContextManager("full-agent-async-options", db_path=str(tmp_path / "async-options.db"))
        await wf.ctx.initialize()

        result = await wf.full_agent("你好", callback=False, max_iterations=4)

        assert result == "异步自定义回复"
        assert wf.executor_options == (False, 4)

    asyncio.run(_run())
