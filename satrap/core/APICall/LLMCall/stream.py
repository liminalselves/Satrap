"""同步与异步共用的流式响应累积器"""

from typing import Any, cast

from satrap.core.type import (
    LLMCallResponse,
    LLMCallStreamEvent,
    TokenUsage,
    safe_getattr,
)
from .responses import _extract_token_usage, parse_call_response


def _stream_field(value: Any, name: str, default: Any = None) -> Any:
    """
    兼容对象和字典形式读取流式响应字段 (保留原始类型, 供嵌套结构递归提取)

    参数:
    - value: 输入值
    - name: 名称
    - default: 默认值

    返回:
    - Any: 兼容对象和字典形式读取流式响应字段 (保留原始类型, 供嵌套结构递归提取)
    """
    if isinstance(value, dict):
        return cast(dict[str, Any], value).get(name, default)
    val = safe_getattr(value, name, default)
    return default if val is None else val


def _stream_text(value: Any) -> str:
    """
    提取流式字段中的文本内容

    参数:
    - value: 输入值

    返回:
    - str: 提取流式字段中的文本内容
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in cast(list[Any], value):
            text = _stream_field(part, "text", "")
            if text:
                parts.append(str(text))
        return "".join(parts)
    return "" if value is None else str(value)


def _stream_first_text(value: Any, names: tuple[str, ...]) -> str:
    """
    按优先级读取第一个非空文本字段

    参数:
    - value: 输入值
    - names: names 输入值

    返回:
    - str: 按优先级读取第一个非空文本字段
    """
    for name in names:
        text = _stream_text(_stream_field(value, name, None))
        if text:
            return text
    return ""


class _StreamCallAccumulator:
    """聚合 Chat Completions 流式响应并生成最终调用结果"""

    def __init__(self):
        """初始化 _StreamCallAccumulator"""
        self.content_parts: list[str] = []  # 最终答案内容
        self.reasoning_parts: list[str] = []  # 思考内容
        self.tool_calls: dict[int, dict[str, str]] = {}  # tools call 信息
        self.finish_reason: str | None = None  # 完成原因
        self.usage: TokenUsage | None = None  # 最终 usage 块, choices 为空时也需保留

    def consume(self, chunk: Any) -> list[LLMCallStreamEvent]:
        """
        接收一个来自 API 的原始响应块, 更新累加器状态, 并返回本次 chunk 产生的事件列表

        参数:
        - chunk: 来自 API 的原始响应块 (对象或字典)

        返回:
        - list[LLMCallStreamEvent]: 接收一个来自 API 的原始响应块, 更新累加器状态, 并返回本次 chunk 产生的事件列表
        """
        events: list[LLMCallStreamEvent] = []
        chunk_usage = _extract_token_usage(chunk)
        if chunk_usage is not None:
            self.usage = chunk_usage
        choices: list[Any] = _stream_field(chunk, "choices", []) or []

        for choice in choices:  # 遍历每个 choice
            finish_reason = _stream_field(choice, "finish_reason", None)

            if finish_reason:
                self.finish_reason = str(finish_reason)

            delta = _stream_field(choice, "delta", None)
            if delta is None:
                continue

            # ======= 处理思考内容 =======
            thinking = _stream_first_text(
                delta,
                ("reasoning_content", "reasoning", "thinking"),
            )
            if thinking:
                self.reasoning_parts.append(thinking)
                events.append(LLMCallStreamEvent(kind="thinking_delta", delta=thinking))

            # ======= 处理答案内容 =======
            content = _stream_text(_stream_field(delta, "content", None))
            if content:
                self.content_parts.append(content)
                events.append(LLMCallStreamEvent(kind="content_delta", delta=content))

            # ======= 处理 tools call =======
            tool_deltas = _stream_field(delta, "tool_calls", None)
            if tool_deltas is None:
                function_call = _stream_field(delta, "function_call", None)
                tool_deltas = [function_call] if function_call is not None else []

            # ======= 处理 tools call 增量 =======
            for fallback_index, tool_delta in enumerate(tool_deltas or []):
                index_value = _stream_field(tool_delta, "index", fallback_index)
                try:
                    index = int(index_value)
                except (TypeError, ValueError):
                    index = fallback_index

                slot = self.tool_calls.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": ""},
                )  # 获取或创建槽位

                function_data = _stream_field(tool_delta, "function", None)
                if function_data is None:
                    function_data = tool_delta
                # 提取函数信息

                id_delta = _stream_text(_stream_field(tool_delta, "id", None))
                name_delta = _stream_text(_stream_field(function_data, "name", None))
                arguments_delta = _stream_text(
                    _stream_field(function_data, "arguments", None)
                )

                if id_delta:
                    slot["id"] += id_delta
                if name_delta:
                    slot["name"] += name_delta
                if arguments_delta:
                    slot["arguments"] += arguments_delta
                # 增量字段

                if id_delta or name_delta or arguments_delta:
                    events.append(
                        LLMCallStreamEvent(
                            kind="tool_call_delta",
                            tool_call={
                                "index": index,
                                "id": id_delta,
                                "name": name_delta,
                                "arguments": arguments_delta,
                            },  # 工具调用增量
                        )
                    )
        return events

    def response(self, suppress_error: bool = True) -> LLMCallResponse:
        """
        将聚合结果转换为与非流式 call 一致的响应结构

        参数:
        - suppress_error: 是否抑制异常

        返回:
        - LLMCallResponse: 将聚合结果转换为与非流式 call 一致的响应结构
        """
        tool_calls: list[dict[str, Any]] = []
        for index in sorted(self.tool_calls):
            tool_call = self.tool_calls[index]
            tool_calls.append(
                {
                    "id": tool_call["id"],
                    "type": "function",
                    "function": {
                        "name": tool_call["name"],
                        "arguments": tool_call["arguments"] or "{}",
                    },
                }
            )

        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(self.content_parts),
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        if self.reasoning_parts:
            message["reasoning_content"] = "".join(self.reasoning_parts)

        payload: dict[str, Any] = {"choices": [{"message": message}]}
        if self.usage is not None:
            payload["usage"] = {
                "prompt_tokens": self.usage.input_tokens,
                "completion_tokens": self.usage.output_tokens,
                "total_tokens": self.usage.total_tokens,
                "cached_tokens": self.usage.cached_tokens,
            }
        return parse_call_response(payload, suppress_error=suppress_error)
