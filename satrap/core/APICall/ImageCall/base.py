"""同步与异步图片调用共用配置, 校验和脱敏错误处理"""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Generic, Literal, Sequence, TypeVar

import httpx

from satrap.core.log import logger
from .media import MAX_IMAGES, validate_url
from .request import input_sources, merge_params, validate_params
from .response import safe_request_id
from .types import ImageGenError, ImageGenParams, ImageInput


_ClientT = TypeVar("_ClientT")


class _ImageGenBase(Generic[_ClientT]):
    def __init__(
        self, api_key: str, base_url: str, *,
        api_type: Literal["openai_images", "seedream", "novelai"], model: str,
        edit_base_url: str | None = None, timeout: float = 180,
        default_params: ImageGenParams | None = None, suppress_error: bool = True,
        lock_api_key: bool = True, allow_insecure_base_url: bool = False,
    ) -> None:
        """
        初始化图片调用配置, 请求地址必须完整且不会被改写

        参数:
        - api_key: 厂商密钥或 Persistent API Token
        - base_url: 完整生成请求地址
        - api_type: 仅用于构造入参的协议
        - model: 默认模型 ID
        - edit_base_url: 可选完整编辑地址, 省略时沿用 base_url
        - timeout: 单次 HTTP 请求超时秒数, 不自动重试
        - default_params: 公共和厂商专有参数的统一默认值
        - suppress_error: API 调用失败时是否记录脱敏日志并返回 None
        - lock_api_key: 是否隐藏公开属性中的密钥
        - allow_insecure_base_url: 是否允许非回环 HTTP 地址
        """
        if api_type not in {"openai_images", "seedream", "novelai"}:
            raise ValueError("不支持的图片入参协议")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key 不能为空")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout 必须是有限正数")
        self.api_type = api_type
        self.base_url = validate_url(base_url, allow_insecure=allow_insecure_base_url)
        self.edit_base_url = validate_url(edit_base_url, allow_insecure=allow_insecure_base_url) if edit_base_url is not None else self.base_url
        self.allow_insecure_base_url = allow_insecure_base_url
        self.api_key = "api key locked" if lock_api_key else api_key
        self._api_key = api_key
        self.model = model
        self.network_timeout = timeout
        self.default_params = deepcopy(default_params)
        self.suppress_error = suppress_error
        params = merge_params(self.default_params, None, api_type)
        validate_params(params, api_type, params.model if params.model is not None else model)
        self.client = self._create_client()

    def _create_client(self) -> _ClientT:
        """由同步或异步入口构造对应的 HTTP 客户端"""
        raise NotImplementedError

    def _prepare(
        self, prompt: str, params: ImageGenParams | None,
        images: Sequence[ImageInput] | None, mask: ImageInput | None,
    ) -> tuple[ImageGenParams, str, list[ImageInput], list[ImageInput], str]:
        """准备一次请求, 本地参数错误始终抛出且发生在 HTTP 调用前"""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt 不能为空")
        if images is not None and (isinstance(images, (str, bytes)) or not images or len(images) > MAX_IMAGES):
            raise ValueError("edit 必须提供非空且不超过上限的图片列表")
        used = merge_params(self.default_params, params, self.api_type)
        model = used.model if used.model is not None else self.model
        validate_params(used, self.api_type, model)
        sources = input_sources(images or (), mask, used)
        return used, model, list(images or ()), sources, self.edit_base_url if images is not None else self.base_url

    def _headers(self) -> dict[str, str]:
        """鉴权只用于生成和显式编码请求, 不用于图片下载"""
        return {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}

    def _http_error(self, status: int, headers: httpx.Headers) -> None:
        """接受全部 2xx, 错误不读取或回显可能包含凭据的响应正文"""
        if not 200 <= status < 300:
            raise ImageGenError("http_error", status=status, request_id=self._request_id(headers))

    @staticmethod
    def _request_id(headers: httpx.Headers) -> str | None:
        """兼容常见请求跟踪头"""
        return safe_request_id(headers.get("x-request-id")) or safe_request_id(headers.get("x-correlation-id"))

    def _failure(self, error: Exception) -> None:
        """错误只输出类型和状态码, 不记录 URL, 提示词, 响应正文或密钥"""
        if isinstance(error, ImageGenError):
            safe = error
        elif isinstance(error, httpx.TimeoutException):
            safe = ImageGenError("network_timeout")
        else:
            safe = ImageGenError("network_error")
        logger.error(f"[图片生成] {safe.code} status={safe.status}")
        if not self.suppress_error:
            raise safe from None

    def _encoding_request(
        self, base_url: str, image: ImageInput, model: str | None, information_extracted: float,
    ) -> tuple[str, str]:
        """Vibe 编码是显式的独立操作, 不从生成地址推导端点"""
        if self.api_type != "novelai":
            raise ValueError("encode_vibe 仅用于 NovelAI")
        if not isinstance(image, ImageInput):
            raise ValueError("image 必须是 ImageInput")
        if isinstance(information_extracted, bool) or not isinstance(information_extracted, (int, float)) or not math.isfinite(information_extracted) or not 0 <= information_extracted <= 1:
            raise ValueError("information_extracted 必须在 0 到 1 之间")
        default_model = self.default_params.model if self.default_params is not None else None
        used_model = (default_model if default_model is not None else self.model) if model is None else model
        if not isinstance(used_model, str) or not used_model:
            raise ValueError("model 不能为空")
        return validate_url(base_url, allow_insecure=self.allow_insecure_base_url), used_model
