from typing import List, Dict, Any, Optional, Union, Literal
import requests
import aiohttp
import asyncio
import json

from satrap.core.log import logger

def parse_rerank_result(
    api_response: Optional[Dict[str, Any]],
    min_score: float = 0.0,
    suppress_error: bool = True,
) -> List[Dict[str, Any]]:
    """
    解析 Rerank API 的输出

    参数:
    - api_response: API 返回的原始 JSON 字典
    - min_score: 最小相关性分数阈值
    - suppress_error: 如果为 True (默认), API 报错时返回空列表而不是抛出异常

    返回:
    - 包含 'text', 'score', 'index' 的字典列表; 如果出错或无结果, 返回空列表
    """

    # 0. 基础类型检查 (防止 api_response 本身是 None)
    if not api_response or not isinstance(api_response, dict):
        msg = "重排接口响应为空或不是字典格式"
        if suppress_error:
            logger.warning(msg)
            return []

        raise ValueError(msg)

    # 1. 状态码检查
    status_code = api_response.get("status_code")
    if status_code is not None and status_code != 200:
        error_msg = api_response.get("message", "Unknown Error")
        log_msg = f"重排接口返回错误状态码: {status_code}, 错误信息: {error_msg}"
        if suppress_error:
            logger.error(log_msg)
            return []
        else:
            raise ValueError(log_msg)

    # 2. 获取 output
    results = None
    if "results" in api_response and isinstance(api_response["results"], list):
        results = api_response["results"]
    elif "output" in api_response and isinstance(api_response["output"], dict):
        results = api_response["output"].get("results")
    else:
        results = api_response.get("results")
    # 兼容保证

    if not results or not isinstance(results, list):
        logger.warning("重排接口响应中未找到有效的 'results' 列表")
        return []

    # 3. 提取核心数据
    parsed_data = []
    for item in results:
        try:
            score = item.get("relevance_score", 0.0)
            if score < min_score:
                continue

            doc = item.get("document")
            if isinstance(doc, dict):
                text = doc.get("text", "")
            else:
                text = item.get("text", "")
            # 提取 document.text

            parsed_data.append({
                "text": text,
                "score": score,
                "original_index": item.get("index")
            })

        except Exception as e:
            logger.warning(f"重排接口结果中跳过格式错误项: {e}")
            continue

    # 4. 确保按分数降序排序
    parsed_data.sort(key=lambda x: x["score"], reverse=True)
    return parsed_data

class ReRank:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        top_k: int = 5,
        min_score: float = 0.0,
        lock_api_key: bool = True,
    ):
        """[同步版本] ReRank API 封装

        参数:
        - api_key: OpenAI API 密钥
        - base_url: OpenAI API 基础 URL
        - model: 要使用的模型名称
        - top_k: 返回的文档数量 (默认 5)
        - min_score: 最小相关性分数阈值 (默认 0.0)
        - lock_api_key: 是否锁定 API 密钥 (默认 True)
        """
        self.api_key = api_key
        self.base_url = base_url + "/rerank"
        self.model = model
        self.top_k = top_k
        self.min_score = min_score
        self.lock_api_key = lock_api_key
       
    def call(
        self,
        query: str,
        documents: List[str],
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """调用 ReRank API 并解析结果
        
        参数:
        - query: 查询文本
        - documents: 文档列表
        - top_k: 返回的文档数量 (默认 self.top_k)
        - min_score: 最小相关性分数阈值 (默认 self.min_score)

        返回:
        - 包含 'text', 'score', 'original_index' 的字典列表; 如果出错或无结果, 返回空列表
        """
        if top_k is None:
            top_k = self.top_k
        if min_score is None:
            min_score = self.min_score

        try:
            request = {
                "model": self.model,
                "query": query,
                "documents": documents,
                "top_n": top_k,
                "return_documents": True,
            }   # 构建请求体

            response = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json=request,
                timeout=60,
            )   # 发送 POST 请求

            api_response = response.json()   # 解析 JSON 响应

        except requests.exceptions.Timeout:
            logger.error("[ReRank] 重排接口请求请求超时")
            return []
        
        except requests.exceptions.ConnectionError as e:
            logger.error(f"[ReRank] 重排接口连接错误: {e}")
            return []

        except json.JSONDecodeError as e:
            logger.error(f"[ReRank] 重排接口响应解析错误: {e}")
            return []
        
        except Exception as e:
            logger.error(f"[ReRank] 重排接口调用出错: {e}")
            return []

        parsed_results = parse_rerank_result(
            api_response=api_response,
            min_score=min_score,
            suppress_error=True,
        )   # 解析结果

        if len(parsed_results) > top_k:   # 保证返回数量不超过上限
            parsed_results = parsed_results[:top_k]

        return parsed_results

    def get_api_key(self) -> str:
        """获取当前 ReRank 实例的 API Key"""
        return self.api_key if not self.lock_api_key else "api key locked"

    def get_base_url(self) -> str:
        """获取当前 ReRank 实例的 Base URL"""
        return self.base_url

    def get_model(self) -> str:
        """获取当前 ReRank 实例使用的模型名称"""
        return self.model

    def get_top_k(self) -> int:
        """获取当前 ReRank 实例的 top_k 参数"""
        return self.top_k

    def get_min_score(self) -> float:
        """获取当前 ReRank 实例的 min_score 参数"""
        return self.min_score

    def set_parameters(
        self,
        model: Optional[str] = None,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
    ):
        """更新 ReRank 实例的默认参数设置

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



class AsyncReRank:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        top_k: int = 5,
        min_score: float = 0.0,
        lock_api_key: bool = True,
    ):
        """[异步版本] ReRank API 封装

        参数:
        - api_key: OpenAI API 密钥
        - base_url: OpenAI API 基础 URL
        - model: 要使用的模型名称
        - top_k: 返回的文档数量 (默认 5)
        - min_score: 最小相关性分数阈值 (默认 0.0)
        - lock_api_key: 是否锁定 API Key (默认 True)
        """
        self.api_key = api_key
        self.base_url = base_url + "/rerank"
        self.model = model
        self.top_k = top_k
        self.min_score = min_score
        self.lock_api_key = lock_api_key
       
    async def call(
        self,
        query: str,
        documents: List[str],
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """异步调用 ReRank API 并解析结果
        
        参数:
        - query: 查询文本
        - documents: 文档列表
        - top_k: 返回的文档数量 (默认 self.top_k)
        - min_score: 最小相关性分数阈值 (默认 self.min_score)

        返回:
        - 包含 'text', 'score', 'original_index' 的字典列表; 如果出错或无结果, 返回空列表
        """
        if top_k is None:
            top_k = self.top_k
        if min_score is None:
            min_score = self.min_score

        try:
            request = {
                "model": self.model,
                "query": query,
                "documents": documents,
                "top_n": top_k,
                "return_documents": True,
            }   # 构建请求体

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.base_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json=request,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as response:
                    api_response = await response.json()   # 解析 JSON 响应

        except asyncio.TimeoutError:
            logger.error("[AsyncReRank] 重排接口请求超时")
            return []
        
        except aiohttp.ClientConnectionError as e:
            logger.error(f"[AsyncReRank] 重排接口连接错误: {e}")
            return []

        except json.JSONDecodeError as e:
            logger.error(f"[AsyncReRank] 重排接口响应解析错误: {e}")
            return []
        
        except Exception as e:
            logger.error(f"[AsyncReRank] 重排接口调用出错: {e}")
            return []

        parsed_results = parse_rerank_result(
            api_response=api_response,
            min_score=min_score,
            suppress_error=True,
        )   # 解析结果

        if len(parsed_results) > top_k:   # 保证返回数量不超过上限
            parsed_results = parsed_results[:top_k]

        return parsed_results

    def get_api_key(self) -> str:
        """获取当前 AsyncReRank 实例的 API Key"""
        return self.api_key if not self.lock_api_key else "api key locked"

    def get_base_url(self) -> str:
        """获取当前 AsyncReRank 实例的 Base URL"""
        return self.base_url

    def get_model(self) -> str:
        """获取当前 AsyncReRank 实例使用的模型名称"""
        return self.model

    def get_top_k(self) -> int:
        """获取当前 AsyncReRank 实例的 top_k 参数"""
        return self.top_k

    def get_min_score(self) -> float:
        """获取当前 AsyncReRank 实例的 min_score 参数"""
        return self.min_score

    def set_parameters(
        self,
        model: Optional[str] = None,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
    ):
        """更新 AsyncReRank 实例的默认参数设置

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


