"""请求预处理, SDK 类型边界与配置预算"""

from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionToolUnionParam,
    ChatCompletionToolChoiceOptionParam,
)
from typing import Any, Dict, List, Optional, Iterable, cast
from openai import APIError
import json

from satrap.core.utils.vision import normalize_chat_messages
from satrap.core.type import safe_getattr

_THINKING_FIELD_MAP: Dict[str, tuple[Any, Any]] = {
    "reasoning_effort": ("none", None),  # 空值表示沿用 thinking 原值
    "thinking.type": ("disabled", "enabled"),
    "enable_thinking": (False, True),
    "thinking_level": ("none", None),
}


def _as_message_params(
    messages: Iterable[Dict[str, Any]]
) -> Iterable[ChatCompletionMessageParam]:
    """
    将项目消息列表转换为 SDK 的 messages 参数类型

    项目消息是 SDK ChatCompletionMessageParam 的结构超集
    (含 reasoning_content, thinking 等自定义键, 运行时由兼容 API 透传),
    无法在类型层声明等价, 边界断言集中在此函数

    参数:
    - messages: 项目内部消息列表

    返回:
    - Iterable[ChatCompletionMessageParam]: 可直接传入 SDK create() 的 messages 参数
    """
    return cast(Iterable[ChatCompletionMessageParam], messages)


def _as_tool_params(
    tools: List[Dict[str, Any]]
) -> Iterable[ChatCompletionToolUnionParam]:
    """
    将项目工具定义列表转换为 SDK 的 tools 参数类型

    参数:
    - tools: 项目内部工具定义列表

    返回:
    - Iterable[ChatCompletionToolUnionParam]: 可直接传入 SDK create() 的 tools 参数
    """
    return cast(Iterable[ChatCompletionToolUnionParam], tools)


def _as_tool_choice_param(tool_choice: str) -> ChatCompletionToolChoiceOptionParam:
    """
    将工具选择配置转换为 SDK 的 tool_choice 参数类型

    参数:
    - tool_choice: 工具选择配置字符串

    返回:
    - ChatCompletionToolChoiceOptionParam: 可直接传入 SDK create() 的 tool_choice 参数
    """
    return cast(ChatCompletionToolChoiceOptionParam, tool_choice)


def _stream_usage_option_unsupported(error: APIError) -> bool:
    """
    判断供应商是否明确拒绝 stream_options.include_usage

    参数:
    - error: OpenAI SDK API 异常

    返回:
    - bool: 是否可在尚未收到响应块时安全降级重试
    """
    message = str(error).lower()
    option_named = "stream_options" in message or "include_usage" in message
    rejected = any(
        word in message
        for word in ("unknown", "unsupported", "unrecognized", "invalid", "not support")
    )
    return option_named and rejected


def _rename_thinking_field(
    messages: List[Dict[str, Any]],
    target_field: str | None = None,
) -> List[Dict[str, Any]]:
    """
    将消息中的 'reasoning_content' 字段重命名为 target_field
    如果 target_field 就是 'reasoning_content', 则不做任何操作

    参数:
    - messages: 输入的消息列表
    - target_field: 目标字段名称, 用于替换 'reasoning_content'

    返回:
    - List[Dict[str, Any]]: 将消息中的 'reasoning_content' 字段重命名为 target_field
    """
    if target_field == "reasoning_content" or target_field is None:
        return messages  # 无需重命名, 直接返回原列表

    new_messages: list[dict[str, Any]] = []
    for msg in messages:
        new_msg = msg.copy()
        if "reasoning_content" in new_msg:
            thinking_value = new_msg.pop("reasoning_content")
            new_msg[target_field] = thinking_value
        new_messages.append(new_msg)
    return new_messages


def _build_thinking_extra_body(
    thinking: str,
    thinking_fields: Optional[List[str]] = None,
    omit_none_thinking_fields: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    根据思考强度构造 extra_body

    参数:
    - thinking: 思考强度, off 表示关闭, 其他值按供应商约定原样传递
    - thinking_fields: 该模型需要的思考字段列表, 如 ["reasoning_effort", "thinking.type"]
    - omit_none_thinking_fields: 关闭思考时是否省略值为 none 的字段

    返回:
    - extra_body dict, 或 None (不传 extra_body)
    """
    if not thinking_fields:
        return None
    body: Dict[str, Any] = {}
    for field in thinking_fields:
        if field not in _THINKING_FIELD_MAP:
            continue
        off_val, on_val = _THINKING_FIELD_MAP[field]
        value = (
            off_val
            if thinking == "off"
            else (on_val if on_val is not None else thinking)
        )
        if thinking == "off" and omit_none_thinking_fields and value == "none":
            continue
        if "." in field:
            parent, child = field.split(".", 1)
            if not isinstance(body.get(parent), dict):
                body[parent] = {}
            body[parent][child] = value
        else:
            body[field] = value
    return body if body else None


def _compute_output_budget(cfg: Any) -> int | None:
    """
    计算输出预算 = context_window x (1 - history_ratio) (两者都配置时生效)

    参数:
    - cfg: 配置对象

    返回:
    - int | None: 计算输出预算 = context_window x (1 - history_ratio) (两者都配置时生效)
    """
    cw = safe_getattr(cfg, "context_window")
    hr = safe_getattr(cfg, "history_ratio")
    if cw and hr:
        return int(cw * (1 - hr))
    return None


def prepare_call_messages(
    messages: list[dict[str, Any]],
    thinking_field_name: str | None,
    img_urls: list[str] | None,
) -> list[dict[str, Any]]:
    """统一思考字段与多模态消息预处理"""
    return normalize_chat_messages(
        _rename_thinking_field(messages, thinking_field_name), img_urls=img_urls
    )


def prepare_structured_messages(
    messages: list[dict[str, str]], format: dict[str, Any] | None
) -> list[dict[str, str]]:
    """复制消息并注入结构化输出要求"""
    processed = [message.copy() for message in messages]
    if format:
        format_str = json.dumps(format, ensure_ascii=False, indent=2)
        instruction = f"\n请严格按照以下 JSON 格式输出结果, 不要包含 Markdown 标记或其他多余文本:\n{format_str}"
        if processed and processed[0].get("role") == "system":
            processed[0]["content"] += f"\n\n{instruction}"
        else:
            processed.insert(0, {"role": "system", "content": instruction})
    return processed
