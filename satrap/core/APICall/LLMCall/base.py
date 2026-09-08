"""同步与异步模型共用的配置和请求构造"""

from typing import Any, Generic, TypeVar, List, Optional, Iterable

from satrap.core.utils import normalize_openai_base_url
from .utils import (
    _as_message_params,
    _as_tool_params,
    _as_tool_choice_param,
    _build_thinking_extra_body,
)

_ClientT = TypeVar("_ClientT")


class _LLMBase(Generic[_ClientT]):
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
        allow_insecure_base_url: bool = False,
        thinking_field_name: Optional[str] = "reasoning_content",
        thinking_fields: Optional[List[str]] = None,
        omit_none_thinking_fields: bool = False,
    ):
        """
        LLM API 共用配置初始化

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
        - allow_insecure_base_url: 是否显式允许非回环 HTTP API 地址
        - thinking_field_name: 可选参数, 用于指定思考内容的字段名称
        - thinking_fields: 可选参数, 该模型需要的思考字段列表, 如 ["reasoning_effort", "thinking.type"]
        - omit_none_thinking_fields: 关闭思考时是否省略值为 none 的字段
        """
        self.api_key = api_key if not lock_api_key else "api key locked"
        self.model = model
        self.base_url = normalize_openai_base_url(
            base_url,
            allow_insecure=allow_insecure_base_url,
        )
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.suppress_error = suppress_error
        self.return_false = return_false
        self.thinking_field_name = thinking_field_name
        self.thinking_fields = thinking_fields
        self.omit_none_thinking_fields = omit_none_thinking_fields

        self.client = self._create_client(api_key, timeout)

    def _create_client(self, api_key: str, timeout: int) -> _ClientT:
        """由入口选择同步或异步客户端"""
        raise NotImplementedError

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
        omit_none_thinking_fields: Optional[bool] = None,
    ):
        """
        更新 LLM 实例的默认参数设置

        参数:
        - model: 新的模型名称
        - temperature: 新的温度参数
        - top_p: 新的 top_p 参数
        - max_tokens: 新的最大 token 数
        - thinking_fields: 新的思考字段列表
        - omit_none_thinking_fields: 关闭思考时是否省略值为 none 的字段
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
        if omit_none_thinking_fields is not None:
            self.omit_none_thinking_fields = omit_none_thinking_fields

    def _generation_parameters(
        self,
        model: str | None,
        temperature: float | None,
        top_p: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """合并调用参数与实例默认值, 保留显式零值"""
        return {
            "model": model if model else self.model,
            "temperature": temperature if temperature is not None else self.temperature,
            "top_p": top_p if top_p is not None else self.top_p,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
        }

    def _build_request(
        self,
        parameters: dict[str, Any],
        messages: Iterable[dict[str, Any]],
        *,
        thinking: str = "off",
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        stream: bool = False,
        include_usage: bool = False,
        structured: bool = False,
    ) -> dict[str, Any]:
        """构造各类请求共用的 SDK 参数, 每次返回独立字典"""
        request = {**parameters, "messages": _as_message_params(messages)}
        if not structured:
            request["extra_body"] = _build_thinking_extra_body(
                thinking, self.thinking_fields, self.omit_none_thinking_fields
            )
        if tools is not None:
            request["tools"] = _as_tool_params(tools)
            request["tool_choice"] = _as_tool_choice_param(tool_choice)
        if stream:
            request["stream"] = True
        if include_usage:
            request["stream_options"] = {"include_usage": True}
        if structured:
            request["response_format"] = {"type": "json_object"}
        return request
