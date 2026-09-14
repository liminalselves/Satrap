"""异步模型调用入口"""

from openai import AsyncOpenAI, APIError
from typing import (
    List,
    Dict,
    Any,
    Optional,
    Union,
    Literal,
    AsyncIterator,
    cast,
)

from satrap.core.type import LLMCallResponse, LLMCallStreamEvent, safe_getattr_str
from .responses import parse_chat_response, parse_call_response
from .stream import _StreamCallAccumulator
from .utils import (
    _stream_usage_option_unsupported,
    prepare_call_messages,
    prepare_structured_messages,
)
from .base import _LLMBase

from satrap.core.log import logger


class AsyncLLM(_LLMBase[AsyncOpenAI]):
    def _create_client(self, api_key: str, timeout: int) -> AsyncOpenAI:
        return AsyncOpenAI(api_key=api_key, base_url=self.base_url, timeout=timeout)

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
        parameters = self._generation_parameters(model, temperature, top_p, max_tokens)

        if not messages:
            logger.warning("对话输入 messages 为空")
            return "" if not self.return_false else False

        try:
            response = await self.client.chat.completions.create(
                **self._build_request(parameters, messages, thinking=thinking)
            )  # 发起网络请求并等待结果

            return parse_chat_response(
                api_response=response, suppress_error=self.suppress_error
            )

        except APIError as e:
            err_msg = f"[AsyncLLM] LLM API 返回错误: {e}"
            if not self.suppress_error:
                raise e
            logger.error(err_msg)
            return "" if not self.return_false else False

        except Exception as e:
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
        parameters = self._generation_parameters(model, temperature, top_p, max_tokens)

        if not messages:
            logger.warning("对话输入 messages 为空")
            yield "" if not self.return_false else False
            return

        try:
            stream = cast(
                Any,
                await self.client.chat.completions.create(
                    **self._build_request(
                        parameters, messages, thinking=thinking, stream=True
                    )
                ),
            )  # 发起异步流式网络请求

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
        parameters = self._generation_parameters(model, temperature, top_p, max_tokens)

        if not messages:
            logger.warning("对话输入 messages 为空")
            return "" if not self.return_false else False

        processed_messages = prepare_structured_messages(messages, format)

        try:
            response = await self.client.chat.completions.create(
                **self._build_request(parameters, processed_messages, structured=True)
            )  # 使用注入了格式提示的消息列表发起请求

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
        parameters = self._generation_parameters(model, temperature, top_p, max_tokens)

        if not messages:
            logger.warning("对话输入 messages 为空")
            return (
                LLMCallResponse(
                    type="message",
                    content="对话输入 messages 为空，请在后台日志中查看详细信息",
                )
                if not self.return_false
                else False
            )

        processed_messages = prepare_call_messages(
            messages, self.thinking_field_name, img_urls
        )

        try:
            response = await self.client.chat.completions.create(
                **self._build_request(
                    parameters,
                    processed_messages,
                    thinking=thinking,
                    tools=tools,
                    tool_choice=tool_choice,
                )
            )

            return parse_call_response(response, self.suppress_error)

        except APIError as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[AsyncLLM] LLM API 错误: {e}")
            return (
                LLMCallResponse(
                    type="message", content="LLM API 错误，请在后台日志中查看详细信息"
                )
                if not self.return_false
                else False
            )
        except Exception as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[AsyncLLM] 调用过程发生未知异常: {e}")
            return (
                LLMCallResponse(
                    type="message",
                    content="调用过程发生未知异常，请在后台日志中查看详细信息",
                )
                if not self.return_false
                else False
            )

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
        parameters = self._generation_parameters(model, temperature, top_p, max_tokens)

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
            yield LLMCallStreamEvent(
                kind="done", response=response, finish_reason="error"
            )
            return

        processed_messages = prepare_call_messages(
            messages, self.thinking_field_name, img_urls
        )
        request_params = self._build_request(
            parameters,
            processed_messages,
            thinking=thinking,
            tools=tools,
            tool_choice=tool_choice,
            stream=True,
            include_usage=True,
        )

        try:
            while True:
                accumulator = _StreamCallAccumulator()
                received_chunk = False
                try:
                    stream = cast(
                        Any, await self.client.chat.completions.create(**request_params)
                    )
                    async for chunk in stream:
                        received_chunk = True
                        for event in accumulator.consume(chunk):
                            yield event
                    break
                except APIError as error:
                    if (
                        not received_chunk
                        and "stream_options" in request_params
                        and _stream_usage_option_unsupported(error)
                    ):
                        request_params.pop("stream_options", None)
                        logger.warning(
                            "[AsyncLLM] 当前供应商不支持流式 usage, 已降级为普通流式请求"
                        )
                        continue
                    raise

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
            response = (
                False
                if self.return_false
                else LLMCallResponse(type="message", content=message)
            )

            yield LLMCallStreamEvent(kind="error", error=message, response=response)
            yield LLMCallStreamEvent(
                kind="done", response=response, finish_reason="error"
            )

        except Exception as e:
            if not self.suppress_error:
                raise e

            message = "调用过程发生未知异常，请在后台日志中查看详细信息"
            logger.error(f"[AsyncLLM] 调用过程发生未知异常: {e}")
            response = (
                False
                if self.return_false
                else LLMCallResponse(type="message", content=message)
            )

            yield LLMCallStreamEvent(kind="error", error=message, response=response)
            yield LLMCallStreamEvent(
                kind="done", response=response, finish_reason="error"
            )
