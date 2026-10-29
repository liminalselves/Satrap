"""Responses 请求, 输出项与 SSE 事件转换, 不管理远端会话状态"""

from collections.abc import Iterable, Mapping
import copy
import hashlib
import json
import re
from typing import cast

from satrap.core.type import LLMCallResponse, LLMCallStreamEvent, ModelProtocolPayload
from satrap.core.utils import parse_tool_arguments
from satrap.core.utils.model_history import replay_items
from ..responses import _extract_token_usage
from .base import ModelProtocolError, StreamDecoder


def field(value: object, name: str, default: object = None) -> object:
    """
    读取 SDK 对象或兼容 JSON 字段

    参数:
    - value: 原始对象
    - name: 字段名称
    - default: 缺少字段的默认值

    返回:
    - 原始字段或默认值
    """
    return cast(Mapping[str, object], value).get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def item_dict(value: object) -> dict[str, object]:
    """
    获取输出项的 JSON 副本

    参数:
    - value: SDK 输出项或字典

    返回:
    - 可序列化的独立字典, 非对象项直接报错
    """
    if isinstance(value, dict):
        return copy.deepcopy(cast(dict[str, object], value))
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(mode="json", exclude_none=True)
        if isinstance(result, dict):
            return cast(dict[str, object], result)
    raise ModelProtocolError("invalid_response", "模型服务返回了无法解析的输出项")


def text(value: object) -> str:
    """
    保留明确的文本字段

    参数:
    - value: 动态字段

    返回:
    - 文本值, 其他类型返回空字符串
    """
    return value if isinstance(value, str) else ""


def encode_content(content: object) -> str | list[dict[str, object]]:
    """
    转换用户输入及函数结果的多模态内容

    参数:
    - content: 已经完成路径解析和预算检查的通用内容

    返回:
    - Responses 输入文本或媒体片段, 不上传文件或执行网络请求
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        raise ModelProtocolError("invalid_request", "模型消息内容必须是文本或内容片段列表")
    parts: list[dict[str, object]] = []
    for raw in cast(list[object], content):
        part = item_dict(raw)
        kind = part.get("type")
        if kind == "text":
            parts.append({"type": "input_text", "text": text(part.get("text"))})
        elif kind == "image_url":
            image = part.get("image_url")
            url = text(field(image, "url")) if not isinstance(image, str) else image
            parts.append({"type": "input_image", "image_url": url, "detail": field(image, "detail", "auto")})
        elif kind in {"input_text", "input_image", "input_file"}:
            parts.append(part)
        else:
            raise ModelProtocolError("unsupported_media", "当前 Responses 协议未支持该媒体格式, 请使用图片抽帧兼容方式")
    return parts


class ResponsesProtocol:
    """每个 LLM 实例固定的无状态 Responses 适配器"""

    api_type = "responses"

    def __init__(self, provider: str) -> None:
        """
        保存来源摘要, 不保存地址中的潜在认证资料

        参数:
        - provider: 实际客户端 API 基址
        """
        self.provider = hashlib.sha256(provider.encode("utf-8")).hexdigest()

    def encode_messages(self, messages: Iterable[dict[str, object]], model: str) -> list[dict[str, object]]:
        """
        转换消息, 在来源一致时回放原始输出项

        参数:
        - messages: 项目内部消息副本
        - model: 当前实际模型

        返回:
        - 严格保持原始顺序的 Responses 输入项
        """
        result: list[dict[str, object]] = []
        for message in messages:
            role = message.get("role")
            if role == "assistant":
                native = replay_items(message, self.provider, model)
                if native is not None:
                    result.extend(native)
                    continue
            if role == "tool":
                result.append({"type": "function_call_output", "call_id": message.get("tool_call_id"),
                               "output": encode_content(message.get("content"))})
                continue
            if role not in {"system", "developer", "user", "assistant"}:
                raise ModelProtocolError("invalid_request", "模型消息包含不支持的角色")
            content = message.get("content")
            if content or not message.get("tool_calls"):
                result.append({"role": role, "content": encode_content(content)})
            calls = message.get("tool_calls")
            if isinstance(calls, list):
                for call in cast(list[object], calls):
                    function = field(call, "function", call)
                    arguments = field(function, "arguments", "{}")
                    result.append({"type": "function_call", "call_id": field(call, "id"),
                                   "name": field(function, "name"), "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)})
        return result

    def build_request(self, parameters: Mapping[str, object], messages: Iterable[dict[str, object]], *,
                      thinking_body: dict[str, object] | None, thinking: str,
                      tools: list[dict[str, object]] | None, tool_choice: object,
                      stream: bool, include_usage: bool, structured: bool) -> dict[str, object]:
        """
        构造本地管理历史的 Responses 请求

        参数:
        - parameters: 通用模型与生成参数
        - messages: 已准备媒体的项目消息
        - thinking_body: 兼容接口字段, 不直接透传
        - thinking: 显式思考强度
        - tools: 自定义函数定义
        - tool_choice: 工具选择策略
        - stream: 是否使用 SSE
        - include_usage: Responses 由终态提供计量, 不发送兼容接口选项
        - structured: 是否使用 JSON 对象输出

        返回:
        - Responses SDK 参数, 不包含远端会话或自动截断设置
        """
        request: dict[str, object] = {key: value for key, value in parameters.items() if value is not None and key != "max_tokens"}
        request.update(input=self.encode_messages(messages, text(parameters.get("model"))), store=False, truncation="disabled",
                       include=["reasoning.encrypted_content"])
        if parameters.get("max_tokens") is not None:
            request["max_output_tokens"] = parameters["max_tokens"]
        if thinking != "off":
            request["reasoning"] = {"effort": thinking, "summary": "auto"}
        elif thinking_body and "reasoning_effort" in thinking_body:
            request["reasoning"] = {"effort": thinking_body["reasoning_effort"]}
        if tools is not None:
            converted: list[dict[str, object]] = []
            for tool in tools:
                if tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
                    raise ModelProtocolError("invalid_request", "Responses 首版仅支持本地函数工具")
                function = cast(dict[str, object], tool["function"])
                converted.append({"type": "function", **copy.deepcopy(function), "strict": function.get("strict", False)})
            choice: object = cast(dict[str, object], tool_choice) if isinstance(tool_choice, dict) else tool_choice
            if field(choice, "type") == "function":
                choice = {"type": "function", "name": field(field(choice, "function"), "name")}
            request.update(tools=converted, tool_choice=choice)
        if stream:
            request["stream"] = True
        if structured:
            request["text"] = {"format": {"type": "json_object"}}
        return request

    def parse_response(self, response: object, suppress_error: bool = True) -> LLMCallResponse:
        """
        解析有类型的输出项并保留本次增量资料

        参数:
        - response: 原始 SDK 响应
        - suppress_error: 是否将终态错误返回为结构化响应

        返回:
        - 可展示内容, 本地工具调用和独立的协议资料
        """
        try:
            status = field(response, "status")
            if status != "completed":
                if status == "incomplete":
                    reason = field(field(response, "incomplete_details"), "reason")
                    code, message = ("output_limit", "模型输出达到上限, 本次结果未完成, 未执行工具") if reason == "max_output_tokens" else ("incomplete_response", "模型响应未完成, 未执行工具")
                    raise ModelProtocolError(code, message)
                error_code = text(field(field(response, "error"), "code"))
                error_code = error_code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) else None
                raise ModelProtocolError("provider_error" if status == "failed" else "invalid_response", "模型服务未返回完整响应", error_code)
            output = field(response, "output")
            if not isinstance(output, list):
                raise ModelProtocolError("invalid_response", "模型响应缺少输出项列表")
            items = [item_dict(item) for item in cast(list[object], output)]
            contents: list[str] = []
            summaries: list[str] = []
            calls: list[dict[str, object]] = []
            for item in items:
                kind = item.get("type")
                if kind == "reasoning":
                    summary = item.get("summary", [])
                    if isinstance(summary, list):
                        summaries.extend(text(field(part, "text")) for part in cast(list[object], summary))
                elif kind == "message":
                    content = item.get("content", [])
                    if isinstance(content, list):
                        contents.extend(text(field(part, "text")) if field(part, "type") == "output_text" else text(field(part, "refusal")) for part in cast(list[object], content))
                elif kind == "function_call":
                    if not text(item.get("call_id")) or not text(item.get("name")) or item.get("status") == "incomplete":
                        raise ModelProtocolError("invalid_response", "模型返回了不完整的工具调用")
                    arguments, error = parse_tool_arguments(text(item.get("arguments")))
                    call: dict[str, object] = {"id": item["call_id"], "name": item["name"], "arguments": arguments}
                    if error is not None:
                        call["argument_error"] = error
                    calls.append(call)
                else:
                    raise ModelProtocolError("unsupported_output", "模型返回了当前未接入的工具或输出类型")
            payload: ModelProtocolPayload = {"api_type": "responses", "provider": self.provider, "model": text(field(response, "model")),
                                             "response_id": text(field(response, "id")), "items": items}
            return LLMCallResponse(type="tools_call" if calls else "message", content="".join(contents), thinking="".join(summaries) or None,
                                   tool_calls=calls or None, usage=_extract_token_usage(response),
                                   finish_reason="tool_calls" if calls else "stop", protocol_payload=payload)
        except ModelProtocolError as error:
            if not suppress_error:
                raise
            return LLMCallResponse(type="error", content=error.info.text, error_info=error.info,
                                   usage=_extract_token_usage(response), finish_reason="length" if error.info.code == "output_limit" else "error")

    def new_stream_decoder(self) -> StreamDecoder:
        """
        创建当前请求的 SSE 解码状态

        返回:
        - 不与其他会话共享的解码器
        """
        return ResponsesStreamDecoder(self)


class ResponsesStreamDecoder:
    """正文增量用于展示, 终态响应用于执行和历史回放"""

    def __init__(self, protocol: ResponsesProtocol) -> None:
        """
        初始化独立请求状态

        参数:
        - protocol: 不可变的协议适配器
        """
        self.protocol = protocol
        self.terminal: object = None
        self.finish_reason: str | None = None
        self.calls: dict[str, int] = {}

    def consume(self, chunk: object) -> list[LLMCallStreamEvent]:
        """
        将 SSE 事件映射为通用增量, 不拼接两次完整输出

        参数:
        - chunk: 原始 SDK 事件

        返回:
        - 本事件新增的可展示动作
        """
        kind = field(chunk, "type")
        if self.terminal is not None:
            return []
        if kind in {"response.completed", "response.failed", "response.incomplete"}:
            self.terminal = field(chunk, "response")
        elif kind == "error":
            raise ModelProtocolError("provider_error", "模型服务在流式生成期间返回错误")
        elif kind == "response.output_text.delta":
            return [LLMCallStreamEvent(kind="content_delta", delta=text(field(chunk, "delta")))]
        elif kind == "response.refusal.delta":
            return [LLMCallStreamEvent(kind="content_delta", delta=text(field(chunk, "delta")))]
        elif kind == "response.reasoning_summary_text.delta":
            return [LLMCallStreamEvent(kind="thinking_delta", delta=text(field(chunk, "delta")))]
        elif kind == "response.output_item.added" and field(field(chunk, "item"), "type") == "function_call":
            item = field(chunk, "item")
            item_id = text(field(item, "id"))
            index = len(self.calls)
            self.calls[item_id] = index
            return [LLMCallStreamEvent(kind="tool_call_delta", tool_call={"index": index, "id": text(field(item, "call_id")),
                                                                         "name": text(field(item, "name")), "arguments": ""})]
        elif kind == "response.function_call_arguments.delta":
            item_id = text(field(chunk, "item_id"))
            if item_id not in self.calls:
                raise ModelProtocolError("invalid_response", "工具参数缺少对应的调用起点")
            return [LLMCallStreamEvent(kind="tool_call_delta", tool_call={"index": self.calls[item_id], "id": "", "name": "", "arguments": text(field(chunk, "delta"))})]
        return []

    def response(self, suppress_error: bool = True) -> LLMCallResponse:
        """
        从真实终态生成最终结果, 缺少终态时拒绝执行工具

        参数:
        - suppress_error: 是否转换终态错误

        返回:
        - 与非流式相同的解析结果
        """
        if self.terminal is None:
            raise ModelProtocolError("incomplete_stream", "模型响应流提前结束, 未收到完整终态, 未执行工具")
        result = self.protocol.parse_response(self.terminal, suppress_error)
        self.finish_reason = result.finish_reason
        return result
