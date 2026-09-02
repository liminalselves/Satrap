"""出站 HTTP URL 与网页抓取安全边界测试"""
from __future__ import annotations

import json
import socket
from typing import NoReturn, Protocol

import pytest

from satrap.core.utils.outbound import (
    UnsafeOutboundURLError,
    validate_outbound_http_url,
    validate_outbound_redirect,
)
from satrap.expend.tools import search as search_module
from satrap.expend.tools.search import FetchPageTool, SearchTool


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
