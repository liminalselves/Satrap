from __future__ import annotations
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit
import http.client
import socket
from ssl import SSLContext, create_default_context
from .utils import (
    DEFAULT_MAX_RESPONSE_BYTES,
    UnsafeOutboundURLError,
    OutboundHTTPError,
    OutboundResponseTooLargeError,
    ResolvedOutboundTarget,
    OutboundHTTPResponse,
    resolve_outbound_http_url,
    resolve_outbound_redirect,
    _merge_query_params,
)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """连接固定 IP 并保留原始 HTTP Host 的连接"""

    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        """
        初始化固定地址 HTTP 连接

        参数:
        - host: 原始 URL 主机名
        - port: 目标端口
        - address: 校验阶段取得的固定 IP
        - timeout: 连接与读取超时秒数
        """
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        """直接连接校验阶段取得的固定 IP"""
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连接固定 IP 并按原始主机名执行 TLS 校验的连接"""

    def __init__(
        self,
        host: str,
        port: int,
        address: str,
        timeout: float,
        context: SSLContext,
    ) -> None:
        """
        初始化固定地址 HTTPS 连接

        参数:
        - host: 原始 URL 主机名和 TLS SNI 名称
        - port: 目标端口
        - address: 校验阶段取得的固定 IP
        - timeout: 连接与读取超时秒数
        - context: TLS 校验上下文
        """
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._ssl_context = context

    def connect(self) -> None:
        """连接固定 IP 并使用原始主机名完成 TLS 握手"""
        raw_socket = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._ssl_context.wrap_socket(raw_socket, server_hostname=self.host)


def _read_sync_response(
    response: http.client.HTTPResponse,
    max_response_bytes: int,
) -> bytes:
    """
    读取同步响应并强制执行字节上限

    参数:
    - response: 标准库 HTTP 响应
    - max_response_bytes: 最大允许响应字节数

    返回:
    - bytes: 未超过限制的完整响应正文
    """
    content_length = response.getheader("Content-Length")
    if content_length:
        try:
            if int(content_length) > max_response_bytes:
                raise OutboundResponseTooLargeError(
                    f"响应超过 {max_response_bytes} 字节限制"
                )
        except ValueError:
            pass
    content = response.read(max_response_bytes + 1)
    if len(content) > max_response_bytes:
        raise OutboundResponseTooLargeError(f"响应超过 {max_response_bytes} 字节限制")
    return content


def _sync_request_once(
    target: ResolvedOutboundTarget,
    *,
    headers: Mapping[str, str] | None,
    timeout: float,
    max_response_bytes: int,
) -> OutboundHTTPResponse:
    """
    对一个已校验目标执行一次固定地址 GET

    参数:
    - target: 已校验并解析的出站目标
    - headers: 请求头
    - timeout: 连接与读取超时秒数
    - max_response_bytes: 最大允许响应字节数

    返回:
    - OutboundHTTPResponse: 完整内存响应
    """
    parsed = urlsplit(target.url)
    request_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    request_headers = {
        key: value for key, value in (headers or {}).items() if key.lower() != "host"
    }
    last_error: OSError | None = None
    for _, address in target.addresses:
        connection: http.client.HTTPConnection
        if parsed.scheme.lower() == "https":
            connection = _PinnedHTTPSConnection(
                target.hostname,
                target.port,
                address,
                timeout,
                create_default_context(),
            )
        else:
            connection = _PinnedHTTPConnection(
                target.hostname, target.port, address, timeout
            )
        try:
            connection.request("GET", request_target, headers=request_headers)
            response = connection.getresponse()
            content = _read_sync_response(response, max_response_bytes)
            response_headers = {key: value for key, value in response.getheaders()}
            return OutboundHTTPResponse(
                url=target.url,
                status_code=response.status,
                headers=response_headers,
                content=content,
            )
        except OSError as error:
            last_error = error
        finally:
            connection.close()
    raise OutboundHTTPError("无法连接已校验的目标地址") from last_error


def safe_sync_get(
    url: str,
    *,
    params: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 15,
    allow_redirects: bool = True,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    trusted_hosts: Iterable[str] = (),
    max_redirects: int = 5,
) -> OutboundHTTPResponse:
    """
    同步执行地址绑定和响应限流的安全 GET 请求

    参数:
    - url: 待访问的绝对 URL
    - params: 追加的结构化查询参数
    - headers: 请求头
    - timeout: 每次连接与读取超时秒数, 默认 15
    - allow_redirects: 是否自动跟随重定向, 默认 True
    - max_response_bytes: 最大允许响应字节数
    - trusted_hosts: 允许访问私网地址的显式主机名
    - max_redirects: 最大重定向次数, 默认 5

    返回:
    - OutboundHTTPResponse: 最终响应或未自动跟随的首个响应
    """
    from . import (
        resolve_outbound_http_url,
        resolve_outbound_redirect,
        _sync_request_once,
    )

    merged_url = _merge_query_params(url, params)
    current = resolve_outbound_http_url(merged_url, trusted_hosts=trusted_hosts)
    for redirect_count in range(max_redirects + 1):
        response = _sync_request_once(
            current,
            headers=headers,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )
        if not allow_redirects or not response.is_redirect:
            return response
        if redirect_count >= max_redirects:
            raise UnsafeOutboundURLError("重定向次数超过限制")
        current = resolve_outbound_redirect(
            current.url,
            response.headers.get("Location", ""),
            trusted_hosts=trusted_hosts,
        )
    raise UnsafeOutboundURLError("重定向次数超过限制")
