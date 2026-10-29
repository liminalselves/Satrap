"""同步与异步模型共用的配置和请求构造"""

from typing import Any, Generic, TypeVar, List, Optional, Iterable, Literal, cast

from satrap.core.utils import normalize_openai_base_url
from .utils import _build_thinking_extra_body
from .errors import log_model_error, model_error_info
from satrap.core.type import LLMErrorInfo, LLMCallResponse
from .protocols import select_protocol
from .utils import prepare_call_messages

_ClientT = TypeVar("_ClientT")


class _LLMBase(Generic[_ClientT]):
    model_config_name: str = ""
    model_context_window: int | None = None
    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "put-your-model-name-here",
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int = 1000,
        timeout: float = 60,
        suppress_error: bool = True,
        return_false: bool = False,
        lock_api_key: bool = True,
        allow_insecure_base_url: bool = False,
        thinking_field_name: Optional[str] = "reasoning_content",
        thinking_fields: Optional[List[str]] = None,
        omit_none_thinking_fields: bool = False,
        supports_visual_input: bool = False,
        supports_native_video_input: bool = False,
        video_frame_interval: float = 2.0,
        *, api_type: str = "chat_completions",
    ):
        """
        LLM API 共用配置初始化

        参数:
        - api_key: API 密钥
        - base_url: API 地址
        - model: 使用的模型名称
        - temperature: 生成随机性, 兼容协议默认 0.7, Responses 默认不发送
        - top_p: 采样比例, 兼容协议默认 0.95, Responses 默认不发送
        - max_tokens: 最大生成 token 数, 默认 1000
        - timeout: SDK 网络读写超时秒数, 默认 60, 不是完整流式生成的总限时
        - suppress_error: 是否抑制异常, 默认 True
        - return_false: 启用时发生错误返回 false 而非空字符串
        - lock_api_key: 是否锁定 API Key 的获取以防止泄露, 默认 True
        - allow_insecure_base_url: 是否显式允许非回环 HTTP API 地址
        - thinking_field_name: 可选参数, 用于指定思考内容的字段名称
        - thinking_fields: 可选参数, 该模型需要的思考字段列表, 如 ["reasoning_effort", "thinking.type"]
        - omit_none_thinking_fields: 关闭思考时是否省略值为 none 的字段
        - supports_visual_input: 是否支持图片输入, 默认 False
        - supports_native_video_input: 是否支持当前原生视频请求格式, 默认 False
        - video_frame_interval: 动画及兼容视频抽帧间隔秒数, 默认 2.0
        - api_type: 初始化时固定的请求协议, 默认 chat_completions
        """
        self.api_key = api_key if not lock_api_key else "api key locked"
        self.model = model
        self.base_url = normalize_openai_base_url(
            base_url,
            allow_insecure=allow_insecure_base_url,
        )
        self.protocol = select_protocol(api_type, str(self.base_url or "https://api.openai.com/v1"))
        self.temperature = 0.7 if temperature is None and api_type == "chat_completions" else temperature
        self.top_p = 0.95 if top_p is None and api_type == "chat_completions" else top_p
        self.max_tokens = max_tokens
        self.suppress_error = suppress_error
        self.return_false = return_false
        self.thinking_field_name = thinking_field_name
        self.thinking_fields = thinking_fields
        self.omit_none_thinking_fields = omit_none_thinking_fields

        if not isinstance(supports_visual_input, bool):
            raise ValueError("supports_visual_input 必须是布尔值")
        self.supports_visual_input = supports_visual_input
        from satrap.core.utils.media_config import validate_video_settings
        validate_video_settings(supports_native_video_input, video_frame_interval)
        self.supports_native_video_input = supports_native_video_input
        self.video_frame_interval = video_frame_interval

        self.client = self._create_client(api_key, timeout)
        if api_type == "responses":
            actual_url = str(getattr(self.client, "base_url", self.base_url or "https://api.openai.com/v1")).rstrip("/")
            self.protocol = select_protocol(api_type, actual_url)
        self.network_timeout = timeout

    def _report_error(self, error: Exception) -> LLMErrorInfo:
        """
        记录脱敏诊断并按调用方配置保留异常传播

        参数:
        - error: 本次请求的终态异常

        返回:
        - 可用于所有响应形式的结构化错误
        """
        info = model_error_info(error, timeout=self.network_timeout)
        log_model_error(info, self.model, error)
        if not self.suppress_error:
            raise error
        return info

    def _create_client(self, api_key: str, timeout: float) -> _ClientT:
        """由入口选择同步或异步客户端"""
        raise NotImplementedError

    @property
    def api_type(self) -> str:
        """
        读取初始化时选定的协议

        返回:
        - 协议名称, 切换协议应构造新实例
        """
        return self.protocol.api_type

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
        supports_visual_input: Optional[bool] = None,
        supports_native_video_input: Optional[bool] = None,
        video_frame_interval: Optional[float] = None,
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
        - supports_visual_input: 是否支持图片输入, None 表示保持原值
        - supports_native_video_input: 是否支持原生视频, None 表示保持原值
        - video_frame_interval: 抽帧间隔秒数, None 表示保持原值
        """
        from satrap.core.utils.media_config import validate_video_settings
        native = getattr(self, "supports_native_video_input", False) if supports_native_video_input is None else supports_native_video_input
        interval = getattr(self, "video_frame_interval", 2.0) if video_frame_interval is None else video_frame_interval
        validate_video_settings(native, interval)
        if supports_visual_input is not None and not isinstance(supports_visual_input, bool):
            raise ValueError("supports_visual_input 必须是布尔值")
        self.supports_native_video_input = native
        self.video_frame_interval = interval
        if supports_visual_input is not None:
            self.supports_visual_input = supports_visual_input
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
        """
        由初始化时选择的协议构造独立请求

        参数:
        - parameters: 已合并的生成参数
        - messages: 项目内部消息副本
        - thinking: 显式思考强度
        - tools: 可选工具定义
        - tool_choice: 工具选择策略
        - stream: 是否流式请求
        - include_usage: 是否请求兼容接口的流式计量
        - structured: 是否输出 JSON 对象

        返回:
        - 对应 SDK 端点的参数
        """
        thinking_body = _build_thinking_extra_body(thinking, self.thinking_fields, self.omit_none_thinking_fields) if self.api_type == "chat_completions" else None
        # 旧供应商思考字段只参与 Chat Completions, Responses 使用原生推理参数
        return self.protocol.build_request(parameters, messages, thinking_body=thinking_body, thinking=thinking,
            tools=tools, tool_choice=tool_choice, stream=stream, include_usage=include_usage, structured=structured)

    def _prepare_messages(self, messages: list[dict[str, Any]], img_urls: list[str] | None,
                          video_urls: list[str] | None) -> list[dict[str, Any]]:
        """
        共用媒体检查, 保留协议专属的工具结果表示

        参数:
        - messages: 调用方提供的原始消息
        - img_urls: 可选用户图片
        - video_urls: 可选视频

        返回:
        - 不修改原历史的消息副本
        """
        return prepare_call_messages(messages, self.thinking_field_name if self.api_type == "chat_completions" else None,
                                     img_urls, video_urls=video_urls, supports_visual_input=self.supports_visual_input,
                                     supports_native_video_input=self.supports_native_video_input,
                                     video_frame_interval=self.video_frame_interval, expand_media=self.api_type == "chat_completions")

    def _normalize_response(self, response: LLMCallResponse, model: str | None) -> LLMCallResponse:
        """
        绑定实际请求的逻辑模型名称, 不受供应商返回的版本别名影响

        参数:
        - response: 已解析结果
        - model: 本次调用覆盖的模型名称

        返回:
        - 绑定请求身份后的独立响应
        """
        if response.protocol_payload is not None:
            response.protocol_payload["model"] = model or self.model
        if response.error_info is not None:
            log_model_error(response.error_info, model or self.model)
        return response

    def _parse_response(self, response: object, model: str | None = None) -> LLMCallResponse:
        """
        按固定协议解析非流式结果

        参数:
        - response: SDK 原始响应
        - model: 本次调用覆盖的模型

        返回:
        - 统一响应
        """
        return self._normalize_response(self.protocol.parse_response(response, self.suppress_error), model)

    def _parse_text_response(self, response: object, model: str | None) -> str | Literal[False]:
        """
        保留文本入口的返回约定

        参数:
        - response: SDK 响应
        - model: 本次模型覆盖

        返回:
        - 模型文本, 显式启用 return_false 时失败返回 False
        """
        if self.api_type == "chat_completions":
            from .responses import parse_chat_response
            return parse_chat_response(cast(dict[str, object], response), self.suppress_error)
        parsed = self._parse_response(response, model)
        return False if parsed.error_info is not None and self.return_false else parsed.content
