import aiohttp
import asyncio
from typing import List, Dict, Any, Optional, cast
import json
from satrap.core.log import logger

from .base import _ReRankBase


class AsyncReRank(_ReRankBase):

    async def call(
        self,
        query: str,
        documents: List[str],
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        异步调用 ReRank API 并解析结果

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
            request = self._request_body(query, documents, top_k)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.base_url,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as response:
                    if not 200 <= response.status < 300:
                        logger.error(
                            f"[AsyncReRank] 重排接口返回 HTTP {response.status}"
                        )
                        if not self.suppress_error:
                            raise RuntimeError(f"重排接口返回 HTTP {response.status}")
                        return []
                    raw_response: object = await response.json()  # 解析 JSON 响应
                    if not isinstance(raw_response, dict):
                        raise ValueError("重排接口响应不是 JSON 对象")
                    api_response = {
                        str(key): value
                        for key, value in cast(
                            dict[object, object], raw_response
                        ).items()
                    }

        except asyncio.TimeoutError:
            logger.error("[AsyncReRank] 重排接口请求超时")
            if not self.suppress_error:
                raise RuntimeError("重排接口调用失败")
            return []

        except aiohttp.ClientConnectionError as e:
            logger.error(f"[AsyncReRank] 重排接口连接错误: {e}")
            if not self.suppress_error:
                raise RuntimeError("重排接口调用失败")
            return []

        except json.JSONDecodeError as e:
            logger.error(f"[AsyncReRank] 重排接口响应解析错误: {e}")
            if not self.suppress_error:
                raise RuntimeError("重排接口调用失败")
            return []

        except Exception as e:
            logger.error(f"[AsyncReRank] 重排接口调用出错: {e}")
            if not self.suppress_error:
                raise RuntimeError("重排接口调用失败")
            return []

        return self._finish_response(api_response, min_score, top_k)
