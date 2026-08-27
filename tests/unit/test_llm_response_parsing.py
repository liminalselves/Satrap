from typing import Any

from openai.types.chat.chat_completion import ChatCompletion

from satrap.core.APICall.LLMCall import parse_call_response


def _build_completion(message: dict[str, Any]) -> ChatCompletion:
    """构造不访问网络的 OpenAI SDK 响应对象"""
    return ChatCompletion.model_validate({
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
    })


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
