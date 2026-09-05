"""网页搜索与页面内容抓取工具"""
import requests
import random
from typing import Any, cast
from typing import Protocol
import json
from bs4 import BeautifulSoup

from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.core.utils.outbound import (
    OutboundHTTPError,
    UnsafeOutboundURLError,
    safe_async_get,
    safe_sync_get,
    validate_outbound_http_url,
    validate_outbound_redirect,
)


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.131 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:88.0) Gecko/20100101 Firefox/88.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.131 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Version/14.1.2 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Version/14.1 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:88.0) Gecko/20100101 Firefox/88.0",
]


class _RequestsResponse(Protocol):
    """搜索工具依赖的 requests 响应最小接口"""

    encoding: str
    status_code: int
    headers: dict[str, str]

    @property
    def text(self) -> str:
        """返回按当前编码解码的响应正文"""
        ...

    @property
    def apparent_encoding(self) -> str | None:
        """返回响应推断编码"""
        ...

    @property
    def is_redirect(self) -> bool:
        """返回响应是否为重定向"""
        ...

    def raise_for_status(self) -> None:
        """在 HTTP 状态异常时抛出 requests 异常"""
        ...


class _RequestsGet(Protocol):
    """requests.get 的可检查调用接口"""

    def __call__(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int,
        allow_redirects: bool = True,
    ) -> _RequestsResponse:
        """
        发起同步 HTTP GET 请求

        参数:
        - url: 请求 URL
        - params: 查询参数
        - headers: 请求头
        - timeout: 超时秒数
        - allow_redirects: 是否自动跟随重定向

        返回:
        - _RequestsResponse: 可检查的响应接口
        """
        ...


_requests_get: _RequestsGet = safe_sync_get
"""执行地址绑定和响应限流的同步 GET 请求"""


class _TitleElement(Protocol):
    """声明页面标题节点所需的最小接口"""

    string: str | None


class _TitleSoup(Protocol):
    """声明页面标题提取所需的最小接口"""

    title: _TitleElement | None

class SearchTool(Tool):
    """搜索爬虫工具"""
    tool_name = "search"
    description = "通过搜索引擎获取网络信息"
    params_dict = {
        "query": ("string", "搜索关键词"),
        "max_results": ("number", "返回结果数量，默认5，最大20")
    }

    def __init__(self, timeout: int = 10):
        """
        初始化 SearchTool

        参数:
        - timeout: 超时时间
        """
        super().__init__(self.tool_name, self.description, self.params_dict)
        self.timeout = timeout
        self.base_urls = ["https://cn.bing.com", "https://www.bing.com"]   # 备用域名列表

    def _get_headers(self) -> dict[str, str]:
        """
        生成随机请求头

        返回:
        - dict[str, str]: 生成随机请求头
        """
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "*/*",
            "Connection": "keep-alive",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7",
            "Referer": "https://www.bing.com/"
        }

    def _parse_result(self, html: str, max_results: int) -> list[dict[str, str]]:
        """
        解析 Bing 搜索结果页面

        参数:
        - html: HTML 内容
        - max_results: 最大results

        返回:
        - list[dict[str, str]]: 解析 Bing 搜索结果页面
        """
        soup = cast(Any, BeautifulSoup(html, "html.parser"))
        results: list[dict[str, str]] = []

        for item in cast(list[Any], soup.select("li.b_algo")):
            title_elem = item.select_one("h2 a")
            if not title_elem:
                continue
            title = title_elem.get_text(strip=True)
            url = title_elem.get("href")

            snippet_elem = item.select_one("p")
            # 提取摘要
            snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet
            })
            if len(results) >= max_results:
                break
        # Bing 结果容器: <li class="b_algo">

        return results
    
    def execute(self, query: str, max_results: int = 5) -> str:
        """
        执行搜索, 返回 JSON 字符串

        参数:
        - query: 查询内容
        - max_results: 最大results

        返回:
        - str:  JSON 字符串
        """
        max_results = min(max_results, 20)   # 限制最大条数
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

            except Exception:   # 当前域名失败, 尝试下一个
                continue

        return json.dumps({"error": "所有域名均无法访问，请检查网络或稍后重试"})

class AsyncSearchTool(AsyncTool):
    """搜索爬虫工具"""
    tool_name = "search"
    description = "通过搜索引擎获取网络信息"
    params_dict = {
        "query": ("string", "搜索关键词"),
        "max_results": ("number", "返回结果数量，默认5，最大20")
    }

    def __init__(self, timeout: int = 10):
        """
        初始化 AsyncSearchTool

        参数:
        - timeout: 超时时间
        """
        super().__init__(self.tool_name, self.description, self.params_dict)
        self.timeout = timeout
        self.base_urls = ["https://cn.bing.com", "https://www.bing.com"]

    def _get_headers(self) -> dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "*/*",
            "Connection": "keep-alive",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7",
            "Referer": "https://www.bing.com/"
        }

    def _parse_result(self, html: str, max_results: int) -> list[dict[str, str]]:
        soup = cast(Any, BeautifulSoup(html, "html.parser"))
        results: list[dict[str, str]] = []
        for item in cast(list[Any], soup.select("li.b_algo")):
            title_elem = item.select_one("h2 a")
            if not title_elem:
                continue
            title = title_elem.get_text(strip=True)
            url = title_elem.get("href")
            snippet_elem = item.select_one("p")
            snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet
            })
            if len(results) >= max_results:
                break
        return results

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


class FetchPageTool(Tool):
    """网页内容获取工具 (同步)"""
    tool_name = "fetch_page"
    description = "获取指定URL的网页内容，提取标题和正文文本"
    params_dict = {
        "url": ("string", "要访问的网页URL"),
        "max_length": ("number", "返回的文本最大长度，默认5000，超出则截断")
    }

    def __init__(self, timeout: int = 10):
        """
        初始化 FetchPageTool

        参数:
        - timeout: 超时时间
        """
        super().__init__(self.tool_name, self.description, self.params_dict)
        self.timeout = timeout

    def _get_headers(self) -> dict[str, str]:
        """
        生成随机请求头

        返回:
        - dict[str, str]: 生成随机请求头
        """
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }

    def _extract_text(self, html: str) -> str:
        """
        从HTML中提取纯文本; 去除脚本, 样式等无关内容

        参数:
        - html: HTML 内容

        返回:
        - str: 从HTML中提取纯文本; 去除脚本, 样式等无关内容
        """
        soup = cast(Any, BeautifulSoup(html, "html.parser"))
        # 移除脚本和样式
        for element in soup(["script", "style", "meta", "link", "noscript"]):
            element.decompose()
        # 获取文本并规范化空白字符
        text = soup.get_text(separator="\n", strip=True)
        lines = (line.strip() for line in text.splitlines())
        return "\n".join(line for line in lines if line)

    def execute(self, url: str, max_length: int = 5000) -> str:
        """
        执行网页获取, 返回JSON字符串

        参数:
        - url: URL
        - max_length: 最大length

        返回:
        - str: JSON字符串
        """
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
            title = soup.title.string.strip() if soup.title and soup.title.string else "无标题"
            # 提取页面标题, 缺失时使用稳定占位值

            text = self._extract_text(resp.text)
            # 提取正文文本并移除页面结构噪音
            if len(text) > max_length:
                text = text[:max_length] + "...(内容已截断)"

            result: dict[str, object] = {
                "url": current_url,
                "title": title,
                "content": text,
                "status_code": resp.status_code
            }
            return json.dumps(result, ensure_ascii=False, indent=2)

        except (OutboundHTTPError, requests.RequestException) as e:
            return json.dumps({
                "error": f"请求失败: {str(e)}",
                "url": url
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({
                "error": f"解析失败: {str(e)}",
                "url": url
            }, ensure_ascii=False)

class AsyncFetchPageTool(AsyncTool):
    """网页内容获取工具 (异步)"""
    tool_name = "fetch_page"
    description = "获取指定URL的网页内容，提取标题和正文文本"
    params_dict = {
        "url": ("string", "要访问的网页URL"),
        "max_length": ("number", "返回的文本最大长度，默认5000，超出则截断")
    }

    def __init__(self, timeout: int = 10):
        """
        初始化 AsyncFetchPageTool

        参数:
        - timeout: 超时时间
        """
        super().__init__(self.tool_name, self.description, self.params_dict)
        self.timeout = timeout

    def _get_headers(self) -> dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }

    def _extract_text(self, html: str) -> str:
        soup = cast(Any, BeautifulSoup(html, "html.parser"))
        for element in soup(["script", "style", "meta", "link", "noscript"]):
            element.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = (line.strip() for line in text.splitlines())
        return "\n".join(line for line in lines if line)

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
                return json.dumps({
                    "error": f"HTTP {response.status_code}",
                    "url": response.url,
                }, ensure_ascii=False)

            html = response.text
            soup = cast(_TitleSoup, BeautifulSoup(html, "html.parser"))
            title = soup.title.string.strip() if soup.title and soup.title.string else "无标题"
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
            return json.dumps({
                "error": f"解析失败: {str(e)}",
                "url": url,
            }, ensure_ascii=False)
