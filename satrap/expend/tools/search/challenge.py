"""识别有限的轻量网页验证模板, 不执行普通页面或外部脚本"""
from __future__ import annotations

from dataclasses import dataclass
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlsplit
from typing import cast
from bs4 import BeautifulSoup
import json
import re

from satrap.core.utils.outbound import OutboundHTTPResponse
from .extraction import HtmlNode, MAX_HTML_BYTES
from .utils import WebRequestError


MAX_SCRIPT_BYTES = 256 * 1024
MAX_JOB_BYTES = 512 * 1024
MAX_REPLY_BYTES = 8192


@dataclass(frozen=True)
class ScriptChallenge:
    """只包含已支持模板的内联源码及页面数据, 不包含网络或文件访问能力"""

    url: str
    data: str
    scripts: tuple[str, ...]

    def payload(self) -> bytes:
        """
        序列化有界 worker 请求

        返回:
        - UTF-8 JSON, 超限抛出 js_challenge_limit
        """
        value = json.dumps({"data": self.data, "scripts": self.scripts}, ensure_ascii=False).encode("utf-8")
        if len(value) > MAX_JOB_BYTES:
            raise WebRequestError("js_challenge_limit", "验证脚本请求超过 512 KiB", status=200)
        return value


def inspect_challenge(response: OutboundHTTPResponse) -> ScriptChallenge | None:
    """
    只识别带 renderData 的 Aliyun acw_sc__v2 内联验证页

    参数:
    - response: 已通过公网请求边界的响应

    返回:
    - 已支持模板或 None; 强验证标记存在但模板不兼容时明确报错
    """
    media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if response.status_code != 200 or media not in {"", "text/html", "application/xhtml+xml"}:
        return None
    if b"aliyun_waf_aa" not in response.content or b"renderData" not in response.content or len(response.content) > MAX_HTML_BYTES:
        return None
    html = response.text
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise WebRequestError("js_challenge_limit", "验证页面超过 HTML 提取预算", status=200)
    soup = cast(HtmlNode, BeautifulSoup(html, "html.parser"))
    node = soup.select_one("textarea#renderData")
    if node is None or soup.select_one("meta[name=aliyun_waf_aa]") is None:
        return None
    data = node.get_text()
    nodes = soup.select("script")
    scripts = tuple(node.get_text() for node in nodes)
    external = any(node.get("src") is not None or node.get("type") not in {None, "", "text/javascript", "application/javascript"} for node in nodes)
    for element in soup.select("head, title, script, style, textarea, meta, link, noscript"):
        if element.name is not None:
            element.decompose()
    if soup.get_text(strip=True):
        return None   # 文章正文或代码示例不得触发验证脚本执行
    if external or not 1 <= len(scripts) <= 3:
        raise WebRequestError("js_challenge_unsupported", "验证模板依赖未支持的脚本加载方式", status=200)
    if len(data.encode("utf-8")) > 4096 or sum(len(script.encode("utf-8")) for script in scripts) > MAX_SCRIPT_BYTES:
        raise WebRequestError("js_challenge_limit", "验证源码或页面数据超过预算", status=200)
    try:
        parsed: object = json.loads(data)
        values = cast(dict[str, object], parsed) if isinstance(parsed, dict) else {}
        source = values.get("l1")
        if not isinstance(source, str) or re.fullmatch(r"var\s+arg1\s*=\s*['\"][0-9a-fA-F]{50}['\"]\s*;?", source) is None:
            raise ValueError("unknown renderData")
    except (ValueError, TypeError) as error:
        raise WebRequestError("js_challenge_unsupported", "验证页面数据不属于已支持模板", status=200) from error
    return ScriptChallenge(response.url, data, scripts)


def validate_cookie(reply: bytes, challenge: ScriptChallenge) -> tuple[str, str]:
    """
    校验 worker 输出, 只允许给原主机写一个模板 Cookie

    参数:
    - reply: worker 的有界 JSON 结果
    - challenge: 可信宿主已识别的当前模板

    返回:
    - Cookie 名和值; 不接受外域, 额外 Cookie, 非重载意图或非法字段
    """
    messages = {"js_runtime_unavailable": "未安装轻量验证依赖, 请安装 Satrap 的 web-js 可选依赖",
                "js_execution_timeout": "验证脚本执行超时", "js_memory_limit": "验证脚本超过 V8 堆预算",
                "js_challenge_unsupported": "验证脚本需要未支持的浏览器接口",
                "js_execution_failed": "验证脚本或 worker 执行失败"}
    try:
        if len(reply) > MAX_REPLY_BYTES:
            raise ValueError("reply too large")
        raw: object = json.loads(reply.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("invalid object")
        value = cast(dict[str, object], raw)
        if value.get("ok") is False and value.get("code") in messages:
            code = cast(str, value["code"])
            raise WebRequestError(code, messages[code], status=200)
        if set(value) != {"ok", "cookie", "reload"} or value["ok"] is not True or value["reload"] is not True:
            raise ValueError("invalid response")
        cookie = value["cookie"]
        if not isinstance(cookie, str) or len(cookie) > 4096 or "\r" in cookie or "\n" in cookie:
            raise ValueError("invalid cookie")
        parsed = SimpleCookie()
        parsed.load(cookie)
        if list(parsed) != ["acw_sc__v2"]:
            raise ValueError("unexpected cookie")
        morsel = parsed["acw_sc__v2"]
        host = urlsplit(challenge.url).hostname
        if morsel["domain"] and morsel["domain"].lstrip(".").lower() != host:
            raise ValueError("foreign domain")
        if morsel["path"] not in {"", "/"} or morsel["httponly"]:
            raise ValueError("invalid scope")
        if not re.fullmatch(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,256}", morsel.value):
            raise ValueError("invalid token")
        return "acw_sc__v2", morsel.value
    except (ValueError, TypeError, KeyError, CookieError) as error:
        raise WebRequestError("js_execution_failed", "验证结果不符合单次 Cookie 协议", status=200) from error
