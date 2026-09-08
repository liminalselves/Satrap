import requests
from typing import cast
import json
from bs4 import BeautifulSoup
from satrap.core.utils.TCBuilder import Tool
from satrap.core.utils.outbound import (
    OutboundHTTPError,
    UnsafeOutboundURLError,
    validate_outbound_http_url,
    validate_outbound_redirect,
)
from .utils import _requests_get, _TitleSoup
from .base import _SearchCore, _FetchCore


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
        - url: URL
        - max_length: 最大length

        返回:
        - str: JSON字符串
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
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"

            soup = cast(_TitleSoup, BeautifulSoup(resp.text, "html.parser"))
            title = (
                soup.title.string.strip()
                if soup.title and soup.title.string
                else "无标题"
            )
            # 提取页面标题, 缺失时使用稳定占位值

            text = self._extract_text(resp.text)
            # 提取正文文本并移除页面结构噪音
            if len(text) > max_length:
                text = text[:max_length] + "...(内容已截断)"

            result: dict[str, object] = {
                "url": current_url,
                "title": title,
                "content": text,
                "status_code": resp.status_code,
            }
            return json.dumps(result, ensure_ascii=False, indent=2)

        except (OutboundHTTPError, requests.RequestException) as e:
            return json.dumps(
                {"error": f"请求失败: {str(e)}", "url": url}, ensure_ascii=False
            )
        except Exception as e:
            return json.dumps(
                {"error": f"解析失败: {str(e)}", "url": url}, ensure_ascii=False
            )
