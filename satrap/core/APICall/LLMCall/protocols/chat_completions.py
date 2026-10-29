"""保留现有 Chat Completions 请求与解析语义"""

from collections.abc import Iterable, Mapping
from typing import cast

from ..responses import parse_call_response
from ..stream import _StreamCallAccumulator
from .base import StreamDecoder
from satrap.core.type import LLMCallResponse


class ChatCompletionsProtocol:
    """无会话状态的 Chat Completions 适配器"""

    api_type = "chat_completions"

    def build_request(self, parameters: Mapping[str, object], messages: Iterable[dict[str, object]], *,
                      thinking_body: dict[str, object] | None, thinking: str,
                      tools: list[dict[str, object]] | None, tool_choice: object,
                      stream: bool, include_usage: bool, structured: bool) -> dict[str, object]:
        """
        保持兼容接口的参数形式

        参数:
        - parameters: 已合并的生成参数
        - messages: 内部消息副本
        - thinking_body: 供应商思考字段
        - thinking: 通用思考强度, 已在 thinking_body 中处理
        - tools: 可选工具定义
        - tool_choice: 工具选择策略
        - stream: 是否流式请求
        - include_usage: 是否请求流式 usage
        - structured: 是否输出 JSON 对象

        返回:
        - Chat Completions SDK 参数
        """
        request: dict[str, object] = {**parameters, "messages": [{key: value for key, value in msg.items() if key != "protocol_payload"} for msg in messages]}
        for message in cast(list[dict[str, object]], request["messages"]):
            content = message.get("content")
            if isinstance(content, list):
                message["content"] = [{key: value for key, value in cast(dict[str, object], part).items() if key != "_satrap_media_reference"}
                                      if isinstance(part, dict) else part for part in cast(list[object], content)]
        if not structured:
            request["extra_body"] = thinking_body
        if tools is not None:
            request.update(tools=tools, tool_choice=tool_choice)
        if stream:
            request["stream"] = True
        if include_usage:
            request["stream_options"] = {"include_usage": True}
        if structured:
            request["response_format"] = {"type": "json_object"}
        return request

    def parse_response(self, response: object, suppress_error: bool = True) -> LLMCallResponse:
        """
        复用兼容接口的解析器

        参数:
        - response: SDK 或兼容供应商响应
        - suppress_error: 是否转换解析错误

        返回:
        - 项目统一响应
        """
        return parse_call_response(cast(dict[str, object], response), suppress_error)

    def new_stream_decoder(self) -> StreamDecoder:
        """
        创建兼容接口增量聚合器

        返回:
        - 本次调用独立的聚合器
        """
        return _StreamCallAccumulator()
