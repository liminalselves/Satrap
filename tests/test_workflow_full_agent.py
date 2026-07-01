from __future__ import annotations

import asyncio
import copy

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
        self.messages = copy.deepcopy(messages)
        self.tools = tools
        return LLMCallResponse(type="message", content="同步回复")


class _FailingLLM:
    def __init__(self):
        self.messages = None

    def call(self, messages, tools=None):
        self.messages = copy.deepcopy(messages)
        return False


class _FakeAsyncLLM:
    def __init__(self):
        self.messages = None
        self.tools = None

    async def call(self, messages, tools=None):
        self.messages = copy.deepcopy(messages)
        self.tools = tools
        return LLMCallResponse(type="message", content="异步回复")


class _FailingAsyncLLM:
    def __init__(self):
        self.messages = None

    async def call(self, messages, tools=None):
        self.messages = copy.deepcopy(messages)
        return False


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


def test_model_workflow_tools_agent_keeps_only_system_context(tmp_path):
    llm = _FakeLLM()
    wf = ModelWorkflowFramework(
        llm=llm,   # type: ignore[arg-type]
        context_id="tools-agent-sync",
        tools_manager=_FakeTools(),   # type: ignore[arg-type]
    )
    wf.ctx = ContextManager("tools-agent-sync", db_path=str(tmp_path / "tools-sync.db"))
    wf.ctx.reset_system_prompt("系统提示")
    wf.ctx.add_user_message("历史消息")
    wf.ctx.add_bot_message("历史回复")

    result = wf.tools_agent("本轮消息")

    assert result == "同步回复"
    assert [message["role"] for message in llm.messages] == ["system", "user"]
    assert llm.messages[0]["content"] == "系统提示"
    assert llm.messages[1]["content"] == "本轮消息"
    assert wf.ctx.get_context() == [{"role": "system", "content": "系统提示"}]

    reloaded = ContextManager("tools-agent-sync", db_path=str(tmp_path / "tools-sync.db"))
    assert reloaded.get_context() == [{"role": "system", "content": "系统提示"}]


def test_model_workflow_tools_agent_clears_without_system_and_preserves_options(tmp_path):
    class _Workflow(ModelWorkflowFramework):
        def agent_executor(self, model_response, callback=False, max_iterations=10):
            self.executor_options = (callback, max_iterations)
            return ([{"role": "assistant", "content": "临时回复"}], True)

    wf = _Workflow(
        llm=_FakeLLM(),   # type: ignore[arg-type]
        context_id="tools-agent-options",
        tools_manager=_FakeTools(),   # type: ignore[arg-type]
    )
    wf.ctx = ContextManager("tools-agent-options", db_path=str(tmp_path / "tools-options.db"))
    wf.ctx.add_user_message("历史消息")

    result = wf.tools_agent("本轮消息", callback=False, max_iterations=2)

    assert result == "临时回复"
    assert wf.executor_options == (False, 2)
    assert wf.ctx.get_context() == []


def test_model_workflow_tools_agent_cleans_context_after_model_failure(tmp_path):
    llm = _FailingLLM()
    wf = ModelWorkflowFramework(
        llm=llm,   # type: ignore[arg-type]
        context_id="tools-agent-fail",
        tools_manager=_FakeTools(),   # type: ignore[arg-type]
    )
    wf.ctx = ContextManager("tools-agent-fail", db_path=str(tmp_path / "tools-fail.db"))
    wf.ctx.reset_system_prompt("系统提示")

    result = wf.tools_agent("本轮消息")

    assert result == "模型调用失败"
    assert [message["role"] for message in llm.messages] == ["system", "user"]
    assert wf.ctx.get_context() == [{"role": "system", "content": "系统提示"}]


def test_context_manager_del_context_keeps_only_system_messages(tmp_path):
    ctx = ContextManager("del-context-sync", db_path=str(tmp_path / "del-sync.db"))
    ctx.add_user_message("第一条不是系统")
    ctx.reset_system_prompt("系统提示")
    ctx.add_bot_message("历史回复")

    ctx.del_context()

    assert ctx.get_context() == [{"role": "system", "content": "系统提示"}]


def test_async_model_workflow_tools_agent_keeps_only_system_context(tmp_path):
    async def _run():
        llm = _FakeAsyncLLM()
        wf = AsyncModelWorkflowFramework(
            llm=llm,   # type: ignore[arg-type]
            context_id="tools-agent-async",
            tools_manager=_FakeTools(),   # type: ignore[arg-type]
        )
        wf.ctx = AsyncContextManager("tools-agent-async", db_path=str(tmp_path / "tools-async.db"))
        await wf.ctx.initialize()
        await wf.ctx.reset_system_prompt("系统提示")
        await wf.ctx.add_user_message("历史消息")
        await wf.ctx.add_bot_message("历史回复")

        result = await wf.tools_agent("本轮消息")

        assert result == "异步回复"
        assert [message["role"] for message in llm.messages] == ["system", "user"]
        assert llm.messages[0]["content"] == "系统提示"
        assert llm.messages[1]["content"] == "本轮消息"
        assert wf.ctx.get_context() == [{"role": "system", "content": "系统提示"}]

        reloaded = AsyncContextManager("tools-agent-async", db_path=str(tmp_path / "tools-async.db"))
        await reloaded.initialize()
        assert reloaded.get_context() == [{"role": "system", "content": "系统提示"}]

    asyncio.run(_run())


def test_async_model_workflow_tools_agent_clears_without_system_and_preserves_options(tmp_path):
    class _Workflow(AsyncModelWorkflowFramework):
        async def agent_executor(self, model_response, callback=False, max_iterations=10):
            self.executor_options = (callback, max_iterations)
            return ([{"role": "assistant", "content": "异步临时回复"}], True)

    async def _run():
        wf = _Workflow(
            llm=_FakeAsyncLLM(),   # type: ignore[arg-type]
            context_id="tools-agent-async-options",
            tools_manager=_FakeTools(),   # type: ignore[arg-type]
        )
        wf.ctx = AsyncContextManager("tools-agent-async-options", db_path=str(tmp_path / "tools-async-options.db"))
        await wf.ctx.initialize()
        await wf.ctx.add_user_message("历史消息")

        result = await wf.tools_agent("本轮消息", callback=False, max_iterations=2)

        assert result == "异步临时回复"
        assert wf.executor_options == (False, 2)
        assert wf.ctx.get_context() == []

    asyncio.run(_run())


def test_async_model_workflow_tools_agent_cleans_context_after_model_failure(tmp_path):
    async def _run():
        llm = _FailingAsyncLLM()
        wf = AsyncModelWorkflowFramework(
            llm=llm,   # type: ignore[arg-type]
            context_id="tools-agent-async-fail",
            tools_manager=_FakeTools(),   # type: ignore[arg-type]
        )
        wf.ctx = AsyncContextManager("tools-agent-async-fail", db_path=str(tmp_path / "tools-async-fail.db"))
        await wf.ctx.initialize()
        await wf.ctx.reset_system_prompt("系统提示")

        result = await wf.tools_agent("本轮消息")

        assert result == "模型调用失败"
        assert [message["role"] for message in llm.messages] == ["system", "user"]
        assert wf.ctx.get_context() == [{"role": "system", "content": "系统提示"}]

    asyncio.run(_run())


def test_async_context_manager_del_context_keeps_only_system_messages(tmp_path):
    async def _run():
        ctx = AsyncContextManager("del-context-async", db_path=str(tmp_path / "del-async.db"))
        await ctx.initialize()
        await ctx.add_user_message("第一条不是系统")
        await ctx.reset_system_prompt("系统提示")
        await ctx.add_bot_message("历史回复")

        await ctx.del_context()

        assert ctx.get_context() == [{"role": "system", "content": "系统提示"}]

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
