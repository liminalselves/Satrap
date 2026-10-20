import asyncio
from typing import cast
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import threading
import socket
import time
import gzip
import json

import pytest

from satrap.core.utils.outbound import resolve_outbound_http_url, OutboundResponseTooLargeError
from satrap.expend.tools.search import SearchTool, AsyncSearchTool, FetchPageTool, AsyncFetchPageTool
from satrap.expend.tools.search import transport
from satrap.expend.tools.search.transport import BrowserTransport, AsyncBrowserTransport
from satrap.expend.tools.search.utils import WebPolicy, Deadline, WebRequestError, retry_delay


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.base = ""
        self.calls: list[tuple[str, dict[str, str], float]] = []
        self.started = threading.Event()
        self.counts = Counter()
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), WebHandler)


class WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def respond(self, body=b"", status=200, headers=None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        server = cast(WebServer, self.server)
        path = urlsplit(self.path).path
        with server.lock:
            server.calls.append((self.path, dict(self.headers), time.monotonic()))
            server.counts[path] += 1
        if path.endswith("/search"):
            query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
            if query == "none":
                return self.respond(b'<ol id="b_results"><li class="b_no">No results</li></ol>')
            if query == "challenge":
                return self.respond(b'<title>Verify</title><form action="/turing/captcha"></form>')
            if query == "unknown":
                return self.respond(b"<title>New search layout</title><p>Unknown</p>")
            if query == "fallback" and path.startswith("/primary/"):
                return self.respond(b"busy", 503)
            body = '<ol id="b_results">' + "".join(
                f'<li class="b_algo"><h2><a href="https://example.com/{i}">标题{i}</a></h2><p>摘要{i}</p></li>'
                for i in range(6)
            ) + "</ol>"
            return self.respond(body.encode())
        if path == "/cookie":
            return self.respond(status=302, headers={"Location": "/needs-cookie", "Set-Cookie": "web_test=yes; Path=/"})
        if path == "/needs-cookie":
            status = 200 if "web_test=yes" in self.headers.get("Cookie", "") else 401
            return self.respond(b"cookie-present", status)
        if path == "/private":
            return self.respond(status=302, headers={"Location": "http://169.254.169.254/latest/meta-data"})
        if path == "/loop":
            return self.respond(status=302, headers={"Location": "/loop"})
        if path == "/gzip":
            return self.respond(gzip.compress(b"x" * (128 * 1024)), headers={"Content-Encoding": "gzip"})
        if path == "/gzip-full":
            return self.respond(gzip.compress(b"x" * (9 * 1024 * 1024)), headers={"Content-Encoding": "gzip"})
        if path.startswith("/status/"):
            return self.respond(b"http-status-body", int(path.rsplit("/", 1)[1]))
        if path == "/big":
            return self.respond(b"x" * (128 * 1024))
        if path == "/stream":
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for _ in range(128):
                    self.wfile.write(b"x" * 4096)
                    self.wfile.flush()
                    time.sleep(0.001)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            self.close_connection = True
            return
        if path == "/denied":
            return self.respond(b"denied", 403)
        if path == "/limited":
            return self.respond(b"limited", 429, {"Retry-After": "120"})
        if path == "/challenge":
            return self.respond(b'<form id="challenge-form"></form>')
        if path == "/slow":
            server.started.set()
            time.sleep(0.25)
        if path == "/redirect-slow":
            time.sleep(0.08)
            return self.respond(status=302, headers={"Location": "/redirect-slow"})
        return self.respond('<title>测试页面</title><p>正文提到验证码不应误判</p><script>隐藏</script>'.encode())


@pytest.fixture
def web_server(monkeypatch):
    server = WebServer()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    original = socket.getaddrinfo

    def dns(host, port, *args, **kwargs):
        if host.rstrip(".").lower() == "public.web.test":
            return original("127.0.0.1", port, *args, **kwargs)
        return original(host, port, *args, **kwargs)

    def resolve(url):
        return resolve_outbound_http_url(url, trusted_hosts=("public.web.test",))

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(transport, "resolve_outbound_http_url", resolve)
    server.base = f"http://public.web.test:{server.server_port}"
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=1)


def tools(server: WebServer, policy: WebPolicy | None = None) -> tuple[SearchTool, AsyncSearchTool, FetchPageTool, AsyncFetchPageTool]:
    policy = policy or WebPolicy(interval=0)
    result = (SearchTool(), AsyncSearchTool(), FetchPageTool(), AsyncFetchPageTool())
    for tool in result:
        tool.policy = policy
    for tool in (result[0], result[1]):
        tool.base_urls = [server.base + "/primary", server.base + "/secondary"]
    return result


@pytest.mark.asyncio
async def test_real_browser_search_fetch_cache_and_cookie_lifetime(web_server, monkeypatch):
    """完整走实际 curl 请求, 验证绑定地址, 中文解析, 缓存共享与单次 Cookie 生命周期"""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    sync, asynchronous, fetch, async_fetch = tools(web_server)
    first = json.loads(sync.execute("a&count=999", 2))
    assert len(first) == 2 and first[0]["title"] == "标题0"
    assert json.loads(await asynchronous.execute("a&count=999", 2)) == first
    assert len(web_server.calls) == 1
    path, headers, _ = web_server.calls[0]
    assert parse_qs(urlsplit(path).query) == {"q": ["a&count=999"], "count": ["2"]}
    assert headers["Host"].startswith("public.web.test:")
    assert "Chrome/" in headers["User-Agent"]
    assert "Chrome/92." not in headers["User-Agent"]
    assert headers["Accept-Language"].startswith("zh-CN")
    dotted = web_server.base.replace("public.web.test", "PUBLIC.WEB.TEST.")
    assert "content" in json.loads(await async_fetch.execute(dotted + "/ok"))
    assert "content" in json.loads(fetch.execute(dotted + "/ok"))
    page = json.loads(fetch.execute(web_server.base + "/ok"))
    assert page["title"] == "测试页面" and "正文提到验证码" in page["content"]
    assert "隐藏" not in page["content"]
    assert json.loads(await async_fetch.execute(web_server.base + "/ok")) == page
    for tool in (fetch, async_fetch):
        value = tool.execute(web_server.base + "/cookie")
        if asyncio.iscoroutine(value):
            value = await value
        assert json.loads(value)["content"] == "cookie-present"
        value = tool.execute(web_server.base + "/needs-cookie")
        if asyncio.iscoroutine(value):
            value = await value
        assert json.loads(value)["status_code"] == 401


@pytest.mark.asyncio
async def test_search_result_states_fallback_and_argument_validation(web_server):
    """正常无结果, 未知页面, 备用入口和参数错误具有相同的同步异步行为"""
    for query, expected in (("none", []), ("unknown", "parse_error"), ("fallback", "success")):
        results = []
        for asynchronous in (False, True):
            sync, async_tool, _, _ = tools(web_server)
            value = await async_tool.execute(query) if asynchronous else sync.execute(query)
            result = json.loads(value)
            if expected == "success":
                assert len(result) == 5
            elif expected == []:
                assert result == []
            else:
                assert result["code"] == expected
            results.append(result)
        assert results[0] == results[1]
    assert web_server.counts["/secondary/search"] == 2
    before = len(web_server.calls)
    sync, asynchronous, fetch, async_fetch = tools(web_server)
    for query, count in (("", 5), ("word", 0), ("word", 1.5), ("word", True)):
        assert json.loads(sync.execute(query, cast(int, count)))["code"] == "invalid_argument"
        assert json.loads(await asynchronous.execute(query, cast(int, count)))["code"] == "invalid_argument"
    assert json.loads(fetch.execute(web_server.base, 0))["code"] == "invalid_argument"
    assert json.loads(await async_fetch.execute(web_server.base, 0))["code"] == "invalid_argument"
    assert len(web_server.calls) == before


@pytest.mark.asyncio
async def test_fetch_status_truncation_and_parse_errors(web_server, monkeypatch):
    """集中验证非成功状态, 正文截断与解析异常的同步异步返回契约"""
    _, _, fetch, async_fetch = tools(web_server)
    for status in (201, 204, 404, 503):
        url = web_server.base + f"/status/{status}"
        sync_result = json.loads(fetch.execute(url))
        assert sync_result == json.loads(await async_fetch.execute(url))
        assert sync_result["code"] == "http_error" and sync_result["status_code"] == status
    url = web_server.base + "/ok"
    result = json.loads(fetch.execute(url, 4))
    assert result == json.loads(await async_fetch.execute(url, 4))
    assert result["content"] == "测试页面...(内容已截断)"
    for tool in (fetch, async_fetch):
        monkeypatch.setattr(tool, "_extract_text", lambda soup: 1 / 0)
    assert json.loads(fetch.execute(url))["code"] == "parse_error"
    assert json.loads(await async_fetch.execute(url))["code"] == "parse_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("path,code,delay", [("/denied", "access_denied", 60),
                                           ("/limited", "rate_limited", 120),
                                           ("/challenge", "verification_required", 60)])
async def test_http_and_200_challenges_cool_down_across_tool_types(web_server, path, code, delay):
    """原始失败与共享冷却均不重复访问同站点, 200 验证页也不成为正文"""
    sync, asynchronous, fetch, async_fetch = tools(web_server)
    first = json.loads(await async_fetch.execute(web_server.base + path))
    assert first["code"] == code and first["retry_after"] == delay
    before = len(web_server.calls)
    for result in (fetch.execute(web_server.base + "/ok"), sync.execute("word"), await asynchronous.execute("word")):
        parsed = json.loads(result)
        assert parsed["code"] == code and parsed["retry_after"] >= delay - 1
    assert len(web_server.calls) == before


@pytest.mark.asyncio
async def test_search_challenge_has_no_fallback_or_cache(web_server):
    sync, asynchronous, _, _ = tools(web_server)
    assert json.loads(sync.execute("challenge"))["code"] == "verification_required"
    assert json.loads(await asynchronous.execute("challenge"))["code"] == "verification_required"
    assert web_server.counts["/primary/search"] == 1
    assert web_server.counts["/secondary/search"] == 0


@pytest.mark.asyncio
async def test_private_addresses_and_redirects_never_reach_server(web_server):
    """实际 HTTP 入口在网络前拒绝私网, 包括公开网页重定向后的私网目标"""
    for asynchronous in (False, True):
        _, _, sync, async_tool = tools(web_server)
        for url in ("http://127.0.0.1/", "http://169.254.169.254/latest", "file:///etc/passwd",
                    "https://name:secret@example.com/", web_server.base + "/private"):
            result = await async_tool.execute(url) if asynchronous else sync.execute(url)
            assert json.loads(result)["code"] == "unsafe_url"
    assert len(web_server.calls) == 2
    assert all(urlsplit(call[0]).path == "/private" for call in web_server.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_stream_gzip_limits_and_redirect_limits(web_server, asynchronous):
    """通过实际 curl 回调中止有长度, 流式和压缩后的超限正文"""
    for path in ("/big", "/stream", "/gzip"):
        if asynchronous:
            async with AsyncBrowserTransport(Deadline(3), WebPolicy(interval=0), max_bytes=4096) as client:
                with pytest.raises(OutboundResponseTooLargeError):
                    await client.get(web_server.base + path)
        else:
            with BrowserTransport(Deadline(3), WebPolicy(interval=0), max_bytes=4096) as client:
                with pytest.raises(OutboundResponseTooLargeError):
                    client.get(web_server.base + path)
    _, _, fetch, async_fetch = tools(web_server)
    result = await async_fetch.execute(web_server.base + "/loop") if asynchronous else fetch.execute(web_server.base + "/loop")
    assert json.loads(result)["code"] == "redirect_limit"
    assert web_server.counts["/loop"] == 6
    result = await async_fetch.execute(web_server.base + "/gzip-full") if asynchronous else fetch.execute(web_server.base + "/gzip-full")
    assert json.loads(result)["code"] == "response_too_large"


@pytest.mark.asyncio
async def test_total_deadline_and_cancellation_release_network_slot(web_server):
    """重定向总时限不会重置, 取消正在读取的异步请求后站点能继续使用"""
    _, _, fetch, async_fetch = tools(web_server)
    fetch.timeout = async_fetch.timeout = 0.16
    for tool in (fetch, async_fetch):
        started = time.monotonic()
        value = tool.execute(web_server.base + "/redirect-slow")
        if asyncio.iscoroutine(value):
            value = await value
        assert json.loads(value)["code"] == "timeout"
        assert time.monotonic() - started < 0.7
    async_fetch.timeout = 3
    pending = asyncio.create_task(async_fetch.execute(web_server.base + "/slow"))
    assert await asyncio.to_thread(web_server.started.wait, 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert "content" in json.loads(await async_fetch.execute(web_server.base + "/ok"))


@pytest.mark.asyncio
async def test_dns_deadline_and_cancellation_do_not_start_late_requests(web_server, monkeypatch):
    """超时或取消遗留的后台 DNS 仅能结束解析, 不能迟到后继续发起 HTTP"""
    original = transport.resolve_outbound_http_url
    entered = threading.Event()

    def slow_dns(url):
        entered.set()
        time.sleep(0.15)
        return original(url)

    monkeypatch.setattr(transport, "resolve_outbound_http_url", slow_dns)
    _, _, fetch, async_fetch = tools(web_server)
    fetch.timeout = async_fetch.timeout = 0.03
    assert json.loads(fetch.execute(web_server.base + "/ok"))["code"] == "timeout"
    assert json.loads(await async_fetch.execute(web_server.base + "/ok"))["code"] == "timeout"
    entered.clear()
    async_fetch.timeout = 2
    pending = asyncio.create_task(async_fetch.execute(web_server.base + "/ok"))
    assert await asyncio.to_thread(entered.wait, 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await asyncio.sleep(0.2)
    assert web_server.calls == []
    monkeypatch.setattr(transport, "resolve_outbound_http_url", original)
    assert "content" in json.loads(await async_fetch.execute(web_server.base + "/ok"))


@pytest.mark.asyncio
async def test_shared_limiter_queue_deadline_and_cache_expiry(web_server):
    """不同线程和事件循环入口共享限流, 等待有总时限, 缓存到期后重新请求"""
    policy = WebPolicy(interval=0.08, cache_ttl=0.03)
    sync, asynchronous, fetch, async_fetch = tools(web_server, policy)
    await asyncio.gather(asyncio.to_thread(fetch.execute, web_server.base + "/ok"),
                         async_fetch.execute(web_server.base + "/ok"),
                         async_fetch.execute(web_server.base + "/ok"))
    starts = [call[2] for call in web_server.calls]
    assert all(right - left >= 0.065 for left, right in zip(starts, starts[1:]))
    policy.acquire("queue.web.test", Deadline(1))
    try:
        with pytest.raises(WebRequestError, match="超时"):
            await policy.acquire_async("queue.web.test", Deadline(0.02))
    finally:
        policy.release("queue.web.test")
    sync.execute("cached")
    before = len(web_server.calls)
    await asynchronous.execute("cached")
    assert len(web_server.calls) == before
    await asyncio.sleep(0.04)
    await asynchronous.execute("cached")
    assert len(web_server.calls) == before + 1
    for index in range(140):
        policy.remember(str(index), 1, "[]")
    assert policy.cached("0", 1) is None and policy.cached("139", 1) == "[]"
    bounded = WebPolicy()
    bounded.remember("one", 1, "x" * (5 * 1024 * 1024))
    bounded.remember("two", 1, "y" * (5 * 1024 * 1024))
    assert bounded.cached("one", 1) is None and bounded.cached("two", 1) is not None
    bounded.remember("too-large", 1, "x" * (9 * 1024 * 1024))
    assert bounded.cached("too-large", 1) is None
    assert retry_delay("not a date") == 60
    assert retry_delay("120") == 120
