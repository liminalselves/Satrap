from typing import Any

import pytest
from openai.types.chat.chat_completion import ChatCompletion

from satrap.core.APICall.LLMCall import parse_call_response


def _build_completion(
    message: dict[str, Any],
    usage: dict[str, Any] | None = None,
) -> ChatCompletion:
    """
    构造不访问网络的 OpenAI SDK 响应对象

    参数:
    - message: 助手消息
    - usage: 可选 token usage

    返回:
    - ChatCompletion: SDK 响应对象
    """
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
    """
    普通响应应兼容常见供应商的缓存命中字段

    参数:
    - usage: 模拟供应商 usage
    - expected: 预期缓存命中 token 数
    """
    response: dict[str, Any] = {
        "choices": [{"message": {"role": "assistant", "content": "完成"}}],
        "usage": usage,
    }

    parsed = parse_call_response(response)

    assert parsed.usage is not None
    assert parsed.usage.cached_tokens == expected
