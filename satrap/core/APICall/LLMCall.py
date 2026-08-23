"""
大语言模型 API 调用封装

提供兼容 OpenAI 接口的同步, 异步, 流式和非流式调用,
统一处理多模态消息, 思考内容, 工具调用与响应事件
"""
from typing import List, Dict, Any, Optional, Union, Literal, Iterator, AsyncIterator, cast
from satrap.core.utils import safe_parse_arguments, normalize_openai_base_url
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent, LLMConfig, safe_getattr, safe_getattr_str, safe_getattr_list, safe_getattr_dict
from satrap.core.utils.vision import normalize_chat_messages
from openai.types.chat.chat_completion import ChatCompletion
from openai import OpenAI, AsyncOpenAI, APIError
import json
import re

from satrap.core.log import logger



def _extract_thinking_from_message(
    message: Union[Any, Dict[str, Any]],
    full_response: Optional[Any] = None
) -> Optional[str]:
    """
    从消息对象中提取思考内容, 支持多种供应商格式

    参数:
    - message: 消息内容
    - full_response: full响应

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
        match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)   # 匹配 <think>...</think>
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

        if not choices or len(choices) == 0:
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

        return content.strip()

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

        if not choices or len(choices) == 0:
            logger.warning("LLM 接口响应中 'choices' 列表为空")
            return LLMCallResponse(type="message", content="")

        # Step.3 提取第一条回复的消息对象
        first_choice = choices[0]
        message = safe_getattr_dict(first_choice, "message")
        if not message and isinstance(first_choice, dict):
            first_choice = cast(Dict[str, Any], first_choice)
            message = first_choice.get("message", {})
        
        if not message:
            return LLMCallResponse(type="message", content="")

        content = safe_getattr_str(message, "content")
        if not content and isinstance(message, dict):
            message = cast(Dict[str, Any], message)
            content = message.get("content", "")
        text_content = content.strip() if content else ""
        # 提取文本内容

        reasoning = _extract_thinking_from_message(message, api_response)
        # 提取思考内容

        # Step.4 检查是否存在工具调用 (tool_calls)
        tool_calls = safe_getattr_list(message, "tool_calls")
        if not tool_calls and isinstance(message, dict):
            message = cast(Dict[str, Any], message)
            tool_calls = message.get("tool_calls", [])

        if tool_calls and len(tool_calls) > 0:
            tool_calls_list: list[dict[str, Any]] = []
            
            for tool_call in tool_calls:
                call_id = safe_getattr_str(tool_call, "id")
                # 提取工具调用 id
                if not call_id and isinstance(tool_call, dict):
                    tool_call = cast(Dict[str, Any], tool_call)
                    call_id = tool_call.get("id", "")
                
                function_data = safe_getattr_dict(tool_call, "function")
                if not function_data and isinstance(tool_call, dict):
                    tool_call = cast(Dict[str, Any], tool_call)
                    function_data = tool_call.get("function", {})
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
    
                    try:   # 确保参数是字符串后再进行 JSON 解析
                        if isinstance(args_str, str):
                            args_dict = safe_parse_arguments(args_str)
                        elif isinstance(args_str, dict):
                            args_dict = cast(dict[str, Any], args_str)
                        else:
                            args_dict = cast(dict[str, Any], {})

                    except json.JSONDecodeError:
                        logger.error(f"[响应处理] 工具调用参数 JSON 解析失败: {args_str}")
                        args_dict = {}

                    call_info: dict[str, Any] = {"name": func_name, "id": call_id, "arguments": args_dict}
                    tool_calls_list.append(call_info)
            # 遍历所有工具调用
                    # 封装单个工具调用信息并添加到列表

            if tool_calls_list:
                return LLMCallResponse(type="tools_call", content=text_content, tool_calls=tool_calls_list, thinking=reasoning)

        # Step.5 默认返回普通消息类型
        return LLMCallResponse(type="message", content=text_content, thinking=reasoning)

    # Step.6 异常处理
    except Exception as e:
        logger.error(f"[响应处理] 解析 LLM 响应时发生错误: {e}")
        if suppress_error:
            return LLMCallResponse(type="message", content="")
        raise e


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
        return messages   # 无需重命名, 直接返回原列表
    
    new_messages: list[dict[str, Any]] = []
    for msg in messages:
        new_msg = msg.copy()
        if "reasoning_content" in new_msg:
            thinking_value = new_msg.pop("reasoning_content")
            new_msg[target_field] = thinking_value
        new_messages.append(new_msg)
    return new_messages


_THINKING_FIELD_MAP: Dict[str, tuple[Any, Any]] = {
    "reasoning_effort": ("none", None),   # None = 用 thinking 原值
    "thinking.type": ("disabled", "enabled"),
    "enable_thinking": (False, True),
    "thinking_level": ("none", None),
}


def _build_thinking_extra_body(
    thinking: str,
    thinking_fields: Optional[List[str]] = None,
    reasoning_body: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    根据思考强度构造 extra_body

    参数:
    - thinking: 思考强度 (off/low/medium/high)
    - thinking_fields: 该模型需要的思考字段列表, 如 ["reasoning_effort", "thinking.type"]
    - reasoning_body: 自定义思考请求格式 (非 None 时优先使用)

    返回:
    - extra_body dict, 或 None (不传 extra_body)
    """
    if reasoning_body is not None:
        return reasoning_body
    if not thinking_fields:
        return None
    body: Dict[str, Any] = {}
    for field in thinking_fields:
        if field not in _THINKING_FIELD_MAP:
            continue
        off_val, on_val = _THINKING_FIELD_MAP[field]
        value = off_val if thinking == "off" else (on_val if on_val is not None else thinking)
        if "." in field:
            parent, child = field.split(".", 1)
            if not isinstance(body.get(parent), dict):
                body[parent] = {}
            body[parent][child] = value
        else:
            body[field] = value
    return body if body else None


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
        self.content_parts: list[str] = []   # 最终答案内容
        self.reasoning_parts: list[str] = []   # 思考内容
        self.tool_calls: dict[int, dict[str, str]] = {}   # tools call 信息
        self.finish_reason: str | None = None   # 完成原因

    def consume(self, chunk: Any) -> list[LLMCallStreamEvent]:
        """
        接收一个来自 API 的原始响应块, 更新累加器状态, 并返回本次 chunk 产生的事件列表

        参数:
        - chunk: 来自 API 的原始响应块 (对象或字典)

        返回:
        - list[LLMCallStreamEvent]: 接收一个来自 API 的原始响应块, 更新累加器状态, 并返回本次 chunk 产生的事件列表
        """
        events: list[LLMCallStreamEvent] = []
        choices: list[Any] = _stream_field(chunk, "choices", []) or []

        for choice in choices:   # 遍历每个 choice
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
                )   # 获取或创建槽位

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
                            },   # 工具调用增量
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

        payload = {"choices": [{"message": message}]}
        return parse_call_response(payload, suppress_error=suppress_error)


class LLM:
    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "put-your-model-name-here",
        temperature: float = 0.7,
        top_p: float = 0.95,
        max_tokens: int = 1000,
        timeout: int = 60,
        suppress_error: bool = True,
        return_false: bool = False,
        lock_api_key: bool = True,
        reasoning_body: Optional[Dict[str, Any]] = None,
        thinking_field_name: Optional[str] = "reasoning_content",
        thinking_fields: Optional[List[str]] = None,
    ):
        """
        [同步版本] LLM API 调用封装

        参数:
        - api_key: API 密钥
        - base_url: API 地址
        - model: 使用的模型名称
        - temperature: 生成文本的随机性 (0.0 - 2.0), 默认 0.7
        - top_p: 控制生成文本的多样性 (0.0 - 1.0), 默认 0.95
        - max_tokens: 最大生成 token 数, 默认 1000
        - timeout: 请求超时时间, 默认 60 秒
        - suppress_error: 是否抑制异常, 默认 True
        - return_false: 启用时发生错误返回 false 而非空字符串
        - lock_api_key: 是否锁定 API Key 的获取以防止泄露, 默认 True
        - reasoning_body: 可选参数, 不同 API 之间的思考请求格式不同, 默认 None
        - thinking_field_name: 可选参数, 用于指定思考内容的字段名称
        - thinking_fields: 可选参数, 该模型需要的思考字段列表, 如 ["reasoning_effort", "thinking.type"]
        """
        self.api_key = api_key if not lock_api_key else "api key locked"
        self.model = model
        self.base_url = normalize_openai_base_url(base_url)
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.suppress_error = suppress_error
        self.return_false = return_false
        self.reasoning_body = reasoning_body
        self.thinking_field_name = thinking_field_name
        self.thinking_fields = thinking_fields

        self.client = OpenAI(api_key=api_key, base_url=self.base_url, timeout=timeout)

    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        thinking: str = "off",
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Union[str, Literal[False]]:
        """
        同步发送对话请求

        参数:
        - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
        - model: 可选参数, 用于覆盖默认模型
        - thinking: 是否启用思考
        - temperature: 可选参数, 用于覆盖默认温度
        - top_p: 可选参数, 用于覆盖默认 top_p
        - max_tokens: 可选参数, 用于覆盖默认最大 token 数

        返回:
        - 模型回复的字符串内容; 如果出错, 根据配置返回空字符串或 False
        """
        target_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            return "" if not self.return_false else False

        try:
            # Step.1 同步调用 API
            response = self.client.chat.completions.create(
                model=target_model,
                messages=messages,   # type: ignore
                temperature=use_temp,
                top_p=use_top_p,
                max_tokens=use_max_tokens,
                extra_body=_build_thinking_extra_body(thinking, self.thinking_fields, self.reasoning_body),
            )   # 发起网络请求

            # Step.2 解析结果
            return parse_chat_response(response, self.suppress_error)

        except APIError as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[LLM] LLM API 错误: {e}")
            return "" if not self.return_false else False
        except Exception as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[LLM] 调用过程发生未知异常: {e}")
            return "" if not self.return_false else False

    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        thinking: str = "off",
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """
        同步流式发送对话请求 (生成器)

        参数:
        - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
        - model: 可选参数, 用于覆盖默认模型
        - thinking: 是否启用思考
        - temperature: 可选参数, 用于覆盖默认温度
        - top_p: 可选参数, 用于覆盖默认 top_p
        - max_tokens: 可选参数, 用于覆盖默认最大 token 数

        返回:
        - 生成器, 每次 yield 一个文本片段; 如果出错, 根据配置返回空字符串或 False
        """
        target_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            yield "" if not self.return_false else False
            return

        try:
            # Step.1 同步流式调用 API
            stream = cast(Any, self.client.chat.completions.create(
                model=target_model,
                messages=messages,   # type: ignore
                temperature=use_temp,
                top_p=use_top_p,
                max_tokens=use_max_tokens,
                extra_body=_build_thinking_extra_body(thinking, self.thinking_fields, self.reasoning_body),
                stream=True,
            ))   # 发起流式网络请求

            # Step.2 逐步 yield 内容
            for chunk in stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    content = safe_getattr_str(delta, "content")
                    if content:
                        yield content

        except APIError as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[LLM] LLM API 错误: {e}")
            yield "" if not self.return_false else False
        except Exception as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[LLM] 调用过程发生未知异常: {e}")
            yield "" if not self.return_false else False

    def structured_output(self,
            messages: List[Dict[str, str]],
            model: Optional[str] = None,
            temperature: Optional[float] = None,
            top_p: Optional[float] = None,
            max_tokens: Optional[int] = None,
            format: Optional[dict[str, Any]] = None,
        ) -> Union[str, Literal[False]]:
            """
            同步调用 LLM 并要求结构化输出

            参数:
            - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
            - model: 可选参数, 用于覆盖默认模型
            - temperature: 可选参数, 用于覆盖默认温度
            - top_p: 可选参数, 用于覆盖默认 top_p
            - max_tokens: 可选参数, 用于覆盖默认最大 token 数
            - format: 用于指定输出格式的字典提示, 示例如下:

            {
                "a": "str | 对参数的描述",
                "x": "int",
                "y": "float",
                "z": "bool",
                "m": {
                    "k": "str",
                    "v": "int"
                },   # object 类型
                "n": "enum"
            }

            返回:
            - 模型回复的字符串内容; 如果出错, 根据配置返回空字符串或 False
            """
            target_model = model if model else self.model
            use_temp = temperature if temperature is not None else self.temperature
            use_top_p = top_p if top_p is not None else self.top_p
            use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

            if not messages:
                logger.warning("对话输入 messages 为空")
                return "" if not self.return_false else False

            # Step.1 处理格式化提示词
            processed_messages = [m.copy() for m in messages]
            # 创建消息列表的深拷贝以避免修改原始数据

            if format:
                format_str = json.dumps(format, ensure_ascii=False, indent=2)
                system_instruction = f"\n请严格按照以下 JSON 格式输出结果, 不要包含 Markdown 标记或其他多余文本:\n{format_str}"
                
                if processed_messages and processed_messages[0].get("role") == "system":
                    processed_messages[0]["content"] += f"\n\n{system_instruction}"
                    # 如果已有 system prompt, 则追加格式要求
                else:
                    processed_messages.insert(0, {"role": "system", "content": system_instruction})
                    # 如果没有 system prompt, 则插入一条新的

            try:
                # Step.2 同步调用 API
                response = self.client.chat.completions.create(
                    model=target_model,
                    messages=processed_messages,   # type: ignore
                    temperature=use_temp,
                    top_p=use_top_p,
                    max_tokens=use_max_tokens,
                    response_format={"type": "json_object"},
                )   # 使用注入了格式提示的消息列表发起请求

                # Step.3 解析结果
                return parse_chat_response(response, self.suppress_error)

            except APIError as e:
                if not self.suppress_error:
                    raise e
                logger.error(f"[LLM] LLM API 错误: {e}")
                return "" if not self.return_false else False
            except Exception as e:
                if not self.suppress_error:
                    raise e
                logger.error(f"[LLM] 调用过程发生未知异常: {e}")
                return "" if not self.return_false else False

    def call(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        thinking: str = "off",
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        img_urls: Optional[List[str]] = None,
    ) -> LLMCallResponse | Literal[False]:
        """
        同步调用 LLM 并返回响应

        参数:
        - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
        - model: 可选参数, 用于覆盖默认模型
        - thinking: 是否要求模型进行思考, 默认为 False
        - temperature: 可选参数, 用于覆盖默认温度
        - top_p: 可选参数, 用于覆盖默认 top_p
        - max_tokens: 可选参数, 用于覆盖默认最大 token 数
        - tools: 可选参数, 工具定义列表, 用于 Function Calling
        - tool_choice: 工具选择策略, 可选 "auto", "none", 或 {"type": "function", "function": {"name": "工具名"}}
        - img_urls: 可选参数, 图片 URL 列表, 支持本地文件路径和远程 URL

        返回:
        - 包含响应类型 (message 或 tools_call), 文本回答与函数调用参数 (字典格式); 如果出错, 根据配置返回空字符串或 False
        """
        target_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            return LLMCallResponse(type="message", content="对话输入 messages 为空，请在后台日志中查看详细信息") if not self.return_false else False

        # Step.1 处理思考字段
        messages = _rename_thinking_field(messages, self.thinking_field_name)

        # Step.2 处理图片 URL, 构建多模态消息
        processed_messages = normalize_chat_messages(messages, img_urls=img_urls)

        try:
            # Step.3 同步调用 API
            if tools is not None:
                response = self.client.chat.completions.create(
                    model=target_model,
                    messages=processed_messages,   # type: ignore
                    temperature=use_temp,
                    top_p=use_top_p,
                    max_tokens=use_max_tokens,
                    extra_body=_build_thinking_extra_body(thinking, self.thinking_fields, self.reasoning_body),
                    tools=tools,   # type: ignore
                    tool_choice=tool_choice,   # type: ignore
                )   # 发起网络请求

            else:
                response = self.client.chat.completions.create(
                    model=target_model,
                    messages=processed_messages,   # type: ignore
                    temperature=use_temp,
                    top_p=use_top_p,
                    max_tokens=use_max_tokens,
                    extra_body=_build_thinking_extra_body(thinking, self.thinking_fields, self.reasoning_body),
                )   # 发起网络请求

            # Step.4 解析结果
            return parse_call_response(response, self.suppress_error)

        except APIError as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[LLM] LLM API 错误: {e}")
            return LLMCallResponse(type="message", content="LLM API 错误，请在后台日志中查看详细信息") if not self.return_false else False
        except Exception as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[LLM] 调用过程发生未知异常: {e}")
            return LLMCallResponse(type="message", content="调用过程发生未知异常，请在后台日志中查看详细信息") if not self.return_false else False

    def stream_call(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        thinking: str = "off",
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        img_urls: Optional[List[str]] = None,
    ) -> Iterator[LLMCallStreamEvent]:
        """
        同步流式调用 LLM 并返回结构化增量事件

        参数:
        - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
        - model: 可选参数, 用于覆盖默认模型
        - thinking: 是否要求模型进行思考, 默认为 False
        - temperature: 可选参数, 用于覆盖默认温度
        - top_p: 可选参数, 用于覆盖默认 top_p
        - max_tokens: 可选参数, 用于覆盖默认最大 token 数
        - tools: 可选参数, 工具定义列表, 用于 Function Calling
        - tool_choice: 工具选择策略, 可选 "auto", "none", 或 {"type": "function", "function": {"name": "工具名"}}
        - img_urls: 可选参数, 图片 URL 列表, 支持本地文件路径和远程 URL

        返回:
        - 生成器, 生成增量事件
        """

        target_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            response: LLMCallResponse | bool = (
                False
                if self.return_false
                else LLMCallResponse(
                    type="message",
                    content="对话输入 messages 为空，请在后台日志中查看详细信息",
                )
            )
            yield LLMCallStreamEvent(kind="done", response=response, finish_reason="error")
            return

        messages = _rename_thinking_field(messages, self.thinking_field_name)
        processed_messages = normalize_chat_messages(messages, img_urls=img_urls)
        request_params: dict[str, Any] = {
            "model": target_model,
            "messages": processed_messages,
            "temperature": use_temp,
            "top_p": use_top_p,
            "max_tokens": use_max_tokens,
            "extra_body": _build_thinking_extra_body(thinking, self.thinking_fields, self.reasoning_body),
            "stream": True,
        }
        if tools is not None:
            request_params["tools"] = tools
            request_params["tool_choice"] = tool_choice

        accumulator = _StreamCallAccumulator()
        try:
            stream = cast(Any, self.client.chat.completions.create(**request_params))
            for chunk in stream:
                yield from accumulator.consume(chunk)

            response = accumulator.response(self.suppress_error)
            yield LLMCallStreamEvent(
                kind="done",
                response=response,
                finish_reason=accumulator.finish_reason,
            )

        except APIError as e:
            if not self.suppress_error:
                raise e
            message = f"LLM API 错误，请在后台日志中查看详细信息"
            logger.error(f"[LLM] LLM API 错误: {e}")
            response = False if self.return_false else LLMCallResponse(type="message", content=message)

            yield LLMCallStreamEvent(kind="error", error=message, response=response)
            yield LLMCallStreamEvent(kind="done", response=response, finish_reason="error")

        except Exception as e:
            if not self.suppress_error:
                raise e

            message = "调用过程发生未知异常，请在后台日志中查看详细信息"
            logger.error(f"[LLM] 调用过程发生未知异常: {e}")
            response = False if self.return_false else LLMCallResponse(type="message", content=message)

            yield LLMCallStreamEvent(kind="error", error=message, response=response)
            yield LLMCallStreamEvent(kind="done", response=response, finish_reason="error")

    def get_model(self) -> str:
        """
        获取当前 LLM 实例使用的模型名称

        返回:
        - str: 当前 LLM 实例使用的模型名称
        """
        return self.model

    def get_api_key(self) -> str:
        """
        获取当前 LLM 实例的 API Key

        返回:
        - str: 当前 LLM 实例的 API Key
        """
        return self.api_key

    def get_base_url(self) -> Optional[str]:
        """
        获取当前 LLM 实例的 Base URL;
        如果未设置则返回 None

        返回:
        - Optional[str]: 当前 LLM 实例的 Base URL
        """
        return self.base_url
    
    def set_parameters(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        thinking_fields: Optional[List[str]] = None,
    ):
        """
        更新 LLM 实例的默认参数设置

        参数:
        - model: 新的模型名称
        - temperature: 新的温度参数
        - top_p: 新的 top_p 参数
        - max_tokens: 新的最大 token 数
        - thinking_fields: 新的思考字段列表
        """
        if model is not None:
            self.model = model
        if temperature is not None:
            self.temperature = temperature
        if top_p is not None:
            self.top_p = top_p
        if max_tokens is not None:
            self.max_tokens = max_tokens
        if thinking_fields is not None:
            self.thinking_fields = thinking_fields


class AsyncLLM:
    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "put-your-model-name-here",
        temperature: float = 0.7,
        top_p: float = 0.95,
        max_tokens: int = 1000,
        timeout: int = 60,
        suppress_error: bool = True,
        return_false: bool = False,
        lock_api_key: bool = True,
        reasoning_body: Optional[Dict[str, Any]] = None,
        thinking_field_name: Optional[str] = "reasoning_content",
        thinking_fields: Optional[List[str]] = None,
    ):
        """
        [异步版本] LLM API 调用封装

        参数:
        - api_key: API 密钥
        - base_url: API 地址
        - model: 模型名称
        - temperature: 生成文本的随机性 (0.0 - 2.0), 默认 0.7
        - top_p: 控制生成文本的多样性 (0.0 - 1.0), 默认 0.95
        - max_tokens: 最大生成 token 数, 默认 1000
        - timeout: 请求超时时间, 默认 60 秒
        - suppress_error: 是否抑制 API 调用中的异常, 默认 True
        - return_false: 启用时发生错误返回 false 而非空字符串
        - lock_api_key: 是否锁定 API Key 的获取以防止泄露, 默认 True
        - reasoning_body: 可选参数, 不同 API 之间的思考请求格式不同, 默认 None
        - thinking_field_name: 思考字段名称, 默认 "reasoning_content"
        - thinking_fields: 可选参数, 该模型需要的思考字段列表, 如 ["reasoning_effort", "thinking.type"]
        """
        self.api_key = api_key if not lock_api_key else "api key locked"
        self.model = model
        self.base_url = normalize_openai_base_url(base_url)
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.suppress_error = suppress_error
        self.return_false = return_false
        self.reasoning_body = reasoning_body
        self.thinking_field_name = thinking_field_name
        self.thinking_fields = thinking_fields

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=timeout,
        )   # 初始化异步 OpenAI 客户端

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        thinking: str = "off",
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Union[str, Literal[False]]:
        """
        调用 LLM 进行对话生成

        参数:
        - messages: 对话历史列表, 格式如 [{"role": "user", "content": "..."}]
        - model: 临时覆盖初始化时的模型名称
        - thinking: 是否启用思考
        - temperature: 覆盖初始化时的温度参数
        - top_p: 覆盖初始化时的 top_p 参数
        - max_tokens: 覆盖初始化时的最大生成 token 数

        返回:
        - 助手回复的文本内容; 如果出错且 return_false 为 True, 则返回 False
        """
        # Step.1 准备调用参数 (使用默认值或覆盖值)
        use_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            return "" if not self.return_false else False

        try:
            # Step.2 异步调用 OpenAI 接口
            response = await self.client.chat.completions.create(
                model=use_model,
                messages=messages,   # type: ignore
                temperature=use_temp,
                top_p=use_top_p,
                max_tokens=use_max_tokens,
                extra_body=_build_thinking_extra_body(thinking, self.thinking_fields, self.reasoning_body),
            )   # 发起网络请求并等待结果

            # Step.3 解析并返回结果
            return parse_chat_response(
                api_response=response,
                suppress_error=self.suppress_error
            )

        except APIError as e:
            # Step.4 API 层面错误的特定处理
            err_msg = f"[AsyncLLM] LLM API 返回错误: {e}"
            if not self.suppress_error:
                raise e
            logger.error(err_msg)
            return ""

        except Exception as e:
            # Step.5 其他未知异常处理 (如网络连接失败)
            err_msg = f"[AsyncLLM] 调用过程发生未知异常: {e}"
            if not self.suppress_error:
                raise e
            logger.error(err_msg)
            return "" if not self.return_false else False

    async def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        thinking: str = "off",
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """
        异步流式发送对话请求 (异步生成器)

        参数:
        - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
        - model: 可选参数, 用于覆盖默认模型
        - thinking: 是否启用思考
        - temperature: 可选参数, 用于覆盖默认温度
        - top_p: 可选参数, 用于覆盖默认 top_p
        - max_tokens: 可选参数, 用于覆盖默认最大 token 数

        返回:
        - 异步生成器, 每次 yield 一个文本片段; 如果出错, 根据配置返回空字符串或 False
        """
        target_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            yield "" if not self.return_false else False
            return

        try:
            # Step.1 异步流式调用 API
            stream = cast(Any, await self.client.chat.completions.create(
                model=target_model,
                messages=messages,   # type: ignore
                temperature=use_temp,
                top_p=use_top_p,
                max_tokens=use_max_tokens,
                extra_body=_build_thinking_extra_body(thinking, self.thinking_fields, self.reasoning_body),
                stream=True,
            ))   # 发起异步流式网络请求

            # Step.2 逐步 yield 内容
            async for chunk in stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    content = safe_getattr_str(delta, "content")
                    if content:
                        yield content

        except APIError as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[AsyncLLM] LLM API 错误: {e}")
            yield "" if not self.return_false else False
        except Exception as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[AsyncLLM] 调用过程发生未知异常: {e}")
            yield "" if not self.return_false else False

    async def structured_output(
            self,
            messages: List[Dict[str, str]],
            model: Optional[str] = None,
            temperature: Optional[float] = None,
            top_p: Optional[float] = None,
            max_tokens: Optional[int] = None,
            format: Optional[dict[str, Any]] = None,
        ) -> Union[str, Literal[False]]:
            """
            异步调用 LLM 并要求结构化输出

            参数:
            - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
            - model: 可选参数, 用于覆盖默认模型
            - temperature: 可选参数, 用于覆盖默认温度
            - top_p: 可选参数, 用于覆盖默认 top_p
            - max_tokens: 可选参数, 用于覆盖默认最大 token 数
            - format: 用于指定输出格式的字典提示, 示例如下:

            {
                "a": "str | 对参数的描述",
                "x": "int",
                "y": "float",
                "z": "bool",
                "m": {
                    "k": "str",
                    "v": "int"
                },   # object 类型
                "n": "enum"
            }

            返回:
            - 模型回复的字符串内容; 如果出错, 根据配置返回空字符串或 False
            """
            target_model = model if model else self.model
            use_temp = temperature if temperature is not None else self.temperature
            use_top_p = top_p if top_p is not None else self.top_p
            use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

            if not messages:
                logger.warning("对话输入 messages 为空")
                return "" if not self.return_false else False

            # Step.1 处理格式化提示词
            processed_messages = [m.copy() for m in messages]
            # 创建消息列表的深拷贝以避免修改原始数据

            if format:
                format_str = json.dumps(format, ensure_ascii=False, indent=2)
                system_instruction = f"\n请严格按照以下 JSON 格式输出结果, 不要包含 Markdown 标记或其他多余文本:\n{format_str}"
                
                if processed_messages and processed_messages[0].get("role") == "system":
                    processed_messages[0]["content"] += f"\n\n{system_instruction}"
                    # 如果已有 system prompt, 则追加格式要求
                else:
                    processed_messages.insert(0, {"role": "system", "content": system_instruction})
                    # 如果没有 system prompt, 则插入一条新的

            try:
                # Step.2 异步调用 API
                response = await self.client.chat.completions.create(
                    model=target_model,
                    messages=processed_messages,   # type: ignore
                    temperature=use_temp,
                    top_p=use_top_p,
                    max_tokens=use_max_tokens,
                    response_format={"type": "json_object"},
                )   # 使用注入了格式提示的消息列表发起请求

                # Step.3 解析结果
                return parse_chat_response(response, self.suppress_error)

            except APIError as e:
                if not self.suppress_error:
                    raise e
                logger.error(f"[AsyncLLM] LLM API 错误: {e}")
                return "" if not self.return_false else False
            except Exception as e:
                if not self.suppress_error:
                    raise e
                logger.error(f"[AsyncLLM] 调用过程发生未知异常: {e}")
                return "" if not self.return_false else False

    async def call(
        self,
        messages: List[Dict[str, str | List[Dict[str, Any]]]],
        model: Optional[str] = None,
        thinking: str = "off",
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        img_urls: Optional[List[str]] = None,
    ) -> LLMCallResponse | Literal[False]:
        """
        异步调用 LLM 并返回响应

        参数:
        - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
        - model: 可选参数, 用于覆盖默认模型
        - thinking: 是否要求模型进行思考, 默认为 False
        - temperature: 可选参数, 用于覆盖默认温度
        - top_p: 可选参数, 用于覆盖默认 top_p
        - max_tokens: 可选参数, 用于覆盖默认最大 token 数
        - tools: 可选参数, 工具定义列表, 用于 Function Calling
        - tool_choice: 工具选择策略, 可选 "auto", "none", 或 {"type": "function", "function": {"name": "工具名"}}
        - img_urls: 可选参数, 图片 URL 列表, 支持本地文件路径和远程 URL

        返回:
        - 包含响应类型 (message 或 tools_call), 文本回答与函数调用参数 (字典格式); 如果出错, 根据配置返回空字符串或 False
        """
        target_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            return LLMCallResponse(type="message", content="对话输入 messages 为空，请在后台日志中查看详细信息") if not self.return_false else False

        # Step.1 处理思考字段
        messages = _rename_thinking_field(messages, self.thinking_field_name)

        # Step.2 处理图片 URL, 构建多模态消息
        processed_messages = normalize_chat_messages(messages, img_urls=img_urls)

        try:
            # Step.3 异步调用 API
            if tools is not None:
                response = await self.client.chat.completions.create(
                    model=target_model,
                    messages=processed_messages,   # type: ignore
                    temperature=use_temp,
                    top_p=use_top_p,
                    max_tokens=use_max_tokens,
                    extra_body=_build_thinking_extra_body(thinking, self.thinking_fields, self.reasoning_body),
                    tools=tools,   # type: ignore
                    tool_choice=tool_choice,   # type: ignore
                )   # 发起异步网络请求

            else:
                response = await self.client.chat.completions.create(
                    model=target_model,
                    messages=processed_messages,   # type: ignore
                    temperature=use_temp,
                    top_p=use_top_p,
                    max_tokens=use_max_tokens,
                    extra_body=_build_thinking_extra_body(thinking, self.thinking_fields, self.reasoning_body),
                )   # 发起异步网络请求

            # Step.4 解析结果
            return parse_call_response(response, self.suppress_error)

        except APIError as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[AsyncLLM] LLM API 错误: {e}")
            return LLMCallResponse(type="message", content="LLM API 错误，请在后台日志中查看详细信息") if not self.return_false else False
        except Exception as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[AsyncLLM] 调用过程发生未知异常: {e}")
            return LLMCallResponse(type="message", content="调用过程发生未知异常，请在后台日志中查看详细信息") if not self.return_false else False

    async def stream_call(
        self,
        messages: List[Dict[str, str | List[Dict[str, Any]]]],
        model: Optional[str] = None,
        thinking: str = "off",
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        img_urls: Optional[List[str]] = None,
    ) -> AsyncIterator[LLMCallStreamEvent]:
        """
        异步流式调用 LLM 并返回结构化增量事件

        参数:
        - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
        - model: 可选参数, 用于覆盖默认模型
        - thinking: 是否要求模型进行思考, 默认为 False
        - temperature: 可选参数, 用于覆盖默认温度
        - top_p: 可选参数, 用于覆盖默认 top_p
        - max_tokens: 可选参数, 用于覆盖默认最大 token 数
        - tools: 可选参数, 工具定义列表, 用于 Function Calling
        - tool_choice: 工具选择策略, 可选 "auto", "none", 或 {"type": "function", "function": {"name": "工具名"}}
        - img_urls: 可选参数, 图片 URL 列表, 支持本地文件路径和远程 URL

        返回:
        - 异步生成器, 生成增量事件
        """
        target_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            response: LLMCallResponse | bool = (
                False
                if self.return_false
                else LLMCallResponse(
                    type="message",
                    content="对话输入 messages 为空，请在后台日志中查看详细信息",
                )
            )
            yield LLMCallStreamEvent(kind="done", response=response, finish_reason="error")
            return

        messages = _rename_thinking_field(messages, self.thinking_field_name)
        processed_messages = normalize_chat_messages(messages, img_urls=img_urls)
        request_params: dict[str, Any] = {
            "model": target_model,
            "messages": processed_messages,
            "temperature": use_temp,
            "top_p": use_top_p,
            "max_tokens": use_max_tokens,
            "extra_body": _build_thinking_extra_body(thinking, self.thinking_fields, self.reasoning_body),
            "stream": True,
        }
        if tools is not None:
            request_params["tools"] = tools
            request_params["tool_choice"] = tool_choice

        accumulator = _StreamCallAccumulator()
        try:
            stream = cast(Any, await self.client.chat.completions.create(**request_params))
            async for chunk in stream:
                for event in accumulator.consume(chunk):
                    yield event

            response = accumulator.response(self.suppress_error)
            yield LLMCallStreamEvent(
                kind="done",
                response=response,
                finish_reason=accumulator.finish_reason,
            )

        except APIError as e:
            if not self.suppress_error:
                raise e

            message = "LLM API 错误，请在后台日志中查看详细信息"
            logger.error(f"[AsyncLLM] LLM API 错误: {e}")
            response = False if self.return_false else LLMCallResponse(type="message", content=message)

            yield LLMCallStreamEvent(kind="error", error=message, response=response)
            yield LLMCallStreamEvent(kind="done", response=response, finish_reason="error")

        except Exception as e:
            if not self.suppress_error:
                raise e

            message = "调用过程发生未知异常，请在后台日志中查看详细信息"
            logger.error(f"[AsyncLLM] 调用过程发生未知异常: {e}")
            response = False if self.return_false else LLMCallResponse(type="message", content=message)

            yield LLMCallStreamEvent(kind="error", error=message, response=response)
            yield LLMCallStreamEvent(kind="done", response=response, finish_reason="error")

    def get_model(self) -> str:
        """
        获取当前 AsyncLLM 实例使用的模型名称

        返回:
        - str: 当前 AsyncLLM 实例使用的模型名称
        """
        return self.model

    def get_api_key(self) -> str:
        """
        获取当前 AsyncLLM 实例的 API Key

        返回:
        - str: 当前 AsyncLLM 实例的 API Key
        """
        return self.api_key
    
    def get_base_url(self) -> Optional[str]:
        """
        获取当前 AsyncLLM 实例的 Base URL;
        如果未设置则返回 None

        返回:
        - Optional[str]: 当前 AsyncLLM 实例的 Base URL
        """
        return self.base_url
    
    def set_parameters(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        thinking_fields: Optional[List[str]] = None,
    ):
        """
        更新 AsyncLLM 实例的默认参数设置

        参数:
        - model: 新的模型名称
        - temperature: 新的温度参数
        - top_p: 新的 top_p 参数
        - max_tokens: 新的最大 token 数
        - thinking_fields: 新的思考字段列表
        """
        if model is not None:
            self.model = model
        if temperature is not None:
            self.temperature = temperature
        if top_p is not None:
            self.top_p = top_p
        if max_tokens is not None:
            self.max_tokens = max_tokens
        if thinking_fields is not None:
            self.thinking_fields = thinking_fields


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


def build_llm_from_config(cfg: LLMConfig, *, async_: bool = False) -> "LLM | AsyncLLM":
    """
    由 LLMConfig 统一构造 LLM / AsyncLLM (字段映射 + 输出预算)

    统一映射全部字段并应用输出预算, 消除各调用方自行构造时的遗漏:
    - 输出预算 = context_window x (1 - history_ratio), 两者都配置时优先于 max_tokens
    - max_tokens = 输出预算 or cfg.max_tokens or 4096
    - top_p / lock_api_key / reasoning_body / thinking_field_name / thinking_fields 全部透传
    - 用 safe_getattr 兼容测试替身 (SimpleNamespace 可能缺字段)

    参数:
    - cfg: LLMConfig (或含同名字段的替身对象)
    - async_: True 返回 AsyncLLM, False 返回 LLM

    返回:
    - 'LLM | AsyncLLM': 由 LLMConfig 统一构造 LLM / AsyncLLM (字段映射 + 输出预算)
    """
    cls = AsyncLLM if async_ else LLM
    kwargs: dict[str, Any] = {
        "api_key": safe_getattr_str(cfg, "api_key"),
        "base_url": safe_getattr_str(cfg, "base_url"),
        "model": safe_getattr_str(cfg, "model"),
        "lock_api_key": safe_getattr(cfg, "lock_api_key", True),
    }
    # 可选字段: 仅非 None 时透传, 避免覆盖 LLM 类默认值 (temperature=0 也需保留)
    temperature = safe_getattr(cfg, "temperature")
    if temperature is not None:
        kwargs["temperature"] = temperature
    budget = _compute_output_budget(cfg)
    if budget is not None:
        kwargs["max_tokens"] = budget
    else:
        max_tokens = safe_getattr(cfg, "max_tokens")
        kwargs["max_tokens"] = max_tokens if max_tokens is not None else 4096
    for name in ("top_p", "reasoning_body", "thinking_field_name", "thinking_fields"):
        val = safe_getattr(cfg, name)
        if val is not None:
            kwargs[name] = val
    return cls(**kwargs)
