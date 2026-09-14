"""
搜索与网页抓取共用逻辑

统一解析页面, 分类请求与安全错误, 同步和异步入口分别负责网络执行
"""

import random
from typing import Any, Protocol, cast
import json
from bs4 import BeautifulSoup

from satrap.core.utils.TCBuilder.tool_base import _ToolBase
from satrap.core.utils.outbound import UnsafeOutboundURLError
from .utils import USER_AGENTS

class _SearchCore(_ToolBase):
    recovery_policy = "retry"  # 搜索请求可重复执行, 返回结果可能随时间变化

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


class _PageResponse(Protocol):
    """同步和异步 HTTP 响应的页面读取接口"""

    @property
    def status_code(self) -> int:
        """
        读取 HTTP 状态码

        返回:
        - 最终响应状态码
        """
        ...

    @property
    def text(self) -> str:
        """
        读取解码后的页面文本

        返回:
        - HTML 字符串, 解码错误由调用方处理
        """
        ...


def _fetch_error(url: str, error: Exception, *, parsing: bool = False) -> str:
    """
    按执行阶段和安全策略生成抓取错误结果

    参数:
    - url: 原始请求地址
    - error: 请求或解析阶段的异常
    - parsing: 是否发生在页面解析阶段, 默认 False 表示请求阶段

    返回:
    - 包含 error 和 url 的 JSON 字符串
    """
    if parsing:
        category = "解析失败"
    elif isinstance(error, UnsafeOutboundURLError):
        category = "请求被安全策略拒绝"
    else:
        category = "请求失败"
    return json.dumps({"error": f"{category}: {error}", "url": url}, ensure_ascii=False)


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


    def _format_response(self, url: str, response: _PageResponse, max_length: int) -> str:
        """
        统一检查响应状态并提取页面标题与正文

        参数:
        - url: 完成重定向后的页面地址
        - response: 同步或异步请求取得的响应
        - max_length: 正文最大长度, 超出时截断并添加说明

        返回:
        - 成功时返回页面 JSON, 非 200 返回 HTTP 错误, 解析异常返回解析失败
        """
        # Step.1 统一处理非成功响应
        if response.status_code != 200:
            return json.dumps(
                {"error": f"HTTP {response.status_code}", "url": url}, ensure_ascii=False
            )
        # Step.2 解析成功页面并限制正文长度
        try:
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.string.strip() if soup.title and soup.title.string else "无标题"
            text = self._extract_text(html)
            if len(text) > max_length:
                text = text[:max_length] + "...(内容已截断)"
            return json.dumps(
                {"url": url, "title": title, "content": text, "status_code": response.status_code},
                ensure_ascii=False,
                indent=2,
            )
        except Exception as error:
            return _fetch_error(url, error, parsing=True)
