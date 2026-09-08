from typing import List, Dict, Any, Optional
from satrap.core.utils import normalize_openai_base_url

from .utils import parse_rerank_result


class _ReRankBase:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        top_k: int = 5,
        min_score: float = 0.0,
        lock_api_key: bool = True,
        allow_insecure_base_url: bool = False,
        timeout: int = 60,
    ):
        """
        [同步版本] ReRank API 封装

        参数:
        - api_key: OpenAI API 密钥
        - base_url: OpenAI API 基础 URL
        - model: 要使用的模型名称
        - top_k: 返回的文档数量 (默认 5)
        - min_score: 最小相关性分数阈值 (默认 0.0)
        - lock_api_key: 是否锁定 API 密钥 (默认 True)
        - allow_insecure_base_url: 是否显式允许非回环 HTTP API 地址
        - timeout: 请求超时时间(秒), 默认 60
        """
        self._api_key = api_key
        self.api_key = api_key if not lock_api_key else "api key locked"
        normalized_base_url = normalize_openai_base_url(
            base_url,
            allow_insecure=allow_insecure_base_url,
        )
        self.base_url = normalized_base_url + "/rerank"
        self.model = model
        self.top_k = top_k
        self.min_score = min_score
        self.lock_api_key = lock_api_key
        self.timeout = timeout
        self.suppress_error = True

    def get_api_key(self) -> str:
        """
        获取当前 ReRank 实例的 API Key

        返回:
        - str: 当前 ReRank 实例的 API Key
        """
        return self.api_key

    def get_base_url(self) -> str:
        """
        获取当前 ReRank 实例的 Base URL

        返回:
        - str: 当前 ReRank 实例的 Base URL
        """
        return self.base_url

    def get_model(self) -> str:
        """
        获取当前 ReRank 实例使用的模型名称

        返回:
        - str: 当前 ReRank 实例使用的模型名称
        """
        return self.model

    def get_top_k(self) -> int:
        """
        获取当前 ReRank 实例的 top_k 参数

        返回:
        - int: 当前 ReRank 实例的 top_k 参数
        """
        return self.top_k

    def get_min_score(self) -> float:
        """
        获取当前 ReRank 实例的 min_score 参数

        返回:
        - float: 当前 ReRank 实例的 min_score 参数
        """
        return self.min_score

    def set_parameters(
        self,
        model: Optional[str] = None,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
    ):
        """
        更新 ReRank 实例的默认参数设置

        参数:
        - model: 新的模型名称
        - top_k: 新的 top_k 参数
        - min_score: 新的 min_score 参数
        """
        if model is not None:
            self.model = model
        if top_k is not None:
            self.top_k = top_k
        if min_score is not None:
            self.min_score = min_score

    def _request_body(
        self, query: str, documents: List[str], top_k: int
    ) -> dict[str, Any]:
        """构造同步与异步共用的重排请求体"""
        return {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": top_k,
            "return_documents": True,
        }

    def _finish_response(
        self, api_response: dict[str, Any], min_score: float, top_k: int
    ) -> List[Dict[str, Any]]:
        """统一分数过滤与返回数量限制"""
        results = parse_rerank_result(api_response, min_score, self.suppress_error)
        return results[:top_k] if len(results) > top_k else results
