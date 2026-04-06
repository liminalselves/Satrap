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


class FetchPageTool(Tool):
    """网页内容获取工具 (同步)"""
    tool_name = "fetch_page"
    description = "获取指定URL的网页内容，提取标题和正文文本"
    params_dict = {
        "url": ("string", "要访问的网页URL"),
        "max_length": ("number", "返回的文本最大长度，默认5000，超出则截断")
    }

    def __init__(self, timeout: int = 10):
        super().__init__(self.tool_name, self.description, self.params_dict)
        self.timeout = timeout

    def _get_headers(self) -> dict:
        """生成随机请求头"""
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }

    def _extract_text(self, html: str) -> str:
        """从HTML中提取纯文本; 去除脚本, 样式等无关内容"""
        soup = BeautifulSoup(html, "html.parser")
        # 移除脚本和样式
        for element in soup(["script", "style", "meta", "link", "noscript"]):
            element.decompose()
        # 获取文本并规范化空白字符
        text = soup.get_text(separator="\n", strip=True)
        lines = (line.strip() for line in text.splitlines())
        return "\n".join(line for line in lines if line)

    def execute(self, url: str, max_length: int = 5000) -> str:
        """执行网页获取, 返回JSON字符串"""
        try:
            resp = requests.get(url, headers=self._get_headers(), timeout=self.timeout)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"

            # 提取标题
            soup = BeautifulSoup(resp.text, "html.parser")
            title = soup.title.string.strip() if soup.title and soup.title.string else "无标题"

            # 提取正文文本
            text = self._extract_text(resp.text)
            if len(text) > max_length:
                text = text[:max_length] + "...(内容已截断)"

            result = {
                "url": url,
                "title": title,
                "content": text,
                "status_code": resp.status_code
            }
            return json.dumps(result, ensure_ascii=False, indent=2)

        except requests.RequestException as e:
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
        super().__init__(self.tool_name, self.description, self.params_dict)
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    def _get_headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }

    def _extract_text(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for element in soup(["script", "style", "meta", "link", "noscript"]):
            element.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = (line.strip() for line in text.splitlines())
        return "\n".join(line for line in lines if line)

    async def execute(self, url: str, max_length: int = 5000) -> str:
        """异步执行网页获取"""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=self._get_headers(), timeout=self.timeout) as resp:
                    if resp.status != 200:
                        return json.dumps({
                            "error": f"HTTP {resp.status}",
                            "url": url
                        }, ensure_ascii=False)

                    html = await resp.text(encoding="utf-8", errors="replace")

                    # 提取标题
                    soup = BeautifulSoup(html, "html.parser")
                    title = soup.title.string.strip() if soup.title and soup.title.string else "无标题"

                    # 提取正文
                    text = self._extract_text(html)
                    if len(text) > max_length:
                        text = text[:max_length] + "...(内容已截断)"

                    result = {
                        "url": url,
                        "title": title,
                        "content": text,
                        "status_code": resp.status
                    }
                    return json.dumps(result, ensure_ascii=False, indent=2)

            except aiohttp.ClientError as e:
                return json.dumps({
                    "error": f"请求失败: {str(e)}",
                    "url": url
                }, ensure_ascii=False)
            except Exception as e:
                return json.dumps({
                    "error": f"解析失败: {str(e)}",
                    "url": url
                }, ensure_ascii=False)
