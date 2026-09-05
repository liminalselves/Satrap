"""出站 HTTP URL 与网页抓取安全边界测试"""
from __future__ import annotations

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from pathlib import Path
import pytest
import socket
from typing import NoReturn, Protocol, cast
import json

from satrap.core.components.message import download_file
from satrap.core.utils.outbound import (
    OutboundResponseTooLargeError,
    ResolvedOutboundTarget,
    TRUSTED_DOWNLOAD_HOSTS_ENV_NAME,
    UnsafeOutboundURLError,
    _PinnedResolver,
    safe_async_get,
    safe_sync_get,
    validate_outbound_http_url,
    validate_outbound_redirect,
)
from satrap.expend.tools.search import FetchPageTool, SearchTool
from satrap.expend.tools import search as search_module


class _Resolver(Protocol):
    """声明测试使用的 DNS 解析函数接口"""

    def __call__(
        self,
        host: str,
        port: int,
        *,
        type: int,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        """
        返回地址解析记录

        参数:
        - host: 待解析主机名
        - port: 目标端口
        - type: 套接字类型

        返回:
        - 地址解析记录
        """
        ...


def _resolver_for(*addresses: str) -> _Resolver:
    """
    构造返回指定 IP 的 getaddrinfo 替身

    参数:
    - addresses: 解析结果 IP

    返回:
    - 与 socket.getaddrinfo 兼容的测试函数
    """
    def resolve(
        host: str,
        port: int,
        *,
        type: int,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        """
        返回固定 IP 的解析记录

        参数:
        - host: 待解析主机名
        - port: 目标端口
        - type: 套接字类型

        返回:
        - 与 socket.getaddrinfo 兼容的解析记录
        """
        family = socket.AF_INET6 if any(":" in item for item in addresses) else socket.AF_INET
        return [(family, type, 6, "", (address, port)) for address in addresses]

    return resolve


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "fe80::1"])
def test_outbound_url_rejects_non_public_addresses(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    """
    出站 URL 校验应拒绝本机, 私网, 链路本地和元数据地址

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    - address: 非公网解析结果
    """
    monkeypatch.setattr(socket, "getaddrinfo", _resolver_for(address))
    with pytest.raises(UnsafeOutboundURLError, match="非公网地址"):
        validate_outbound_http_url("https://attacker.example/resource")


def test_outbound_url_rejects_redirect_to_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    每一跳重定向都必须重新执行公网地址校验

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    monkeypatch.setattr(socket, "getaddrinfo", _resolver_for("127.0.0.1"))
    with pytest.raises(UnsafeOutboundURLError, match="非公网地址"):
        validate_outbound_redirect("https://public.example/start", "http://metadata.local/latest")


def test_trusted_service_host_may_use_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    显式配置的服务主机允许使用私网地址而不扩大到其他主机

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    monkeypatch.setattr(socket, "getaddrinfo", _resolver_for("10.0.0.2"))
    assert validate_outbound_http_url(
        "https://misskey.internal/file",
        trusted_hosts=("misskey.internal",),
    ) == "https://misskey.internal/file"
    with pytest.raises(UnsafeOutboundURLError):
        validate_outbound_http_url("https://other.internal/file")


def test_fetch_page_rejects_ssrf_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    FetchPageTool 应在发起请求前拒绝私网目标

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    monkeypatch.setattr(socket, "getaddrinfo", _resolver_for("169.254.169.254"))
    called = False

    def fail_get(*args: object, **kwargs: object) -> NoReturn:
        """
        记录意外网络调用并立即失败

        参数:
        - args: 位置参数
        - kwargs: 关键字参数

        返回:
        - 不返回结果, 始终抛出 AssertionError
        """
        nonlocal called
        called = True
        raise AssertionError("不得发起网络请求")

    monkeypatch.setattr(search_module, "_requests_get", fail_get)
    result = json.loads(FetchPageTool().execute("http://metadata.example/latest"))
    assert called is False
    assert "非公网地址" in result["error"]


def test_search_uses_structured_query_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    搜索关键词应通过 params 编码而不是直接拼接到 URL

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    captured: dict[str, object] = {}

    class Response:
        text = '<li class="b_algo"><h2><a href="https://example.com">title</a></h2></li>'

        def raise_for_status(self) -> None:
            """模拟成功响应"""

    def fake_get(url: str, **kwargs: object) -> Response:
        """
        记录结构化搜索请求

        参数:
        - url: 搜索服务地址
        - kwargs: 请求关键字参数

        返回:
        - 固定 HTML 响应
        """
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(search_module, "_requests_get", fake_get)
    result = json.loads(SearchTool().execute("a&count=999", max_results=5))

    assert result[0]["title"] == "title"
    assert captured["url"] == "https://cn.bing.com/search"
    assert captured["params"] == {"q": "a&count=999", "count": 5}


# ---------- safe_sync_get / safe_async_get 固定地址行为 ----------

_TRUST_LOOPBACK = ("127.0.0.1",)


class _OutboundHandler(BaseHTTPRequestHandler):
    """回环测试服务器的固定路由处理器"""

    def log_message(self, format: str, *args: object) -> None:
        """静默请求日志"""

    def do_GET(self) -> None:
        """按固定路由响应, 覆盖正文/重定向/超限三类行为"""
        if self.path == "/ok":
            self._respond(200, b"hello-outbound")
        elif self.path == "/big":
            self._respond(200, b"x" * (64 * 1024))
        elif self.path == "/stream-big":
            self.send_response(200)   # HTTP/1.0 关闭定界, 无 Content-Length
            self.end_headers()
            self.wfile.write(b"y" * (64 * 1024))
        elif self.path == "/redirect":
            self._redirect("/ok")
        elif self.path == "/redirect-private":
            self._redirect("http://169.254.169.254/latest/meta-data")
        elif self.path == "/redirect-loop":
            self._redirect("/redirect-loop")
        else:
            self._respond(404, b"not found")

    def _respond(self, status: int, body: bytes) -> None:
        """
        发送带 Content-Length 的响应

        参数:
        - status: HTTP 状态码
        - body: 响应正文
        """
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        """
        发送 302 重定向

        参数:
        - location: Location 响应头
        """
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture()
def local_server() -> Iterator[str]:
    """启动回环测试服务器并返回 base URL (端口随机)"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OutboundHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _, port = cast("tuple[str, int]", server.server_address)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_safe_sync_get_rejects_private_target_without_trust(local_server: str) -> None:
    """未配置可信主机时回环地址应在连接前被拒绝"""
    with pytest.raises(UnsafeOutboundURLError):
        safe_sync_get(f"{local_server}/ok")


@pytest.mark.asyncio
async def test_safe_async_get_rejects_private_target_without_trust(local_server: str) -> None:
    """未配置可信主机时回环地址应在连接前被拒绝 (异步)"""
    with pytest.raises(UnsafeOutboundURLError):
        await safe_async_get(f"{local_server}/ok")


def test_safe_sync_get_downloads_from_trusted_loopback(local_server: str) -> None:
    """可信回环主机应完成固定地址下载"""
    response = safe_sync_get(f"{local_server}/ok", trusted_hosts=_TRUST_LOOPBACK)
    assert response.status_code == 200
    assert response.content == b"hello-outbound"


@pytest.mark.asyncio
async def test_safe_async_get_downloads_from_trusted_loopback(local_server: str) -> None:
    """可信回环主机应完成固定地址下载 (异步)"""
    response = await safe_async_get(f"{local_server}/ok", trusted_hosts=_TRUST_LOOPBACK)
    assert response.status_code == 200
    assert response.content == b"hello-outbound"


@pytest.mark.asyncio
async def test_safe_async_get_follows_redirect_with_per_hop_validation(local_server: str) -> None:
    """同主机重定向应逐跳校验后跟随"""
    response = await safe_async_get(f"{local_server}/redirect", trusted_hosts=_TRUST_LOOPBACK)
    assert response.status_code == 200
    assert response.content == b"hello-outbound"
    assert response.url.endswith("/ok")


@pytest.mark.asyncio
async def test_safe_async_get_blocks_redirect_to_private_address(local_server: str) -> None:
    """重定向到非公网地址应在该跳被拒绝"""
    with pytest.raises(UnsafeOutboundURLError):
        await safe_async_get(f"{local_server}/redirect-private", trusted_hosts=_TRUST_LOOPBACK)


@pytest.mark.asyncio
async def test_safe_async_get_enforces_redirect_limit(local_server: str) -> None:
    """循环重定向应触发次数上限"""
    with pytest.raises(UnsafeOutboundURLError, match="重定向次数超过限制"):
        await safe_async_get(f"{local_server}/redirect-loop", trusted_hosts=_TRUST_LOOPBACK, max_redirects=3)


@pytest.mark.asyncio
async def test_safe_async_get_enforces_byte_limit(local_server: str) -> None:
    """Content-Length 超限应在读取前拒绝"""
    with pytest.raises(OutboundResponseTooLargeError):
        await safe_async_get(
            f"{local_server}/big", trusted_hosts=_TRUST_LOOPBACK, max_response_bytes=1024
        )


@pytest.mark.asyncio
async def test_safe_async_get_enforces_streaming_byte_limit(local_server: str) -> None:
    """无 Content-Length 的响应应由流式计数拦截"""
    with pytest.raises(OutboundResponseTooLargeError):
        await safe_async_get(
            f"{local_server}/stream-big", trusted_hosts=_TRUST_LOOPBACK, max_response_bytes=1024
        )


def test_safe_sync_get_enforces_streaming_byte_limit(local_server: str) -> None:
    """无 Content-Length 的响应应由限量读取拦截 (同步)"""
    with pytest.raises(OutboundResponseTooLargeError):
        safe_sync_get(
            f"{local_server}/stream-big", trusted_hosts=_TRUST_LOOPBACK, max_response_bytes=1024
        )


@pytest.mark.asyncio
async def test_pinned_resolver_only_returns_validated_addresses() -> None:
    """固定解析器应拒绝错配主机且仅返回校验地址"""
    target = ResolvedOutboundTarget(
        url="http://example.com",
        hostname="example.com",
        port=80,
        addresses=((socket.AF_INET, "93.184.216.34"),),
    )
    resolver = _PinnedResolver(target)
    with pytest.raises(OSError):
        await resolver.resolve("evil.example.com", 80)
    results = await resolver.resolve("example.com", 80)
    assert [item["host"] for item in results] == ["93.184.216.34"]


@pytest.mark.asyncio
async def test_download_file_uses_env_trusted_hosts(
    local_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """download_file 默认读取 SATRAP_TRUSTED_DOWNLOAD_HOSTS 环境变量"""
    monkeypatch.delenv(TRUSTED_DOWNLOAD_HOSTS_ENV_NAME, raising=False)
    target = tmp_path / "file.bin"
    with pytest.raises(UnsafeOutboundURLError):
        await download_file(f"{local_server}/ok", str(target))

    monkeypatch.setenv(TRUSTED_DOWNLOAD_HOSTS_ENV_NAME, "127.0.0.1")
    saved = await download_file(f"{local_server}/ok", str(target))
    assert Path(saved).read_bytes() == b"hello-outbound"


@pytest.mark.asyncio
async def test_download_file_accepts_explicit_trusted_hosts(
    local_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """download_file 显式 trusted_hosts 应覆盖环境变量缺省"""
    monkeypatch.delenv(TRUSTED_DOWNLOAD_HOSTS_ENV_NAME, raising=False)
    target = tmp_path / "file.bin"
    saved = await download_file(f"{local_server}/ok", str(target), trusted_hosts=["127.0.0.1"])
    assert Path(saved).read_bytes() == b"hello-outbound"
