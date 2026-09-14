"""
异步搜索与网页抓取工具

负责异步安全请求, 复用公共页面解析与错误分类
"""

import json

from satrap.core.utils.TCBuilder import AsyncTool
from satrap.core.utils.outbound import safe_async_get
from .base import _SearchCore, _FetchCore, _fetch_error

class AsyncSearchTool(_SearchCore, AsyncTool):
    """搜索爬虫工具"""

    tool_name = "search"
    description = "通过搜索引擎获取网络信息"
    params_dict = {
        "query": ("string", "搜索关键词"),
        "max_results": ("number", "返回结果数量，默认5，最大20"),
    }

    async def execute(self, query: str, max_results: int = 5) -> str:
        """
        执行

        参数:
        - query: 查询内容
        - max_results: 最大结果列表

        返回:
        - str: 执行
        """
        max_results = min(max_results, 20)
        for base_url in self.base_urls:
            try:
                response = await safe_async_get(
                    f"{base_url}/search",
                    params={"q": query, "count": max_results},
                    headers=self._get_headers(),
                    timeout=self.timeout,
                )
                if response.status_code != 200:
                    continue
                results = self._parse_result(response.text, max_results)
                if results:
                    return json.dumps(results, ensure_ascii=False, indent=2)
            except Exception:
                continue
        return json.dumps({"error": "所有域名均无法访问，请检查网络或稍后重试"})


class AsyncFetchPageTool(_FetchCore, AsyncTool):
    """网页内容获取工具 (异步)"""

    tool_name = "fetch_page"
    description = "获取指定URL的网页内容，提取标题和正文文本"
    params_dict = {
        "url": ("string", "要访问的网页URL"),
        "max_length": ("number", "返回的文本最大长度，默认5000，超出则截断"),
    }

    async def execute(self, url: str, max_length: int = 5000) -> str:
        """
        异步执行网页获取

        参数:
        - url: 待抓取的 HTTP 或 HTTPS 地址
        - max_length: 正文最大长度, 默认 5000, 超出时截断

        返回:
        - 页面 JSON, HTTP 状态错误, 安全拒绝, 请求失败或解析失败说明
        """
        try:
            response = await safe_async_get(
                url,
                headers=self._get_headers(),
                timeout=self.timeout,
                max_redirects=5,
            )
        except Exception as error:
            return _fetch_error(url, error)
        return self._format_response(response.url, response, max_length)
