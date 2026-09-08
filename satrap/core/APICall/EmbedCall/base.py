from typing import List, Any, Optional, Literal
from satrap.core.utils import normalize_openai_base_url

from typing import Generic, TypeVar
from .utils import _parse_embedding_batch_response, _align_embedding_dimensions

_ClientT = TypeVar("_ClientT")


class _EmbeddingBase(Generic[_ClientT]):
    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "put-your-model-name-here",
        dimensions: Optional[int] = None,
        encoding_format: str = "float",
        suppress_error: bool = True,
        return_false: bool = False,
        lock_api_key: bool = True,
        allow_insecure_base_url: bool = False,
        max_batch_size: int = 100,
        timeout: int = 60,
    ):
        """
        Embedding 共用配置初始化

        参数:
        - api_key: API 密钥
        - base_url: API 地址
        - model: 使用的嵌入模型名称
        - dimensions: 可选, 输出向量的维度
        - encoding_format: 返回编码格式, 默认为 "float", 可选 "base64"
        - suppress_error: 是否抑制异常, 默认 True
        - return_false: 启用时发生错误返回 False 而非空列表
        - lock_api_key: 是否锁定 API Key 的获取以防止泄露, 默认 True
        - allow_insecure_base_url: 是否显式允许非回环 HTTP API 地址
        - max_batch_size: 单次 API 调用最大处理的文本数量, 默认 100
        - timeout: 请求超时时间(秒), 默认 60
        """
        self.api_key = api_key if lock_api_key else "api key locked"
        self.base_url = normalize_openai_base_url(
            base_url,
            allow_insecure=allow_insecure_base_url,
        )
        self.model = model
        self.dimensions = dimensions
        self.encoding_format = encoding_format
        self.suppress_error = suppress_error
        self.return_false = return_false
        if max_batch_size <= 0:
            raise ValueError("max_batch_size 必须大于 0")
        self.max_batch_size = max_batch_size

        self.client = self._create_client(api_key, timeout)

    def _create_client(self, api_key: str, timeout: int) -> _ClientT:
        """由入口创建对应客户端"""
        raise NotImplementedError

    def _empty_return(
        self, is_single: bool
    ) -> List[float] | List[List[float]] | Literal[False]:
        """
        根据 return_false 配置返回适当的空值

        参数:
        - is_single: 输入是否为单个值

        返回:
        - List[float] | List[List[float]] | Literal[False]: 根据 return_false 配置返回适当的空值
        """
        if self.return_false:
            return False
        return []

    def get_model(self) -> str:
        """
        获取当前 Embed 实例使用的模型名称

        返回:
        - str: 当前 Embed 实例使用的模型名称
        """
        return self.model

    def get_api_key(self) -> str:
        """
        获取当前 Embed 实例的 API Key

        返回:
        - str: 当前 Embed 实例的 API Key
        """
        return self.api_key

    def get_base_url(self) -> Optional[str]:
        """
        获取当前 Embed 实例的 Base URL;
        如果未设置则返回 None

        返回:
        - Optional[str]: 当前 Embed 实例的 Base URL
        """
        return self.base_url

    def _request_parameters(
        self, model: str | None, dimensions: int | None, encoding_format: str | None
    ) -> dict[str, Any]:
        """合并模型配置, 保留显式维度值"""
        request: dict[str, Any] = {
            "model": model or self.model,
            "encoding_format": encoding_format or self.encoding_format,
        }
        dimension = dimensions if dimensions is not None else self.dimensions
        if dimension is not None:
            request["dimensions"] = dimension
        return request

    def _parse_batch(
        self, response: Any, count: int, expected_dimension: int | None
    ) -> tuple[List[List[float]], int | None]:
        """按输入位置解析批次并校验跨批次维度"""
        embeddings = _parse_embedding_batch_response(
            response, count, self.suppress_error
        )
        dimension = _align_embedding_dimensions(
            embeddings, expected_dimension, self.suppress_error
        )
        return embeddings, dimension
