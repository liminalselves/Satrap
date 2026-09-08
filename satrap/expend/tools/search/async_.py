from typing import cast
import json
from bs4 import BeautifulSoup
from satrap.core.utils.TCBuilder import AsyncTool
from satrap.core.utils.outbound import safe_async_get
from .utils import _TitleSoup
from .base import _SearchCore, _FetchCore


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
        - url: URL
        - max_length: 最大length

        返回:
        - str: 异步执行网页获取
        """
        try:
            response = await safe_async_get(
                url,
                headers=self._get_headers(),
                timeout=self.timeout,
                max_redirects=5,
            )
            if response.status_code != 200:
                return json.dumps(
                    {
                        "error": f"HTTP {response.status_code}",
                        "url": response.url,
                    },
                    ensure_ascii=False,
                )

            html = response.text
            soup = cast(_TitleSoup, BeautifulSoup(html, "html.parser"))
            title = (
                soup.title.string.strip()
                if soup.title and soup.title.string
                else "无标题"
            )
            # 提取页面标题, 缺失时使用稳定占位值

            text = self._extract_text(html)
            # 提取正文文本并移除页面结构噪音
            if len(text) > max_length:
                text = text[:max_length] + "...(内容已截断)"

            result: dict[str, object] = {
                "url": response.url,
                "title": title,
                "content": text,
                "status_code": response.status_code,
            }
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps(
                {
                    "error": f"解析失败: {str(e)}",
                    "url": url,
                },
                ensure_ascii=False,
            )
