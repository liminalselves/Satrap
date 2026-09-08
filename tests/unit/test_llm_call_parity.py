"""验证包拆分后同步与异步入口的请求及结果契约"""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from openai import APIError

from satrap import LLM, AsyncLLM
from satrap.core.APICall.LLMCall import sync, async_
from satrap.core.type import LLMCallResponse


class _AsyncStream:
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
        return _AsyncStream() if kwargs.get("stream") else _response()

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
    kwargs = dict(
        api_key="test", model="默认模型", thinking_fields=["enable_thinking"], **options
    )
    return LLM(**kwargs), AsyncLLM(**kwargs), calls


async def _invoke(llm, method, messages, **kwargs):
    result = getattr(llm, method)(messages, **kwargs)
    if method.startswith("stream_"):
        return (
            [event async for event in result]
            if isinstance(llm, AsyncLLM)
            else list(result)
        )
    return await result if isinstance(llm, AsyncLLM) else result


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
        assert isinstance(result, LLMCallResponse) and result.content == "结果"
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


@pytest.mark.parametrize(
    "method", ["chat", "call", "structured_output", "stream_chat", "stream_call"]
)
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
