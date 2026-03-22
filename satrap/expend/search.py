from satrap.core.utils.TCBuilder import Tool, AsyncTool
from bs4 import BeautifulSoup
import requests
import aiohttp
import random
import json


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

class SearchTool(Tool):
    """搜索爬虫工具"""
    tool_name = "search"
    description = "通过搜索引擎获取网络信息"
    params_dict = {
        "query": ("string", "搜索关键词"),
        "max_results": ("number", "返回结果数量，默认5，最大20")
    }

    def __init__(self, timeout: int = 10):
        super().__init__(self.tool_name, self.description, self.params_dict)
        self.timeout = timeout
        self.base_urls = ["https://cn.bing.com", "https://www.bing.com"]   # 备用域名列表

    def _get_headers(self) -> dict:
        """生成随机请求头"""
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "*/*",
            "Connection": "keep-alive",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7",
            "Referer": "https://www.bing.com/"
        }

    def _parse_result(self, html: str, max_results: int) -> list:
        """解析 Bing 搜索结果页面"""
        soup = BeautifulSoup(html, "html.parser")
        results = []

        # Bing 结果容器: <li class="b_algo">
        for item in soup.select("li.b_algo"):
            title_elem = item.select_one("h2 a")
            if not title_elem:
                continue
            title = title_elem.get_text(strip=True)
            url = title_elem.get("href")

            # 提取摘要
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
    
    def execute(self, query: str, max_results: int = 5) -> str:
        """执行搜索，返回 JSON 字符串"""
        max_results = min(max_results, 20)  # 限制最大条数
        for base_url in self.base_urls:
            try:
                url = f"{base_url}/search?q={query}&count={max_results}"
                resp = requests.get(url, headers=self._get_headers(), timeout=self.timeout)
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
        super().__init__(self.tool_name, self.description, self.params_dict)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.base_urls = ["https://cn.bing.com", "https://www.bing.com"]

    def _get_headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "*/*",
            "Connection": "keep-alive",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7",
            "Referer": "https://www.bing.com/"
        }

    def _parse_result(self, html: str, max_results: int) -> list:
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for item in soup.select("li.b_algo"):
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
        max_results = min(max_results, 20)
        async with aiohttp.ClientSession() as session:
            for base_url in self.base_urls:
                try:
                    url = f"{base_url}/search?q={query}&count={max_results}"
                    async with session.get(url, headers=self._get_headers(), timeout=self.timeout) as resp:
                        if resp.status != 200:
                            continue
                        html = await resp.text(encoding="utf-8")
                        results = self._parse_result(html, max_results)
                        if results:
                            return json.dumps(results, ensure_ascii=False, indent=2)
                except Exception:
                    continue
        return json.dumps({"error": "所有域名均无法访问，请检查网络或稍后重试"})
