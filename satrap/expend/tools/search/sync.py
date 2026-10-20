"""同步网页搜索与正文读取, 每次调用独享一个浏览器会话"""
from satrap.core.utils.TCBuilder import Tool
from .transport import BrowserTransport
from .utils import WebRequestError
from .base import _SearchCore, _FetchCore, _failure

from satrap.core.log import logger


class SearchTool(_SearchCore, Tool):
    """无需 API Key 的 Bing 网页搜索"""

    tool_name = "search"
    description = "通过搜索引擎查找网页, 返回标题, 链接和摘要; 访问被拦截时说明原因"
    params_dict = {"query": ("string", "搜索关键词"),
                   "max_results": ("number", "返回结果数量, 默认5, 最大20")}

    def execute(self, query: str, max_results: int = 5) -> str:
        """
        搜索网页, 仅在连接错误或服务端错误时尝试一次备用入口

        参数:
        - query: 非空搜索词
        - max_results: 正整数结果数量, 默认五项, 最多二十项

        返回:
        - 搜索结果 JSON 列表, 或包含 code 的失败 JSON
        """
        url = self.base_urls[0] + "/search"
        try:
            query, count, deadline = self._prepare(query, max_results)
            cached = self.policy.cached(query, count)
            if cached is not None:
                return cached
            with BrowserTransport(deadline, self.policy) as client:
                for index, base_url in enumerate(self.base_urls):
                    url = base_url + "/search"
                    try:
                        response = client.get(url, params={"q": query, "count": count})
                        result = self._search_result(response, count)
                        deadline.remaining()
                        self.policy.remember(query, count, result)
                        return result
                    except WebRequestError as error:
                        if index or not error.retryable:
                            raise
                        logger.warning(f"[网页搜索] 尝试备用入口, 类别={error.code}")
                        deadline.remaining()
            raise WebRequestError("request_failed", "搜索入口不可用")
        except Exception as error:
            return _failure(url, error)


class FetchPageTool(_FetchCore, Tool):
    """同步读取网页标题和正文"""

    tool_name = "fetch_page"
    description = "获取指定 URL 的网页标题和正文; 验证页面和访问拒绝不作为有效正文返回"
    params_dict = {"url": ("string", "要访问的网页 URL"),
                   "max_length": ("number", "正文字符上限, 默认5000, 超出则截断")}

    def execute(self, url: str, max_length: int = 5000) -> str:
        """
        同步读取受限网页正文

        参数:
        - url: 绝对 HTTP/HTTPS 地址
        - max_length: 返回正文字符上限, 默认五千

        返回:
        - 页面 JSON 或包含 code 的失败 JSON
        """
        try:
            deadline = self._prepare_fetch(max_length)
            with BrowserTransport(deadline, self.policy) as client:
                response = client.get(url)
                result = self._format_response(response, max_length)
                deadline.remaining()
                return result
        except Exception as error:
            return _failure(url, error)
