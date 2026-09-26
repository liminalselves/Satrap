"""同步与异步转录共用的配置和请求参数构造"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from satrap.core.utils import normalize_openai_base_url

_ClientT = TypeVar("_ClientT")


class _ASRBase(Generic[_ClientT]):
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str = "put-your-model-name-here",
        language: str = "",
        prompt: str = "",
        timeout: int = 60,
        suppress_error: bool = True,
        lock_api_key: bool = True,
        allow_insecure_base_url: bool = False,
    ):
        """
        ASR API 共用配置初始化

        参数:
        - api_key: API 密钥
        - base_url: OpenAI 兼容 API 地址
        - model: 转录模型名称
        - language: 默认识别语言, 空字符串由服务端自动检测
        - prompt: 默认提示词, 空字符串不传递
        - timeout: 请求超时时间, 默认 60 秒
        - suppress_error: 是否抑制异常返回 None, 默认 True
        - lock_api_key: 是否锁定 API Key 的获取以防止泄露, 默认 True
        - allow_insecure_base_url: 是否显式允许非回环 HTTP API 地址
        """
        self.api_key = api_key if not lock_api_key else "api key locked"
        self.model = model
        self.base_url = normalize_openai_base_url(
            base_url,
            allow_insecure=allow_insecure_base_url,
        )
        self.language = language
        self.prompt = prompt
        self.suppress_error = suppress_error
        self.client = self._create_client(api_key, timeout)

    def _create_client(self, api_key: str, timeout: int) -> _ClientT:
        """由入口选择同步或异步客户端"""
        raise NotImplementedError

    def get_model(self) -> str:
        """
        获取当前 ASR 实例使用的模型名称

        返回:
        - str: 当前 ASR 实例使用的模型名称
        """
        return self.model

    def get_api_key(self) -> str:
        """
        获取当前 ASR 实例的 API Key

        返回:
        - str: 当前 ASR 实例的 API Key
        """
        return self.api_key

    def get_base_url(self) -> str | None:
        """
        获取当前 ASR 实例的 Base URL

        返回:
        - str | None: 当前 ASR 实例的 Base URL, 未设置时为 None
        """
        return self.base_url

    def set_parameters(
        self,
        model: str | None = None,
        language: str | None = None,
        prompt: str | None = None,
    ):
        """
        更新 ASR 实例的默认参数设置

        参数:
        - model: 新的模型名称
        - language: 新的默认识别语言
        - prompt: 新的默认提示词
        """
        if model is not None:
            self.model = model
        if language is not None:
            self.language = language
        if prompt is not None:
            self.prompt = prompt

    def _transcription_parameters(
        self,
        model: str | None,
        language: str | None,
        prompt: str | None,
    ) -> dict[str, Any]:
        """合并调用参数与实例默认值, 可选参数为空时不传递给服务端"""
        used_language = language if language is not None else self.language
        used_prompt = prompt if prompt is not None else self.prompt
        parameters: dict[str, Any] = {
            "model": model or self.model,
            "response_format": "json",
        }
        if used_language:
            parameters["language"] = used_language
        if used_prompt:
            parameters["prompt"] = used_prompt
        return parameters
