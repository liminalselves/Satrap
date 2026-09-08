import random
from typing import Any, cast
from bs4 import BeautifulSoup
from satrap.core.utils.TCBuilder.tool_base import _ToolBase
from .utils import USER_AGENTS


class _SearchCore(_ToolBase):
    def __init__(self, timeout: int = 10):
        """
        初始化 SearchTool

        参数:
        - timeout: 超时时间
        """
        super().__init__(self.tool_name, self.description, self.params_dict)
        self.timeout = timeout
        self.base_urls = ["https://cn.bing.com", "https://www.bing.com"]  # 备用域名列表

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
            "Referer": "https://www.bing.com/",
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
            results.append({"title": title, "url": url, "snippet": snippet})
            if len(results) >= max_results:
                break
        # Bing 结果容器: <li class="b_algo">

        return results


class _FetchCore(_ToolBase):
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
