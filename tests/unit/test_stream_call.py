from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterator, TypeVar

import pytest
from pathlib import Path

from satrap import (
    AsyncLLM,
    AsyncModelWorkflowFramework,
    AsyncTool,
    AsyncToolsManager,
    LLM,
    LLMCallResponse,
    LLMCallStreamEvent,
    ModelWorkflowFramework,
    Tool,
    ToolsManager,
)
from satrap.core.utils.context import AsyncContextManager, ContextManager


def _chunks() -> list[dict[str, Any]]:
    return [
        {"choices": [{"delta": {"content": "我来计算"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {
                                    "name": "calculate",
                                    "arguments": '{"expression":"2 +',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": ' 3"}'}}
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]


class _SyncCompletions:
    def create(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        assert kwargs["stream"] is True
        assert kwargs["tools"] == [{"type": "function"}]
        return iter(_chunks())


class _SyncClient:
    chat = SimpleNamespace(completions=_SyncCompletions())


class _AsyncStream:
    def __aiter__(self) -> "_AsyncStream":
        self._iterator: Iterator[dict[str, Any]] = iter(_chunks())
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iterator)
        except StopIteration:
            raise StopAsyncIteration


class _AsyncCompletions:
    async def create(self, **kwargs: Any):
        assert kwargs["stream"] is True
        return _AsyncStream()


class _AsyncClient:
    chat = SimpleNamespace(completions=_AsyncCompletions())


_LLM = TypeVar("_LLM", LLM, AsyncLLM)


def _make_llm(cls: type[_LLM], client: object) -> _LLM:
    llm = cls.__new__(cls)
    llm.client = client   # type: ignore[assignment] 测试替身, 非真实 OpenAI 客户端
    llm.model = "deepseek-v4-flash"
    llm.temperature = 0.2
    llm.top_p = 0.95
    llm.max_tokens = 100
    llm.reasoning_body = None
    llm.thinking_field_name = "reasoning_content"
    llm.suppress_error = False
    llm.return_false = False
    return llm


def test_sync_stream_call_aggregates_tool_arguments():
    llm = _make_llm(LLM, _SyncClient())
    events = list(llm.stream_call([{"role": "user", "content": "2+3"}], tools=[{"type": "function"}]))

    assert [event.kind for event in events] == [
        "content_delta",
        "tool_call_delta",
        "tool_call_delta",
        "done",
    ]
    response = events[-1].response
    assert isinstance(response, LLMCallResponse)
    assert response.type == "tools_call"
    assert response.tool_calls is not None
    assert response.tool_calls[0]["arguments"] == {"expression": "2 + 3"}


@pytest.mark.asyncio
async def test_async_stream_call_aggregates_tool_arguments():
    llm = _make_llm(AsyncLLM, _AsyncClient())
    events = [
        event
        async for event in llm.stream_call(
            [{"role": "user", "content": "2+3"}],
            tools=[{"type": "function"}],
        )
    ]

    assert isinstance(events[-1].response, LLMCallResponse)
    assert events[-1].response.tool_calls is not None
    assert events[-1].response.tool_calls[0]["arguments"] == {"expression": "2 + 3"}


class _CalculateTool(Tool):
    tool_name = "calculate"
    description = "计算"
    params_dict = {"expression": ("string", "表达式")}

    def execute(self, expression: str):
        assert expression == "2 + 3"
        return {"result": 5}


class _AsyncCalculateTool(AsyncTool):
    tool_name = "calculate"
    description = "计算"
    params_dict = {"expression": ("string", "表达式")}

    async def execute(self, expression: str):
        assert expression == "2 + 3"
        return {"result": 5}


class _AgentLLM:
    def __init__(self):
        self.responses = [
            LLMCallResponse(
                type="tools_call",
                content="",
                tool_calls=[
                    {
                        "name": "calculate",
                        "id": "call_1",
                        "arguments": {"expression": "2 + 3"},
                    }
                ],
            ),
            LLMCallResponse(type="message", content="结果是 5"),
        ]

    def stream_call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, thinking: bool = False):
        assert thinking is False
        response = self.responses.pop(0)
        if response.content:
            yield LLMCallStreamEvent(kind="content_delta", delta=response.content)
        yield LLMCallStreamEvent(kind="done", response=response)


def test_stream_full_agent_executes_tool_and_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def _ctx(conversation_id: str) -> ContextManager:
        return ContextManager(conversation_id, db_path=str(tmp_path / "chat_history.db"))

    monkeypatch.setattr("satrap.core.framework.Base.ContextManager", _ctx)
    tools = ToolsManager()
    tools.register_tool(_CalculateTool())
    agent = ModelWorkflowFramework(
        llm=_AgentLLM(),   # type: ignore[arg-type]
        context_id="stream-agent-test",
        tools_manager=tools,
    )

    assert agent.stream_full_agent("请计算 2+3", callback=False) == "结果是 5"
    roles = [message["role"] for message in agent.ctx.get_context()]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_stream_tools_agent_keeps_only_system_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def _ctx(conversation_id: str) -> ContextManager:
        return ContextManager(conversation_id, db_path=str(tmp_path / "chat_history.db"))

    monkeypatch.setattr("satrap.core.framework.Base.ContextManager", _ctx)
    tools = ToolsManager()
    tools.register_tool(_CalculateTool())
    agent = ModelWorkflowFramework(
        llm=_AgentLLM(),   # type: ignore[arg-type]
        context_id="stream-tools-agent-test",
        tools_manager=tools,
    )
    agent.ctx.reset_system_prompt("只保留系统消息")
    agent.ctx.add_user_message("历史消息")

    assert agent.stream_tools_agent("请计算 2+3", callback=False) == "结果是 5"
    assert agent.ctx.get_context() == [
        {"role": "system", "content": "只保留系统消息"},
    ]


class _ThinkingAgentLLM:
    def stream_call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, thinking: bool = False):
        assert thinking is True
        yield LLMCallStreamEvent(kind="thinking_delta", delta="先检查工具")
        yield LLMCallStreamEvent(kind="content_delta", delta="已完成")
        yield LLMCallStreamEvent(
            kind="done",
            response=LLMCallResponse(type="message", content="已完成", thinking="先检查工具"),
        )


def test_stream_full_agent_separates_thinking_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def _ctx(conversation_id: str) -> ContextManager:
        return ContextManager(conversation_id, db_path=str(tmp_path / "chat_history.db"))

    monkeypatch.setattr("satrap.core.framework.Base.ContextManager", _ctx)
    content: list[str] = []
    thinking: list[str] = []
    agent = ModelWorkflowFramework(
        llm=_ThinkingAgentLLM(),   # type: ignore[arg-type]
        context_id="stream-thinking-test",
        content_callback=content.append,
        return_thinking=True,
        thinking_callback=thinking.append,
    )

    assert agent.stream_full_agent("测试", callback=True, thinking=True) == "已完成"
    assert thinking == ["先检查工具"]
    assert content == ["已完成"]


class _AsyncThinkingAgentLLM:
    async def stream_call(self, messages: list[dict], tools: list[dict] | None = None, thinking: bool = False):
        assert thinking is True
        yield LLMCallStreamEvent(kind="thinking_delta", delta="异步检查")
        yield LLMCallStreamEvent(kind="content_delta", delta="异步完成")
        yield LLMCallStreamEvent(
            kind="done",
            response=LLMCallResponse(type="message", content="异步完成", thinking="异步检查"),
        )


@pytest.mark.asyncio
async def test_async_stream_full_agent_separates_thinking_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def _ctx(conversation_id: str) -> AsyncContextManager:
        return AsyncContextManager(conversation_id, db_path=str(tmp_path / "async-chat-history.db"))

    monkeypatch.setattr("satrap.core.framework.Base.AsyncContextManager", _ctx)
    content: list[str] = []
    thinking: list[str] = []

    async def content_callback(value: str):
        content.append(value)

    async def thinking_callback(value: str):
        thinking.append(value)

    agent = AsyncModelWorkflowFramework(
        llm=_AsyncThinkingAgentLLM(),   # type: ignore[arg-type]
        context_id="async-stream-thinking-test",
        content_callback=content_callback,
        return_thinking=True,
        thinking_callback=thinking_callback,
    )
    await agent.initialize()

    assert await agent.stream_full_agent("测试", callback=True, thinking=True) == "异步完成"
    assert thinking == ["异步检查"]
    assert content == ["异步完成"]


class _AsyncToolAgentLLM:
    def __init__(self):
        self.responses = [
            LLMCallResponse(
                type="tools_call",
                content="",
                tool_calls=[
                    {
                        "name": "calculate",
                        "id": "call_1",
                        "arguments": {"expression": "2 + 3"},
                    }
                ],
            ),
            LLMCallResponse(type="message", content="结果是 5"),
        ]

    async def stream_call(self, messages: list[dict], tools: list[dict] | None = None, thinking: bool = False):
        assert thinking is False
        response = self.responses.pop(0)
        if response.content:
            yield LLMCallStreamEvent(kind="content_delta", delta=response.content)
        yield LLMCallStreamEvent(kind="done", response=response)


@pytest.mark.asyncio
async def test_async_stream_tools_agent_keeps_only_system_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def _ctx(conversation_id: str) -> AsyncContextManager:
        return AsyncContextManager(conversation_id, db_path=str(tmp_path / "async-chat-history.db"))

    monkeypatch.setattr("satrap.core.framework.Base.AsyncContextManager", _ctx)
    tools = AsyncToolsManager()
    tools.register_tool(_AsyncCalculateTool())
    agent = AsyncModelWorkflowFramework(
        llm=_AsyncToolAgentLLM(),   # type: ignore[arg-type]
        tools_manager=tools,
        context_id="async-stream-tools-agent-test",
    )
    await agent.initialize()
    await agent.ctx.reset_system_prompt("只保留异步系统消息")
    await agent.ctx.add_user_message("异步历史消息")

    assert await agent.stream_tools_agent("请计算 2+3", callback=False) == "结果是 5"
    assert agent.ctx.get_context() == [
        {"role": "system", "content": "只保留异步系统消息"},
    ]
