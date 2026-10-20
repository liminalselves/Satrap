"""
搜索与网页抓取的共用解析逻辑

区分正常结果, 无结果, 验证页面和解析失败,
同步和异步入口共用参数校验, 输出格式与日志边界
"""
from __future__ import annotations

from urllib.parse import urlsplit
import traceback
from typing import TypedDict
import math
import json
from bs4 import BeautifulSoup

from satrap.core.utils.TCBuilder.tool_base import _ToolBase
from satrap.core.utils.outbound import (
    OutboundHTTPResponse, OutboundResponseTooLargeError, UnsafeOutboundURLError,
)
from .utils import WEB_POLICY, Deadline, WebPolicy, WebRequestError, site_key

from satrap.core.log import logger


class SearchResult(TypedDict):
    """保留搜索工具原有的成功结果字段"""

    title: str
    url: str
    snippet: str


def _failure(url: str, error: Exception) -> str:
    """
    在工具边界统一记录失败, 返回稳定错误类别

    参数:
    - url: 当前请求地址, 日志仅记录站点而不包含查询参数
    - error: 执行或解析异常

    返回:
    - 保留 error 和 url 的 JSON, 追加 code 和可用的 HTTP 或冷却信息
    """
    if isinstance(error, WebRequestError):
        failure = error
    elif isinstance(error, UnsafeOutboundURLError):
        failure = WebRequestError("unsafe_url", f"请求被安全策略拒绝: {error}")
    elif isinstance(error, OutboundResponseTooLargeError):
        failure = WebRequestError("response_too_large", f"请求失败: {error}")
    elif isinstance(error, TimeoutError):
        failure = WebRequestError("timeout", "网页工具执行超时")
    else:
        failure = WebRequestError("request_failed", "网页请求或解析失败")
        logger.error(f"[网页工具] 非预期失败, 类型={type(error).__name__}\n{traceback.format_exc()}")
    try:
        host = site_key(url)
    except ValueError:
        host = "invalid"
    logger.warning(f"[网页工具] 请求失败, 站点={host}, 类别={failure.code}, 状态={failure.status}")
    result: dict[str, str | int] = {"error": str(failure), "code": failure.code, "url": url}
    if failure.status is not None:
        result["status_code"] = failure.status
    if failure.retry_after is not None:
        result["retry_after"] = math.ceil(failure.retry_after)
    return json.dumps(result, ensure_ascii=False)


class _WebCore(_ToolBase):
    """共享网页工具初始化和响应识别"""

    def __init__(self, timeout: float = 10) -> None:
        """
        初始化网页工具

        参数:
        - timeout: 整次调用总时限, 包括限流等待和重定向
        """
        super().__init__(self.tool_name, self.description, self.params_dict)
        self.timeout = timeout
        self.policy: WebPolicy = WEB_POLICY

    def _page(self, response: OutboundHTTPResponse) -> BeautifulSoup:
        """
        检查 HTTP 状态与明确验证页面, 再返回解析树

        参数:
        - response: 受地址和正文大小限制的响应

        返回:
        - HTML 解析树; 验证页面进入共享冷却, 其它错误不伪装成成功正文
        """
        if response.status_code != 200:
            code = {403: "access_denied", 429: "rate_limited"}.get(response.status_code, "http_error")
            raise WebRequestError(code, f"HTTP {response.status_code}", status=response.status_code,
                                  retryable=response.status_code >= 500)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type and content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
            raise WebRequestError("unsupported_content", "目标未返回可读取的网页文本")
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.get_text(strip=True).lower() if soup.title else ""
        challenge_form = soup.select_one("form#challenge-form, form#captcha-form, form[action*='/turing/']")
        challenge_script = soup.select_one("script[src*='/cdn-cgi/challenge-platform/']")
        if challenge_form or (challenge_script and title in {"just a moment...", "attention required! | cloudflare"}):
            self.policy.cool_down(site_key(response.url), "verification_required")
            raise WebRequestError("verification_required", "网站要求浏览器验证, 未取得有效正文",
                                  status=response.status_code, retry_after=60)
        return soup


class _SearchCore(_WebCore):
    """Bing 网页搜索的参数和结果解析"""

    recovery_policy = "retry"

    def __init__(self, timeout: float = 10) -> None:
        """
        初始化搜索工具

        参数:
        - timeout: 整次搜索总时限, 默认十秒
        """
        super().__init__(timeout)
        self.base_urls = ["https://cn.bing.com", "https://www.bing.com"]

    def _prepare(self, query: str, max_results: int) -> tuple[str, int, Deadline]:
        """
        校验搜索参数并固定整次执行截止时间

        参数:
        - query: 非空搜索词, 最多 4096 个字符
        - max_results: 正整数, 最大返回二十项

        返回:
        - 去除首尾空格的搜索词, 结果上限和总时限
        """
        if not isinstance(query, str) or not query.strip() or len(query) > 4096:
            raise WebRequestError("invalid_argument", "搜索词须为 1 至 4096 个字符的非空文本")
        if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
            raise WebRequestError("invalid_argument", "max_results 必须为正整数")
        return query.strip(), min(max_results, 20), Deadline(self.timeout)

    def _search_result(self, response: OutboundHTTPResponse, count: int) -> str:
        """
        区分搜索结果, 明确无结果和页面结构变化

        参数:
        - response: 搜索页面响应
        - count: 最大结果数量

        返回:
        - 原有结构的搜索 JSON 列表, 明确无结果时为空列表; 未知页面抛出解析错误
        """
        soup = self._page(response)
        results: list[SearchResult] = []
        for item in soup.select("li.b_algo"):
            anchor = item.select_one("h2 a")
            if anchor is None:
                continue
            href = anchor.get("href")
            if not isinstance(href, str) or urlsplit(href).scheme not in {"http", "https"}:
                continue
            snippet = item.select_one("p")
            results.append({"title": anchor.get_text(strip=True), "url": href,
                            "snippet": snippet.get_text(strip=True) if snippet else ""})
            if len(results) >= count:
                break
        if not results and soup.select_one("#b_results .b_no") is None:
            raise WebRequestError("parse_error", "搜索页面结构无法识别, 不能确认是否无结果")
        return json.dumps(results, ensure_ascii=False, indent=2)


class _FetchCore(_WebCore):
    """网页正文提取, 不把验证页面当作文章"""

    def _extract_text(self, soup: BeautifulSoup) -> str:
        """
        去除脚本和样式, 保留页面可见文本

        参数:
        - soup: 通过状态和验证页面检查的 HTML 解析树

        返回:
        - 去除空行后的页面文本
        """
        for element in soup(["script", "style", "meta", "link", "noscript"]):
            element.decompose()
        lines = (line.strip() for line in soup.get_text(separator="\n", strip=True).splitlines())
        return "\n".join(line for line in lines if line)

    def _format_response(self, response: OutboundHTTPResponse, max_length: int) -> str:
        """
        提取页面标题和正文, 保留原有成功返回结构

        参数:
        - response: 最终受限页面响应
        - max_length: 正文字符上限, 超出时添加截断说明

        返回:
        - 包含最终地址, 标题, 正文和状态的 JSON
        """
        soup = self._page(response)
        title = soup.title.get_text(strip=True) if soup.title else "无标题"
        try:
            text = self._extract_text(soup)
        except Exception as error:
            logger.error(f"[网页工具] 正文解析失败\n{traceback.format_exc()}")
            raise WebRequestError("parse_error", f"解析失败: {type(error).__name__}") from error
        if len(text) > max_length:
            text = text[:max_length] + "...(内容已截断)"
        return json.dumps({"url": response.url, "title": title, "content": text,
                           "status_code": response.status_code}, ensure_ascii=False, indent=2)

    def _prepare_fetch(self, max_length: int) -> Deadline:
        """
        校验正文长度参数并创建总时限

        参数:
        - max_length: 正整数形式的返回字符上限

        返回:
        - 当前调用总时限, 无效参数抛出 invalid_argument
        """
        if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 1:
            raise WebRequestError("invalid_argument", "max_length 必须为正整数")
        return Deadline(self.timeout)
