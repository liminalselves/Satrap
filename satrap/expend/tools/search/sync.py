"""
同步搜索与网页抓取工具

负责同步请求和重定向校验, 复用公共页面解析与错误分类
"""

import json

from satrap.core.utils.TCBuilder import Tool
from satrap.core.utils.outbound import (
    UnsafeOutboundURLError,
    validate_outbound_http_url,
    validate_outbound_redirect,
)
from .base import _SearchCore, _FetchCore, _fetch_error

class SearchTool(_SearchCore, Tool):
    """搜索爬虫工具"""

    tool_name = "search"
    description = "通过搜索引擎获取网络信息"
    params_dict = {
        "query": ("string", "搜索关键词"),
        "max_results": ("number", "返回结果数量，默认5，最大20"),
    }

    def execute(self, query: str, max_results: int = 5) -> str:
        """
        执行搜索, 返回 JSON 字符串

        参数:
        - query: 查询内容
        - max_results: 最大results

        返回:
        - str:  JSON 字符串
        """
        from . import _requests_get

        max_results = min(max_results, 20)  # 限制最大条数
        for base_url in self.base_urls:
            try:
                url = f"{base_url}/search"
                resp = _requests_get(
                    url,
                    params={"q": query, "count": max_results},
                    headers=self._get_headers(),
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                resp.encoding = "utf-8"
                results = self._parse_result(resp.text, max_results)
                if results:
                    return json.dumps(results, ensure_ascii=False, indent=2)

            except Exception:  # 当前域名失败, 尝试下一个
                continue

        return json.dumps({"error": "所有域名均无法访问，请检查网络或稍后重试"})


class FetchPageTool(_FetchCore, Tool):
    """网页内容获取工具 (同步)"""

    tool_name = "fetch_page"
    description = "获取指定URL的网页内容，提取标题和正文文本"
    params_dict = {
        "url": ("string", "要访问的网页URL"),
        "max_length": ("number", "返回的文本最大长度，默认5000，超出则截断"),
    }

    def execute(self, url: str, max_length: int = 5000) -> str:
        """
        执行网页获取, 返回JSON字符串

        参数:
        - url: 待抓取的 HTTP 或 HTTPS 地址
        - max_length: 正文最大长度, 默认 5000, 超出时截断

        返回:
        - 页面 JSON, HTTP 状态错误, 安全拒绝, 请求失败或解析失败说明
        """
        from . import _requests_get

        try:
            current_url = validate_outbound_http_url(url)
            for _ in range(6):
                resp = _requests_get(
                    current_url,
                    headers=self._get_headers(),
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                if resp.is_redirect:
                    current_url = validate_outbound_redirect(
                        current_url,
                        resp.headers.get("Location", ""),
                    )
                    continue
                break
            else:
                raise UnsafeOutboundURLError("重定向次数超过限制")
            resp.encoding = resp.apparent_encoding or "utf-8"
        except Exception as error:
            return _fetch_error(url, error)
        return self._format_response(current_url, resp, max_length)
