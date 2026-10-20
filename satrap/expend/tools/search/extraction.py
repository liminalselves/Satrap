"""对已下载的网页进行本地正文提取, 不执行脚本或发起额外网络请求"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from trafilatura import extract
import threading
import traceback
import asyncio
from typing import Literal, Protocol, cast
from bs4 import BeautifulSoup

from satrap.core.utils.outbound import OutboundHTTPResponse
from .utils import Deadline, WebRequestError

from satrap.core.log import logger


MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
_PARSE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="satrap-web-text")
_PARSE_SLOTS = threading.BoundedSemaphore(10)
_JS_NOTICES = (
    "you need to enable javascript to run this app",
    "please enable javascript to continue",
    "please enable javascript to view this page",
    "javascript is required to view this page",
    "请启用javascript", "请开启javascript", "请启用 javascript", "请开启 javascript",
)


@dataclass(frozen=True)
class ExtractedPage:
    """保存提取后正文与来源说明, 不持有 HTML 或 Cookie"""

    url: str
    title: str
    content: str
    extraction: Literal["main", "fallback", "plain"]
    warnings: tuple[str, ...] = ()


class HtmlNode(Protocol):
    """约束 BeautifulSoup 动态节点接口, 避免旧版无标注依赖向正文逻辑传播 Unknown"""

    name: str | None

    def get_text(self, separator: str = "", strip: bool = False) -> str: ...
    def select(self, selector: str) -> list[HtmlNode]: ...
    def select_one(self, selector: str) -> HtmlNode | None: ...
    def find_parent(self, name: str) -> HtmlNode | None: ...
    def get(self, key: str) -> object: ...
    def decompose(self) -> None: ...


def response_type(response: OutboundHTTPResponse) -> str:
    """
    校验状态和文本类型

    参数:
    - response: 已下载的受限 HTTP 响应

    返回:
    - 小写媒体类型, 未声明时按 HTML 兼容处理
    """
    if response.status_code != 200:
        code = {403: "access_denied", 429: "rate_limited"}.get(response.status_code, "http_error")
        raise WebRequestError(code, f"HTTP {response.status_code}", status=response.status_code,
                              retryable=response.status_code >= 500)
    media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media_type and media_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
        raise WebRequestError("unsupported_content", "目标未返回可读取的网页文本", status=200)
    return media_type


def _text(node: HtmlNode) -> str:
    """
    读取节点中的可见段落文本

    参数:
    - node: 已清理脚本的节点

    返回:
    - 保留非空行的文本
    """
    return "\n".join(line.rstrip() for line in node.get_text("\n").splitlines() if line.strip())


def html_page(response: OutboundHTTPResponse) -> HtmlNode:
    """
    解析有界 HTML 并识别明确验证页

    参数:
    - response: 已下载的受限响应

    返回:
    - HTML 树; 明确验证页抛出带原 HTTP 状态的错误
    """
    response_type(response)
    if len(response.content) > MAX_HTML_BYTES:
        raise WebRequestError("extraction_limit", "HTML 超过 2 MiB 提取上限", status=200)
    html = response.text
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise WebRequestError("extraction_limit", "解码后的 HTML 超过 2 MiB 提取上限", status=200)
    soup = cast(HtmlNode, BeautifulSoup(html, "html.parser"))
    form = soup.select_one("form#challenge-form, form#captcha-form, form[action*='/turing/']")
    script = soup.select_one("script[src*='/cdn-cgi/challenge-platform/']")
    title_node = soup.select_one("title")
    title = title_node.get_text(strip=True).lower() if title_node else ""
    article = soup.select_one("article p, main p, [role=main] p, article pre, main pre")
    paragraphs = [p.get_text(strip=True) for p in soup.select("p") if p.find_parent("form") is None]
    has_body = bool(article and article.get_text(strip=True)) or (
        len(paragraphs) >= 2 and sum(map(len, paragraphs)) >= 200)
    if not has_body and (form or (script and title in {"just a moment...", "attention required! | cloudflare"})):
        raise WebRequestError("verification_required", "网站要求浏览器验证, 未取得有效正文",
                              status=200, retry_after=60)
    return soup


def _clean(soup: HtmlNode) -> None:
    """
    删除明确的页面噪声, 保留正文容器和代码

    参数:
    - soup: 可就地修改的页面解析树
    """
    for node in soup.select("head, title, script, style, meta, link, noscript, nav, footer, aside, form, [role=navigation], [role=banner], [hidden], [aria-hidden=true]"):
        if node.name is not None:
            node.decompose()


def _fallback(soup: HtmlNode) -> str:
    """
    按段落与链接占比选择正文容器, 最后回退可见整页

    参数:
    - soup: 已去除明确噪声的页面树

    返回:
    - 最佳候选的文本或清理后的整页文本
    """
    candidates: list[tuple[float, str]] = []
    for node in soup.select("article, main, [role=main]"):
        text = _text(node)
        if not text:
            continue
        linked = sum(len(a.get_text(strip=True)) for a in node.select("a"))
        ratio = min(1.0, linked / len(text))
        paragraphs = len(node.select("p, pre, li, tr"))
        score = len(text) * (1 - ratio) ** 2 * (1 + min(paragraphs, 20) / 20)
        if ratio < 0.5:
            candidates.append((score, text))
    return max(candidates, key=lambda item: item[0])[1] if candidates else _text(soup)


def extract_page(response: OutboundHTTPResponse) -> ExtractedPage:
    """
    提取完整正文并保守分类空壳页面

    参数:
    - response: 已下载的受限网页响应

    返回:
    - 完整正文及提取方式; 无正文或超过预算时明确报错
    """
    if response_type(response) == "text/plain":
        text = response.text
        title, method, warnings = "无标题", "plain", ()
    else:
        soup = html_page(response)
        title_node = soup.select_one("title")
        title = title_node.get_text(strip=True) if title_node else "无标题"
        has_script = soup.select_one("script") is not None
        notices = [node.get_text(" ", strip=True) for node in soup.select("noscript")]
        _clean(soup)
        visible = _text(soup)
        js_hint = any(hint in " ".join([visible, *notices]).lower() for hint in _JS_NOTICES)
        remainder = visible.lower()
        for hint in _JS_NOTICES:
            remainder = remainder.replace(hint, "")
        remainder = remainder.strip(" \n\r\t.!。…")
        shell = not any(char.isalnum() for char in remainder) or remainder in {"loading", "加载中"}
        if has_script and js_hint and shell:
            raise WebRequestError("javascript_required", "页面为空壳并要求执行 JavaScript, 未取得正文", status=200)
        if not any(char.isalnum() for char in visible):
            raise WebRequestError("content_unavailable", "HTTP 200, 但未取得可读取的正文", status=200)
        try:
            html = f"<html><body>{soup.select_one('body') or soup}</body></html>"
            # 为 HTML 片段补齐根节点, 避免首个 article 被解析器误作整个根节点而丢失正文
            text = extract(html, output_format="markdown", include_comments=False,
                           include_tables=True, include_formatting=True, include_links=False,
                           favor_precision=True, deduplicate=False) or ""
        except Exception:
            logger.error(f"[网页工具] 主正文提取失败, 尝试清理文本\n{traceback.format_exc()}")
            text = ""
        if text.strip():
            method, warnings = "main", ()
        else:
            text = _fallback(soup)
            method, warnings = "fallback", ("未能确认主正文, 返回清理后的候选或整页文本",)
            logger.warning("[网页工具] 未能确认主正文, 回退到清理后的候选或整页文本")
    if method == "main":
        text = text.strip()
    if not any(char.isalnum() for char in text):
        raise WebRequestError("content_unavailable", "HTTP 200, 但未取得可读取的正文", status=200)
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise WebRequestError("extraction_limit", "提取正文超过 1 MiB 快照上限", status=200)
    return ExtractedPage(response.url, title, text, method, warnings)


def _parse_work(response: OutboundHTTPResponse, abandoned: threading.Event) -> ExtractedPage | None:
    """
    实际出队后才释放解析名额, 已放弃的排队请求不再解析

    参数:
    - response: 待解析响应
    - abandoned: 调用者超时或取消后设置的标记

    返回:
    - 正文结果, 已放弃且尚未开始时返回 None
    """
    try:
        return None if abandoned.is_set() else extract_page(response)
    except Exception as error:
        if abandoned.is_set():
            if isinstance(error, WebRequestError):
                logger.warning(f"[网页工具] 已放弃的后台解析结束, 类别={error.code}")
            else:
                logger.error(f"[网页工具] 已放弃的后台解析失败\n{traceback.format_exc()}")
        raise
    finally:
        _PARSE_SLOTS.release()


async def extract_async(response: OutboundHTTPResponse, deadline: Deadline) -> ExtractedPage:
    """
    在有界线程池中解析, 超时和取消不提前释放仍在工作的槽位

    参数:
    - response: 已下载的受限响应
    - deadline: 网络和解析共用的截止时间

    返回:
    - 提取后的完整正文; 取消继续向上传递
    """
    remaining = deadline.remaining()
    if not _PARSE_SLOTS.acquire(blocking=False):
        raise WebRequestError("busy", "网页正文解析队列已满", status=response.status_code)
    abandoned = threading.Event()
    try:
        future = _PARSE_POOL.submit(_parse_work, response, abandoned)
    except BaseException:
        _PARSE_SLOTS.release()
        raise
    wrapped = asyncio.wrap_future(future)
    wrapped.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
    # 观察迟到异常, 不让已超时的调用留下无人接收的 Future 异常
    try:
        page = await asyncio.wait_for(asyncio.shield(wrapped), timeout=remaining)
        # shield 防止取消仅改变 Future 状态却将响应留在队列中, 名额必须等实际出队才能释放
        if page is None:
            raise WebRequestError("timeout", "网页正文解析已取消", status=response.status_code)
        return page
    except TimeoutError as error:
        abandoned.set()
        raise WebRequestError("timeout", "网页正文解析超时", status=response.status_code) from error
    except asyncio.CancelledError:
        abandoned.set()
        raise
