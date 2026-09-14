"""同步与异步共用的模型响应解析"""

from openai.types.chat.chat_completion import ChatCompletion
from typing import (
    Any,
    Optional,
    Dict,
    Union,
    cast,
)
import re

from satrap.core.utils import safe_parse_arguments
from satrap.core.type import (
    LLMCallResponse,
    TokenUsage,
    safe_getattr,
    safe_getattr_str,
    safe_getattr_list,
)

from satrap.core.log import logger


def _response_content_text(content: object) -> str:
    """
    将字符串或多模态响应内容规范化为纯文本

    参数:
    - content: 字符串或供应商返回的动态多模态内容

    返回:
    - 规范化后的文本; 内容结构不受支持时返回空字符串
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    text_parts: list[str] = []
    for part in cast(list[object], content):
        text: object | None = None
        if isinstance(part, dict):
            text = cast(dict[object, object], part).get("text")
        else:
            text = safe_getattr(part, "text")
        if isinstance(text, str) and text:
            text_parts.append(text)
    return "\n".join(text_parts).strip()


def _extract_thinking_from_message(
    message: Union[Any, Dict[str, Any]]
) -> Optional[str]:
    """
    从消息对象中提取思考内容, 支持多种供应商格式

    参数:
    - message: 消息内容

    支持的格式 (按优先级):
    - reasoning_content (DeepSeek, Qwen, GLM, Kimi, Fireworks, IBM)
    - reasoning (OpenRouter, Together, Perplexity, Groq parsed)
    - thinking (Cohere, 某些自定义)
    - reasoning_details (MiniMax)
    - <think> 标签包裹的内容 (Groq raw, Together 某些模型)
    - 未来可扩展

    返回:
    - Optional[str]: 从消息对象中提取思考内容, 支持多种供应商格式
    """
    # Step.1 提取 reasoning_content 字段
    if isinstance(message, dict):
        message = cast(Dict[str, Any], message)
    reasoning = safe_getattr_str(message, "reasoning_content")
    if not reasoning and isinstance(message, dict):
        reasoning = cast(Dict[str, Any], message).get("reasoning_content", "")
    if reasoning:
        return reasoning

    # Step.2 提取 reasoning 字段
    reasoning = safe_getattr_str(message, "reasoning")
    if not reasoning and isinstance(message, dict):
        reasoning = cast(Dict[str, Any], message).get("reasoning", "")
    if reasoning:
        return reasoning

    # Step.3 提取 thinking 字段
    thinking = safe_getattr_str(message, "thinking")
    if not thinking and isinstance(message, dict):
        thinking = cast(Dict[str, Any], message).get("thinking", "")
    if thinking:
        return thinking

    # Step.4 reasoning_details (数组)
    reasoning_details = safe_getattr_list(message, "reasoning_details")
    if not reasoning_details and isinstance(message, dict):
        reasoning_details = cast(Dict[str, Any], message).get("reasoning_details", [])
    if reasoning_details:
        return "\n".join([str(x) for x in reasoning_details])

    # Step.5 从 content 中提取 <think> 标签
    content = safe_getattr_str(message, "content")
    if not content and isinstance(message, dict):
        content = cast(Dict[str, Any], message).get("content", "")
    if content and isinstance(content, str):
        match = re.search(
            r"<think>(.*?)</think>", content, re.DOTALL
        )  # 匹配 <think>...</think>
        if match:
            return match.group(1).strip()

    # Step.6 退回兼容
    return None


def parse_chat_response(
    api_response: ChatCompletion | Dict[str, Any] | None,
    suppress_error: bool = True,
) -> str:
    """
    解析 LLM API 对话响应, 提取模型的回复文本

    参数:
    - api_response: API 返回的 ChatCompletion 对象或字典
    - suppress_error: 如果为 True (默认), 解析失败时返回空字符串而不是抛出异常

    返回:
    - 模型的回复文本字符串; 如果出错或无内容, 返回空字符串
    """

    # Step.1 基础有效性检查 (确保响应不为空)
    if api_response is None:
        msg = "LLM 接口响应为空"
        if suppress_error:
            logger.warning(f"[响应处理] {msg}")
            return ""
        raise ValueError(msg)

    try:
        # Step.2 尝试提取 choices (兼容对象属性访问和字典访问)
        choices = safe_getattr_list(api_response, "choices")
        if not choices and isinstance(api_response, dict):
            api_response = cast(Dict[str, Any], api_response)
            choices = api_response.get("choices", [])
        # 尝试通过属性或字典键获取 choices 列表行

        if not choices:
            logger.warning("[响应处理] LLM 接口响应中 'choices' 列表为空")
            return ""

        # Step.3 提取第一条回复的消息内容
        first_choice = choices[0]

        if hasattr(first_choice, "message"):
            content = first_choice.message.content
        elif isinstance(first_choice, dict):
            first_choice = cast(Dict[str, Any], first_choice)
            content = first_choice.get("message", {}).get("content")
        else:
            content = ""
        # 处理 Pydantic 对象或字典格式
        # 根据返回的数据类型提取 content 字段

        if content is None:
            return ""

        return _response_content_text(content)

    # Step.4 异常处理
    except Exception as e:
        logger.error(f"[响应处理] 解析 LLM 响应时发生错误: {e}")
        if suppress_error:
            return ""
        raise e


def parse_call_response(
    api_response: ChatCompletion | Dict[str, Any] | None,
    suppress_error: bool = True,
) -> LLMCallResponse:
    """
    解析 LLM API 调用响应, 判断是否包含函数调用

    参数:
    - api_response: API 返回的 ChatCompletion 对象或字典
    - suppress_error: 如果为 True (默认), 解析失败时返回空字符串而不是抛出异常

    返回:
    - 包含响应类型 (message 或 tools_call), 文本回答与函数调用参数列表 (每个元素包含 name, id, arguments)
    """
    # Step.1 基础有效性检查 (确保响应不为空)
    if api_response is None:
        msg = "LLM 接口响应为空"
        if suppress_error:
            logger.warning(f"[响应处理] {msg}")
            return LLMCallResponse(type="message", content="")
        raise ValueError(msg)

    try:
        # Step.2 尝试提取 choices (兼容对象属性访问和字典访问)
        choices = safe_getattr_list(api_response, "choices")
        if not choices and isinstance(api_response, dict):
            api_response = cast(Dict[str, Any], api_response)
            choices = api_response.get("choices", [])

        if not choices:
            logger.warning("LLM 接口响应中 'choices' 列表为空")
            return LLMCallResponse(type="message", content="")

        usage = _extract_token_usage(api_response)

        # Step.3 提取第一条回复的消息对象
        first_choice = choices[0]
        if isinstance(first_choice, dict):
            first_choice = cast(Dict[str, Any], first_choice)
            message: Any = first_choice.get("message")
        else:
            message = safe_getattr(first_choice, "message")

        if not message:
            return LLMCallResponse(type="message", content="")

        content = safe_getattr(message, "content")
        if content is None and isinstance(message, dict):
            message = cast(Dict[str, Any], message)
            content = message.get("content", "")
        text_content = _response_content_text(content)
        # 提取文本内容

        reasoning = _extract_thinking_from_message(message)
        # 提取思考内容

        # Step.4 检查是否存在工具调用 (tool_calls)
        tool_calls = safe_getattr_list(message, "tool_calls")
        if not tool_calls and isinstance(message, dict):
            message = cast(Dict[str, Any], message)
            tool_calls = message.get("tool_calls", [])

        if tool_calls:
            tool_calls_list: list[dict[str, Any]] = []

            for tool_call in tool_calls:  # 遍历所有工具调用
                call_id = safe_getattr_str(tool_call, "id")
                # 提取工具调用 id
                if not call_id and isinstance(tool_call, dict):
                    tool_call = cast(Dict[str, Any], tool_call)
                    call_id = tool_call.get("id", "")

                if isinstance(tool_call, dict):
                    tool_call = cast(Dict[str, Any], tool_call)
                    function_data: Any = tool_call.get("function")
                else:
                    function_data = safe_getattr(tool_call, "function")
                # 提取 function 对象 (兼容对象和字典)

                if function_data:
                    func_name = safe_getattr_str(function_data, "name")
                    if isinstance(function_data, dict):
                        function_data = cast(Dict[str, Any], function_data)
                        func_name = function_data.get("name", "")
                        # 提取函数名

                    args_str = safe_getattr_str(function_data, "arguments", "{}")
                    if isinstance(function_data, dict):
                        function_data = cast(Dict[str, Any], function_data)
                        args_str = function_data.get("arguments", "{}")
                        # 提取参数字符串并解析

                    # 确保参数是字符串后再进行 JSON 解析
                    if isinstance(args_str, str):
                        args_dict = safe_parse_arguments(args_str)
                    elif isinstance(args_str, dict):
                        args_dict = cast(dict[str, Any], args_str)
                    else:
                        args_dict = cast(dict[str, Any], {})

                    call_info: dict[str, Any] = {
                        "name": func_name,
                        "id": call_id,
                        "arguments": args_dict,
                    }
                    tool_calls_list.append(call_info)
                    # 封装单个工具调用信息并添加到列表

            if tool_calls_list:
                return LLMCallResponse(
                    type="tools_call",
                    content=text_content,
                    tool_calls=tool_calls_list,
                    thinking=reasoning,
                    usage=usage,
                )

        # Step.5 默认返回普通消息类型
        return LLMCallResponse(
            type="message", content=text_content, thinking=reasoning, usage=usage
        )

    # Step.6 异常处理
    except Exception as e:
        logger.error(f"[响应处理] 解析 LLM 响应时发生错误: {e}")
        if suppress_error:
            return LLMCallResponse(type="message", content="")
        raise e


def _usage_int(usage: Any, names: tuple[str, ...]) -> Optional[int]:
    """
    读取 usage 中第一个有效的整数值

    参数:
    - usage: API usage 对象或字典
    - names: 按优先级排列的字段名

    返回:
    - Optional[int]: 读取到的 token 数, 不存在时为 None
    """
    for name in names:
        value = safe_getattr(usage, name)
        if value is None and isinstance(usage, dict):
            value = cast(dict[str, Any], usage).get(name)
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_token_usage(response: Any) -> Optional[TokenUsage]:
    """
    从普通或流式响应中提取兼容 OpenAI 命名的 token 使用量

    参数:
    - response: API 响应对象或字典

    返回:
    - Optional[TokenUsage]: 规范化后的 token 使用量
    """
    usage = safe_getattr(response, "usage")
    if usage is None and isinstance(response, dict):
        usage = cast(dict[str, Any], response).get("usage")
    if usage is None:
        return None

    input_tokens = _usage_int(usage, ("prompt_tokens", "input_tokens"))
    output_tokens = _usage_int(usage, ("completion_tokens", "output_tokens"))
    total_tokens = _usage_int(usage, ("total_tokens",))
    cached_tokens = _usage_int(
        usage,
        ("cached_tokens", "prompt_cache_hit_tokens", "cache_read_input_tokens"),
    )
    if cached_tokens is None:
        for details_name in ("prompt_tokens_details", "input_tokens_details"):
            details = safe_getattr(usage, details_name)
            if details is None and isinstance(usage, dict):
                details = cast(dict[str, Any], usage).get(details_name)
            if details is not None:
                cached_tokens = _usage_int(details, ("cached_tokens",))
            if cached_tokens is not None:
                break
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    if (
        input_tokens is None
        and output_tokens is None
        and total_tokens is None
        and cached_tokens is None
    ):
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_tokens=cached_tokens,
    )
