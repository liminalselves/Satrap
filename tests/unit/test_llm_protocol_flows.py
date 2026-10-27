"""业务链路场景与集中边界契约"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
import pytest
from satrap.core.type import LLMErrorInfo
from openai import APIError


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["401", "429", "503", "timeout", "connection", "unknown"])
async def test_request_failure_details_are_shared_and_do_not_leak_credentials(monkeypatch, caplog, kind):
    """真实 SDK 异常贯通同步/异步与流式/非流式, 只回传脱敏原因"""
    import httpx
    from openai import APIStatusError, APITimeoutError, APIConnectionError

    secret = "sk-private-credential-do-not-return"
    request = httpx.Request("POST", f"https://example.com/?key={secret}", headers={"Authorization": f"Bearer {secret}"})
    if kind.isdigit():
        error = APIStatusError(secret, response=httpx.Response(int(kind), request=request),
                               body={"error": {"code": "rate_limit_exceeded" if kind == "429" else "request_rejected", "message": secret}})
    elif kind == "timeout":
        error = APITimeoutError(request=request)
    elif kind == "connection":
        error = APIConnectionError(request=request, message=secret)
    else:
        error = RuntimeError(secret)
    llm, async_llm, _ = _clients(monkeypatch, error=error)
    for client in (llm, async_llm):
        response = client.call([{"role": "user", "content": "问题"}])
        if isinstance(client, _llm_call_parity_AsyncLLM):
            response = await response
            events = [event async for event in client.stream_call([{"role": "user", "content": "问题"}])]
        else:
            events = list(client.stream_call([{"role": "user", "content": "问题"}]))
        assert response.type == "error" and isinstance(response.error_info, LLMErrorInfo)
        assert events[0].kind == "error" and events[0].error_info == response.error_info
        assert events[-1].finish_reason == "error"
        assert "请求失败" in response.content and secret not in response.content
        assert response.error_info.status == (int(kind) if kind.isdigit() else None)
        if kind == "timeout":
            assert response.error_info.code == "network_timeout" and "60" in response.content
    assert secret not in caplog.text
from satrap import LLM, AsyncLLM as _llm_call_parity_AsyncLLM
from satrap.core.APICall.LLMCall import sync, async_
from satrap.core.type import LLMCallResponse as _llm_call_parity_LLMCallResponse
from openai.types.chat.chat_completion import ChatCompletion
from unittest.mock import AsyncMock
import httpx
from satrap.core.APICall.LLMCall import AsyncLLM as _llm_response_parsing_AsyncLLM, parse_call_response
from satrap.core.APICall.LLMCall import parse_chat_response
from pathlib import Path
from typing import Iterator, TypeVar, cast
from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap import AsyncLLM as _stream_call_AsyncLLM, AsyncModelWorkflowFramework, AsyncTool, AsyncToolsManager
from satrap import LLMCallResponse as _stream_call_LLMCallResponse, LLMCallStreamEvent, ModelWorkflowFramework
from satrap import Tool as _stream_call_Tool, ToolsManager as _stream_call_ToolsManager
from satrap.core.APICall.LLMCall import _build_thinking_extra_body
import operator
import json
import ast
from satrap.core.utils.TCBuilder import Tool as _tool_calling_Tool, ToolsManager as _tool_calling_ToolsManager
from satrap.core.utils.TCBuilder import create_tool_defined
from satrap.core.utils.context.request import BASE_AGENT_PROMPT
from satrap.core.utils import safe_parse_arguments

class _llm_call_parity_AsyncStream:
    def __aiter__(self):
        async def iterate():
            yield _chunk()

        return iterate()

def _chunk():
    return SimpleNamespace(
        choices=[
            SimpleNamespace(delta=SimpleNamespace(content="结果"), finish_reason="stop")
        ]
    )

def _response():
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="结果", tool_calls=None))
        ]
    )

def _clients(monkeypatch, error=None, **options):
    calls: list[list[dict[str, Any]]] = [[], []]

    def create(**kwargs):
        calls[0].append(deepcopy(kwargs))
        if error is not None:
            raise error
        return iter([_chunk()]) if kwargs.get("stream") else _response()

    async def acreate(**kwargs):
        calls[1].append(deepcopy(kwargs))
        if error is not None:
            raise error
        return _llm_call_parity_AsyncStream() if kwargs.get("stream") else _response()

    monkeypatch.setattr(
        sync,
        "OpenAI",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    monkeypatch.setattr(
        async_,
        "AsyncOpenAI",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=acreate))
        ),
    )
    return (
        LLM(api_key="test", model="默认模型", thinking_fields=["enable_thinking"], **options),
        _llm_call_parity_AsyncLLM(api_key="test", model="默认模型", thinking_fields=["enable_thinking"], **options),
        calls,
    )

async def _invoke(llm, method, messages, **kwargs):
    result = getattr(llm, method)(messages, **kwargs)
    if method.startswith("stream_"):
        return (
            [event async for event in result]
            if isinstance(llm, _llm_call_parity_AsyncLLM)
            else list(result)
        )
    return await result if isinstance(llm, _llm_call_parity_AsyncLLM) else result

@pytest.mark.parametrize(
    "method", ["chat", "call", "structured_output", "stream_chat", "stream_call"]
)
@pytest.mark.parametrize("system_prompt", [False, True])
async def test_request_and_response_parity(monkeypatch, method, system_prompt):
    llm, async_llm, calls = _clients(monkeypatch)
    messages = [{"role": "user", "content": "问题"}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": "规则"})
    original = deepcopy(messages)
    kwargs: dict[str, Any] = dict(
        model="覆盖模型", temperature=0, top_p=0, max_tokens=0
    )
    if method == "structured_output":
        kwargs["format"] = {"答案": "str"}
    else:
        kwargs["thinking"] = "high"
    if method in ("call", "stream_call"):
        kwargs.update(tools=[], tool_choice="none")
    result = await _invoke(llm, method, messages, **kwargs)
    async_result = await _invoke(async_llm, method, messages, **kwargs)
    assert result == async_result
    assert calls[0] == calls[1]
    assert messages == original
    request = calls[0][0]
    assert request["model"] == "覆盖模型"
    assert request["temperature"] == request["top_p"] == request["max_tokens"] == 0
    if method == "structured_output":
        assert request["response_format"] == {"type": "json_object"}
        assert "extra_body" not in request
        assert "答案" in request["messages"][0]["content"]
    else:
        assert request["extra_body"] == {"enable_thinking": True}
    if method in ("call", "stream_call"):
        assert request["tools"] == []
        assert request["tool_choice"] == "none"
    else:
        assert "tools" not in request and "tool_choice" not in request
    if method == "stream_call":
        assert request["stream_options"] == {"include_usage": True}
        assert result[-1].response.content == "结果"
    elif method == "call":
        assert isinstance(result, _llm_call_parity_LLMCallResponse) and result.content == "结果"
    else:
        assert result == (["结果"] if method == "stream_chat" else "结果")

@pytest.mark.parametrize(
    "method", ["chat", "call", "structured_output", "stream_chat", "stream_call"]
)
@pytest.mark.parametrize("return_false", [False, True])
async def test_empty_input_does_not_send_request(monkeypatch, method, return_false):
    llm, async_llm, calls = _clients(monkeypatch, return_false=return_false)
    assert await _invoke(llm, method, []) == await _invoke(async_llm, method, [])
    assert calls == [[], []]

@pytest.mark.parametrize("method", ["chat", "stream_chat"])
@pytest.mark.parametrize("error_kind", ["api", "unknown"])
@pytest.mark.parametrize(
    "suppress_error,return_false", [(True, False), (True, True), (False, False)]
)
async def test_error_contract(
    monkeypatch, method, error_kind, suppress_error, return_false
):
    import httpx

    error = (
        APIError("失败", httpx.Request("POST", "https://example.com"), body=None)
        if error_kind == "api"
        else RuntimeError("失败")
    )
    llm, async_llm, calls = _clients(
        monkeypatch,
        error=error,
        suppress_error=suppress_error,
        return_false=return_false,
    )
    messages = [{"role": "user", "content": "问题"}]
    if not suppress_error:
        for client in (llm, async_llm):
            with pytest.raises(type(error)) as raised:
                await _invoke(client, method, messages)
            assert raised.value is error
    else:
        assert await _invoke(llm, method, messages) == await _invoke(
            async_llm, method, messages
        )
    assert calls[0] == calls[1]

def _build_completion(
    message: dict[str, Any],
    usage: dict[str, Any] | None = None,
) -> ChatCompletion:
    """构造不访问网络的 OpenAI SDK 响应对象"""
    payload: dict[str, Any] = {
        "id": "chatcmpl-local-test",
        "choices": [{
            "finish_reason": "stop",
            "index": 0,
            "logprobs": None,
            "message": message,
        }],
        "created": 0,
        "model": "local-test",
        "object": "chat.completion",
    }
    if usage is not None:
        payload["usage"] = usage
    return ChatCompletion.model_validate(payload)

def test_parse_call_response_supports_sdk_message_object():
    response = _build_completion({
        "content": "模型回复",
        "refusal": None,
        "role": "assistant",
        "annotations": [],
    })

    parsed = parse_call_response(response)

    assert parsed.type == "message"
    assert parsed.content == "模型回复"

def test_response_parsers_support_multimodal_content_parts():
    """字典与对象形式的多模态 content 均只拼接文本片段"""
    response: dict[str, object] = {
        "choices": [{"message": {"content": [
            {"type": "text", "text": "第一段"},
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            SimpleNamespace(type="text", text="第二段"),
        ]}}],
    }

    assert parse_chat_response(response) == "第一段\n第二段"
    assert parse_call_response(response).content == "第一段\n第二段"

def test_parse_call_response_supports_sdk_tool_call_objects():
    response = _build_completion({
        "content": None,
        "refusal": None,
        "role": "assistant",
        "annotations": [],
        "tool_calls": [{
            "id": "call-local-test",
            "type": "function",
            "function": {
                "name": "search",
                "arguments": '{"query": "Satrap"}',
            },
        }],
    })

    parsed = parse_call_response(response)

    assert parsed.type == "tools_call"
    assert parsed.tool_calls == [{
        "name": "search",
        "id": "call-local-test",
        "arguments": {"query": "Satrap"},
    }]

def test_parse_call_response_extracts_usage_aliases():
    """普通响应应规范化两套常见 usage 字段名"""
    response: dict[str, Any] = {
        "choices": [{"message": {"role": "assistant", "content": "完成"}}],
        "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
    }

    parsed = parse_call_response(response)

    assert parsed.usage is not None
    assert parsed.usage.input_tokens == 12
    assert parsed.usage.output_tokens == 3
    assert parsed.usage.total_tokens == 15

def test_parse_call_response_extracts_sdk_cached_tokens() -> None:
    """OpenAI SDK usage 对象中的缓存命中量应被保留"""
    response = _build_completion(
        {
            "content": "完成",
            "refusal": None,
            "role": "assistant",
            "annotations": [],
        },
        {
            "prompt_tokens": 20,
            "completion_tokens": 2,
            "total_tokens": 22,
            "prompt_tokens_details": {"cached_tokens": 7},
        },
    )

    parsed = parse_call_response(response)

    assert parsed.usage is not None
    assert parsed.usage.cached_tokens == 7

@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {
                "prompt_tokens": 20,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 7},
            },
            7,
        ),
        ({"input_tokens": 20, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 6}}, 6),
        ({"prompt_tokens": 20, "completion_tokens": 2, "prompt_cache_hit_tokens": 5}, 5),
        ({"prompt_tokens": 20, "completion_tokens": 2, "cache_read_input_tokens": 4}, 4),
        ({"prompt_tokens": 20, "completion_tokens": 2, "cached_tokens": 0}, 0),
    ],
)
def test_parse_call_response_extracts_cache_hit_aliases(
    usage: dict[str, Any],
    expected: int,
) -> None:
    """普通响应应兼容常见供应商的缓存命中字段"""
    response: dict[str, Any] = {
        "choices": [{"message": {"role": "assistant", "content": "完成"}}],
        "usage": usage,
    }

    parsed = parse_call_response(response)

    assert parsed.usage is not None
    assert parsed.usage.cached_tokens == expected

@pytest.mark.asyncio
async def test_async_llm_api_error_honors_return_false() -> None:
    """APIError 被抑制时应遵守 return_false 返回契约"""
    llm = _llm_response_parsing_AsyncLLM(
        api_key="test-key",
        model="test-model",
        suppress_error=True,
        return_false=True,
    )
    request = httpx.Request("POST", "https://example.test/chat")
    llm.client.chat.completions.create = AsyncMock(
        side_effect=APIError("boom", request, body=None)
    )

    result = await llm.chat([{"role": "user", "content": "hello"}])

    assert result is False

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
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 21,
                "completion_tokens": 8,
                "total_tokens": 29,
                "prompt_tokens_details": {"cached_tokens": 7},
            },
        },
    ]

class _SyncCompletions:
    def create(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        assert kwargs["stream"] is True
        assert kwargs["tools"] == [{"type": "function"}]
        return iter(_chunks())

class _SyncClient:
    chat = SimpleNamespace(completions=_SyncCompletions())

class _stream_call_AsyncStream:
    def __aiter__(self) -> '_stream_call_AsyncStream':
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
        return _stream_call_AsyncStream()

class _AsyncClient:
    chat = SimpleNamespace(completions=_AsyncCompletions())

_LLM = TypeVar("_LLM", LLM, _stream_call_AsyncLLM)

def _as_llm(fake: Any) -> LLM:
    """LLM 替身类型边界: 替身实现 LLM 流式调用面, cast 集中在此工厂"""
    return cast(LLM, fake)

def _as_async_llm(fake: Any) -> _stream_call_AsyncLLM:
    """AsyncLLM 替身类型边界: 替身实现 AsyncLLM 流式调用面, cast 集中在此工厂"""
    return cast(_stream_call_AsyncLLM, fake)

def _make_llm(cls: type[_LLM], client: object) -> _LLM:
    llm = cls.__new__(cls)
    # 测试替身非真实 OpenAI 客户端, 边界断言集中在工厂内
    llm.client = cast(Any, client)
    llm.model = "deepseek-v4-flash"
    llm.temperature = 0.2
    llm.top_p = 0.95
    llm.max_tokens = 100
    llm.network_timeout = 60.0
    llm.base_url = "https://model.test/v1"
    llm.thinking_field_name = "reasoning_content"
    llm.thinking_fields = None
    llm.omit_none_thinking_fields = False
    llm.supports_visual_input = False
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
    assert isinstance(response, _stream_call_LLMCallResponse)
    assert response.type == "tools_call"
    assert response.tool_calls is not None
    assert response.tool_calls[0]["arguments"] == {"expression": "2 + 3"}
    assert response.usage is not None
    assert response.usage.input_tokens == 21
    assert response.usage.total_tokens == 29
    assert response.usage.cached_tokens == 7

@pytest.mark.asyncio
async def test_async_stream_call_aggregates_tool_arguments():
    llm = _make_llm(_stream_call_AsyncLLM, _AsyncClient())
    events = [
        event
        async for event in llm.stream_call(
            [{"role": "user", "content": "2+3"}],
            tools=[{"type": "function"}],
        )
    ]

    assert isinstance(events[-1].response, _stream_call_LLMCallResponse)
    assert events[-1].response.tool_calls is not None
    assert events[-1].response.tool_calls[0]["arguments"] == {"expression": "2 + 3"}
    assert events[-1].response.usage is not None
    assert events[-1].response.usage.cached_tokens == 7

class _CalculateTool(_stream_call_Tool):
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
            _stream_call_LLMCallResponse(
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
            _stream_call_LLMCallResponse(type="message", content="结果是 5"),
        ]

    def stream_call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, thinking: str = "off", img_urls: list[str] | None = None):
        assert thinking == "off"
        response = self.responses.pop(0)
        if response.content:
            yield LLMCallStreamEvent(kind="content_delta", delta=response.content)
        yield LLMCallStreamEvent(kind="done", response=response)

def test_stream_full_agent_executes_tool_and_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def _ctx(conversation_id: str, db_path: str | None = None) -> ContextManager:
        return ContextManager(conversation_id, db_path=str(tmp_path / "chat_history.db"))

    monkeypatch.setattr("satrap.core.framework.Base.ContextManager", _ctx)
    tools = _stream_call_ToolsManager()
    tools.register_tool(_CalculateTool())
    agent = ModelWorkflowFramework(
        llm=_as_llm(_AgentLLM()),
        context_id="stream-agent-test",
        tools_manager=tools,
    )

    assert agent.stream_full_agent("请计算 2+3", callback=False) == "结果是 5"
    roles = [message["role"] for message in agent.ctx.get_context()]
    assert roles == ["user", "assistant", "tool", "assistant"]

def test_stream_tools_agent_keeps_only_system_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def _ctx(conversation_id: str, db_path: str | None = None) -> ContextManager:
        return ContextManager(conversation_id, db_path=str(tmp_path / "chat_history.db"))

    monkeypatch.setattr("satrap.core.framework.Base.ContextManager", _ctx)
    tools = _stream_call_ToolsManager()
    tools.register_tool(_CalculateTool())
    agent = ModelWorkflowFramework(
        llm=_as_llm(_AgentLLM()),
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
    def stream_call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, thinking: str = "off", img_urls: list[str] | None = None):
        assert thinking == "medium"
        yield LLMCallStreamEvent(kind="thinking_delta", delta="先检查工具")
        yield LLMCallStreamEvent(kind="content_delta", delta="已完成")
        yield LLMCallStreamEvent(
            kind="done",
            response=_stream_call_LLMCallResponse(type="message", content="已完成", thinking="先检查工具"),
        )

def test_stream_full_agent_separates_thinking_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def _ctx(conversation_id: str, db_path: str | None = None) -> ContextManager:
        return ContextManager(conversation_id, db_path=str(tmp_path / "chat_history.db"))

    monkeypatch.setattr("satrap.core.framework.Base.ContextManager", _ctx)
    content: list[str] = []
    thinking: list[str] = []
    agent = ModelWorkflowFramework(
        llm=_as_llm(_ThinkingAgentLLM()),
        context_id="stream-thinking-test",
        content_callback=content.append,
        return_thinking=True,
        thinking_callback=thinking.append,
    )

    assert agent.stream_full_agent("测试", callback=True, thinking="medium") == "已完成"
    assert thinking == ["先检查工具"]
    assert content == ["已完成"]

class _AsyncThinkingAgentLLM:
    async def stream_call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, thinking: str = "off", img_urls: list[str] | None = None):
        assert thinking == "medium"
        yield LLMCallStreamEvent(kind="thinking_delta", delta="异步检查")
        yield LLMCallStreamEvent(kind="content_delta", delta="异步完成")
        yield LLMCallStreamEvent(
            kind="done",
            response=_stream_call_LLMCallResponse(type="message", content="异步完成", thinking="异步检查"),
        )

@pytest.mark.asyncio
async def test_async_stream_full_agent_separates_thinking_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def _ctx(conversation_id: str, db_path: str | None = None) -> AsyncContextManager:
        return AsyncContextManager(conversation_id, db_path=str(tmp_path / "async-chat-history.db"))

    monkeypatch.setattr("satrap.core.framework.Base.AsyncContextManager", _ctx)
    content: list[str] = []
    thinking: list[str] = []

    async def content_callback(value: str):
        content.append(value)

    async def thinking_callback(value: str):
        thinking.append(value)

    agent = AsyncModelWorkflowFramework(
        llm=_as_async_llm(_AsyncThinkingAgentLLM()),
        context_id="async-stream-thinking-test",
        content_callback=content_callback,
        return_thinking=True,
        thinking_callback=thinking_callback,
    )
    await agent.initialize()

    assert await agent.stream_full_agent("测试", callback=True, thinking="medium") == "异步完成"
    assert thinking == ["异步检查"]
    assert content == ["异步完成"]

class _AsyncToolAgentLLM:
    def __init__(self):
        self.responses = [
            _stream_call_LLMCallResponse(
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
            _stream_call_LLMCallResponse(type="message", content="结果是 5"),
        ]

    async def stream_call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, thinking: str = "off", img_urls: list[str] | None = None):
        assert thinking == "off"
        response = self.responses.pop(0)
        if response.content:
            yield LLMCallStreamEvent(kind="content_delta", delta=response.content)
        yield LLMCallStreamEvent(kind="done", response=response)

@pytest.mark.asyncio
async def test_async_stream_tools_agent_keeps_only_system_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def _ctx(conversation_id: str, db_path: str | None = None) -> AsyncContextManager:
        return AsyncContextManager(conversation_id, db_path=str(tmp_path / "async-chat-history.db"))

    monkeypatch.setattr("satrap.core.framework.Base.AsyncContextManager", _ctx)
    tools = AsyncToolsManager()
    tools.register_tool(_AsyncCalculateTool())
    agent = AsyncModelWorkflowFramework(
        llm=_as_async_llm(_AsyncToolAgentLLM()),
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

def test_off_keeps_none_fields_by_default():
    """默认保留供应商使用的 none 关闭值"""
    body = _build_thinking_extra_body(
        "off",
        ["thinking.type", "reasoning_effort", "enable_thinking"],
    )

    assert body == {
        "thinking": {"type": "disabled"},
        "reasoning_effort": "none",
        "enable_thinking": False,
    }

def test_off_can_omit_none_fields_without_dropping_explicit_switches():
    """开启省略选项后只删除 none 字段并保留显式关闭开关"""
    body = _build_thinking_extra_body(
        "off",
        ["thinking.type", "reasoning_effort", "thinking_level", "enable_thinking"],
        omit_none_thinking_fields=True,
    )

    assert body == {
        "thinking": {"type": "disabled"},
        "enable_thinking": False,
    }

def test_enabled_level_is_not_affected_by_none_omission():
    """开启思考时应完整发送开关和强度"""
    body = _build_thinking_extra_body(
        "low",
        ["thinking.type", "reasoning_effort", "thinking_level", "enable_thinking"],
        omit_none_thinking_fields=True,
    )

    assert body == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
        "thinking_level": "low",
        "enable_thinking": True,
    }

def test_extended_levels_are_forwarded_unchanged():
    """扩展思考强度应由强度字段原样传给供应商"""
    for level in ("xhigh", "max", "ultra"):
        body = _build_thinking_extra_body(level, ["reasoning_effort", "thinking_level"])
        assert body == {"reasoning_effort": level, "thinking_level": level}

def test_empty_effective_field_set_returns_none():
    """全部字段被忽略或省略时不发送 extra_body"""
    body = _build_thinking_extra_body(
        "off",
        ["reasoning_effort", "unknown"],
        omit_none_thinking_fields=True,
    )

    assert body is None

class WeatherTool(_tool_calling_Tool):
    def __init__(self):
        super().__init__(
            tool_name="get_weather",
            description="获取指定城市的天气信息",
            params_dict={
                "city": ("string", "要查询的城市名称"),
                "unit": ("string", "温度单位"),
            },
        )

    def execute(self, city: str, unit: str = "celsius") -> dict[str, Any]:
        return {"city": city, "unit": unit, "condition": "晴天", "temperature": 25}

class CalculatorTool(_tool_calling_Tool):
    def __init__(self):
        super().__init__(
            tool_name="calculate",
            description="执行基本的数学计算",
            params_dict={"expression": ("string", "数学表达式")},
        )

    def execute(self, expression: str) -> dict[str, Any]:
        try:
            operators: dict[type, Any] = {
                ast.Add: operator.add,
                ast.Sub: operator.sub,
                ast.Mult: operator.mul,
                ast.Div: getattr(operator, "truediv"),
            }
            tree = ast.parse(expression, mode="eval")

            def evaluate(node: ast.AST) -> int | float:
                if isinstance(node, ast.Expression):
                    return evaluate(node.body)
                if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                    return node.value
                if isinstance(node, ast.BinOp) and type(node.op) in operators:
                    return operators[type(node.op)](evaluate(node.left), evaluate(node.right))
                raise ValueError("不支持的表达式")

            result = evaluate(tree)
            return {"expression": expression, "result": result}
        except Exception as exc:
            return {"expression": expression, "error": str(exc)}

def test_tool_manager_registers_definitions_and_executes_calls():
    manager = _tool_calling_ToolsManager()
    manager.register_tool(WeatherTool())
    manager.register_tool(CalculatorTool())

    definitions = manager.get_tools_definitions()
    assert {item["function"]["name"] for item in definitions} == {"get_weather", "calculate"}
    weather = next(item for item in definitions if item["function"]["name"] == "get_weather")
    assert weather["function"]["parameters"]["required"] == ["city"]
    assert weather["function"]["parameters"]["additionalProperties"] is False

    missing = manager.execute_tool("calculate", {})
    unknown = manager.execute_tool("calculate", {"expression": "1", "extra": True})
    assert missing["error_type"] == unknown["error_type"] == "invalid_arguments"
    assert "expression" in missing["error"] and "extra" in unknown["error"]

    tool_message, result = manager.execute_tool_call(
        {"name": "calculate", "arguments": {"expression": "2 + 3 * 4"}},
    )
    assert tool_message["type"] == "function"
    assert tool_message["function"]["name"] == "calculate"
    assert json.loads(tool_message["function"]["arguments"]) == {"expression": "2 + 3 * 4"}
    assert result["result"] == 14

def test_create_tool_defined_and_tool_call_are_compatible():
    definition = create_tool_defined(
        tool_name="search_web",
        description="搜索信息",
        params_dict={"query": ("string", "搜索关键词")},
    )

    assert definition["function"]["name"] == "search_web"
    assert definition["function"]["parameters"]["required"] == ["query"]


def test_create_tool_defined_preserves_nested_schema() -> None:
    item_schema = {
        "type": "object",
        "properties": {"old": {"type": "string", "minLength": 1}, "new": {"type": "string"}},
        "required": ["old", "new"], "additionalProperties": False,
    }
    definition = create_tool_defined(
        "replace", "替换文本",
        {"replacements": {"type": "array", "items": item_schema, "minItems": 1}},
        required=["replacements"],
    )
    parameters = definition["function"]["parameters"]
    assert parameters["properties"]["replacements"]["items"] == item_schema
    assert parameters["properties"]["replacements"]["items"] is not item_schema
    assert parameters["additionalProperties"] is False


def test_malformed_tool_arguments_return_correction_instead_of_execution() -> None:
    response = parse_call_response({
        "choices": [{
            "finish_reason": "length",
            "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "broken", "type": "function",
                    "function": {"name": "calculate", "arguments": '{"expression":'},
                }],
            },
        }],
    })
    assert response.finish_reason == "length"
    assert response.tool_calls is not None
    call = response.tool_calls[0]
    assert "argument_error" in call

    manager = _tool_calling_ToolsManager()
    manager.register_tool(CalculatorTool())
    _, result = manager.execute_tool_call(call)
    assert result["error_type"] == "invalid_arguments"
    assert "有效的 JSON 对象" in result["error"]


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("custom_prompt", [None, "保持角色"])
def test_base_agent_prompt_is_added_only_to_request_view(enabled, custom_prompt) -> None:
    class PromptLLM:
        model = "offline"
        supports_visual_input = False
        supports_native_video_input = False

        def call(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
            self.messages = messages
            return _stream_call_LLMCallResponse(type="message", content="完成")

    llm = PromptLLM()
    agent = ModelWorkflowFramework(
        llm=cast(LLM, llm), context_id="base-prompt", system_prompt=custom_prompt, persist_context=False,
        default_agent_prompt_enabled=enabled,
    )
    assert agent.full_agent("测试", callback=False) == "完成"
    expected = "\n\n".join(part for part in (custom_prompt, BASE_AGENT_PROMPT if enabled else "") if part)
    systems = [message["content"] for message in llm.messages if message["role"] == "system"]
    assert systems == ([expected] if expected else [])
    assert BASE_AGENT_PROMPT not in str(agent.ctx.get_context())
    if custom_prompt:
        assert agent.ctx.get_context()[0] == {"role": "system", "content": custom_prompt}

def test_sync_and_async_contexts_share_summary_prompt() -> None:
    from satrap.core.utils.context.base import _ContextCore

    assert ContextManager._SUMMARY_PROMPT == _ContextCore._SUMMARY_PROMPT
    assert AsyncContextManager._SUMMARY_PROMPT == _ContextCore._SUMMARY_PROMPT


def test_safe_parse_arguments_standard_json():
    """标准 JSON 字符串直接解析"""
    assert safe_parse_arguments('{"path": "a.md", "content": "hi"}') == {
        "path": "a.md", "content": "hi",
    }

def test_safe_parse_arguments_unescaped_newline_in_value():
    """值内裸换行 (模型常见输出, 如多行 markdown) -> 修复"""
    raw = '{"path": "log.md", "content": "第一行\n第二行```markdown\n代码块```"}'
    parsed = safe_parse_arguments(raw)
    assert parsed == {"path": "log.md", "content": "第一行\n第二行```markdown\n代码块```"}

def test_safe_parse_arguments_unescaped_backslash_path():
    """Windows 路径未转义反斜杠 (如 F 盘 work 目录) -> 修复"""
    raw = '{"path": "F:\\work\\Satrap\\a.txt", "content": "x"}'
    parsed = safe_parse_arguments(raw)
    assert parsed == {"path": "F:\\work\\Satrap\\a.txt", "content": "x"}

def test_safe_parse_arguments_backslash_escape_like_dir_names():
    """路径段首字母是转义符字母 (C:\new, C:\bin) -> 按字面路径修复, 不误解码为控制字符"""
    raw = '{"path": "C:\\new\\bin\\x", "content": "y"}'
    parsed = safe_parse_arguments(raw)
    assert parsed == {"path": "C:\\new\\bin\\x", "content": "y"}

def test_safe_parse_arguments_unescaped_quote_in_value():
    """值内裸引号 (如他说"好的") -> 修复; 边界: 内容以裸引号结尾时尾引号可能丢失 (信息歧义)"""
    raw = '{"content": "他说"好的"然后走了", "path": "a.md"}'
    parsed = safe_parse_arguments(raw)
    assert parsed == {"content": "他说\"好的\"然后走了", "path": "a.md"}

def test_safe_parse_arguments_code_field_still_repaired():
    """原 code 字段修复场景回归 (含未转义引号的代码块)"""
    raw = '{"code": "print(\"hi\")\nprint(1)", "name": "demo"}'
    parsed = safe_parse_arguments(raw)
    assert parsed == {"code": 'print("hi")\nprint(1)', "name": "demo"}

def test_safe_parse_arguments_bool_and_number_values_untouched():
    """非字符串值 (布尔/数字) 不被修复逻辑破坏"""
    raw = '{"append": true, "count": 3, "content": "x"}'
    assert safe_parse_arguments(raw) == {"append": True, "count": 3, "content": "x"}

def test_safe_parse_arguments_unparseable_returns_empty():
    """完全无法解析 -> 空字典 (不抛异常)"""
    assert safe_parse_arguments("这不是 JSON") == {}

def test_safe_parse_arguments_dict_passthrough():
    """dict 输入原样返回"""
    arg = {"path": "a.md"}
    assert safe_parse_arguments(arg) is arg

@pytest.mark.parametrize("raw", ["[]", "[1, 2]", "123", "true", "false", "null", '"text"'])
def test_safe_parse_arguments_normalizes_non_dict_json_to_empty(raw: str) -> None:
    """非字典 JSON (列表/数字/布尔/null/字符串) 应归一为空字典"""
    assert safe_parse_arguments(raw) == {}

def test_safe_parse_arguments_rejects_non_string_keys() -> None:
    """含非字符串键的字典应归一为空字典"""
    assert safe_parse_arguments("{1: 2}") == {}

def test_safe_parse_arguments_literal_eval_fallback() -> None:
    """单引号 Python 字面量应经 literal_eval 兜底解析"""
    assert safe_parse_arguments("{'a': 1}") == {"a": 1}
