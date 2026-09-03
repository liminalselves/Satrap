"""
出站 HTTP 地址校验与安全请求

在请求和重定向前解析目标主机, 默认仅允许全局可路由地址,
并将已校验的地址绑定到实际连接以阻断 DNS 重绑定窗口
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from email.message import Message
from http import HTTPStatus
from ssl import SSLContext, create_default_context
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult


DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
"""普通出站响应的默认最大字节数"""

DEFAULT_MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
"""媒体与文件下载的默认最大字节数"""

_REDIRECT_STATUSES = {
    HTTPStatus.MOVED_PERMANENTLY,
    HTTPStatus.FOUND,
    HTTPStatus.SEE_OTHER,
    HTTPStatus.TEMPORARY_REDIRECT,
    HTTPStatus.PERMANENT_REDIRECT,
}
"""GET 请求允许跟随的重定向状态码"""


class UnsafeOutboundURLError(ValueError):
    """出站 URL 违反网络访问边界"""


class OutboundHTTPError(RuntimeError):
    """安全出站 HTTP 请求失败"""


class OutboundResponseTooLargeError(OutboundHTTPError):
    """出站 HTTP 响应超过大小限制"""


@dataclass(frozen=True)
class ResolvedOutboundTarget:
    """已校验并解析的出站 HTTP 目标"""

    url: str
    hostname: str
    port: int
    addresses: tuple[tuple[socket.AddressFamily, str], ...]


@dataclass
class OutboundHTTPResponse:
    """安全出站 HTTP 请求的内存响应"""

    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    encoding: str = "utf-8"

    @property
    def text(self) -> str:
        """按当前编码解码响应正文"""
        try:
            return self.content.decode(self.encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    @property
    def apparent_encoding(self) -> str | None:
        """返回响应头声明的字符编码, 缺失时回退 UTF-8"""
        content_type = self.headers.get("Content-Type", "")
        message = Message()
        message["content-type"] = content_type
        return message.get_content_charset() or "utf-8"

    @property
    def is_redirect(self) -> bool:
        """判断响应是否要求客户端继续重定向"""
        return self.status_code in _REDIRECT_STATUSES and bool(self.headers.get("Location"))

    def raise_for_status(self) -> None:
        """在 HTTP 状态码表示失败时抛出异常"""
        if self.status_code >= 400:
            raise OutboundHTTPError(f"HTTP {self.status_code}")


def normalize_hostname(hostname: str) -> str:
    """
    规范化主机名用于精确白名单比较

    参数:
    - hostname: URL 中的主机名

    返回:
    - str: 小写且移除结尾根标签的主机名
    """
    return hostname.rstrip(".").lower()


def _resolve_addresses(hostname: str, port: int) -> tuple[tuple[socket.AddressFamily, str], ...]:
    """
    解析目标主机并保留连接所需的地址族

    参数:
    - hostname: 已规范化的目标主机名
    - port: 目标端口

    返回:
    - tuple[tuple[socket.AddressFamily, str], ...]: 去重后的地址族与 IP 列表
    """
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise UnsafeOutboundURLError("目标主机无法解析") from error

    addresses: list[tuple[socket.AddressFamily, str]] = []
    seen: set[tuple[socket.AddressFamily, str]] = set()
    for family, _, _, _, sockaddr in records:
        address = str(sockaddr[0])
        item = (socket.AddressFamily(family), address)
        if item not in seen:
            addresses.append(item)
            seen.add(item)
    if not addresses:
        raise UnsafeOutboundURLError("目标主机没有可用地址")
    return tuple(addresses)


def resolve_outbound_http_url(
    url: str,
    *,
    trusted_hosts: Iterable[str] = (),
) -> ResolvedOutboundTarget:
    """
    校验 HTTP URL 并返回必须用于实际连接的解析地址

    参数:
    - url: 待访问的绝对 URL
    - trusted_hosts: 允许访问私网地址的显式主机名

    返回:
    - ResolvedOutboundTarget: 规范化 URL 与已校验的连接地址
    """
    normalized_url = url.strip()
    if not normalized_url or len(normalized_url) > 8192:
        raise UnsafeOutboundURLError("URL 为空或过长")
    parsed = urlsplit(normalized_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeOutboundURLError("仅允许绝对 HTTP/HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeOutboundURLError("URL 不得包含用户凭据")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as error:
        raise UnsafeOutboundURLError("URL 端口无效") from error

    hostname = normalize_hostname(parsed.hostname)
    trusted = {normalize_hostname(item) for item in trusted_hosts}
    if hostname not in trusted and (hostname == "localhost" or hostname.endswith(".localhost")):
        raise UnsafeOutboundURLError("禁止访问本机地址")

    addresses = _resolve_addresses(hostname, port)
    if hostname not in trusted:
        for _, address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as error:
                raise UnsafeOutboundURLError("目标主机解析结果无效") from error
            if not ip.is_global:
                raise UnsafeOutboundURLError(f"禁止访问非公网地址: {ip.compressed}")
    return ResolvedOutboundTarget(normalized_url, hostname, port, addresses)


def validate_outbound_http_url(
    url: str,
    *,
    trusted_hosts: Iterable[str] = (),
) -> str:
    """
    校验 HTTP URL 的协议, 凭据和解析地址

    参数:
    - url: 待访问的绝对 URL
    - trusted_hosts: 允许访问私网地址的显式主机名

    返回:
    - str: 去除首尾空格后的安全 URL
    """
    return resolve_outbound_http_url(url, trusted_hosts=trusted_hosts).url


def resolve_outbound_redirect(
    current_url: str,
    location: str,
    *,
    trusted_hosts: Iterable[str] = (),
) -> ResolvedOutboundTarget:
    """
    解析并校验一次 HTTP 重定向目标及其连接地址

    参数:
    - current_url: 当前响应 URL
    - location: Location 响应头
    - trusted_hosts: 允许访问私网地址的显式主机名

    返回:
    - ResolvedOutboundTarget: 已校验的重定向目标
    """
    if not location.strip():
        raise UnsafeOutboundURLError("重定向缺少 Location")
    return resolve_outbound_http_url(
        urljoin(current_url, location),
        trusted_hosts=trusted_hosts,
    )


def validate_outbound_redirect(
    current_url: str,
    location: str,
    *,
    trusted_hosts: Iterable[str] = (),
) -> str:
    """
    解析并校验一次 HTTP 重定向目标

    参数:
    - current_url: 当前响应 URL
    - location: Location 响应头
    - trusted_hosts: 允许访问私网地址的显式主机名

    返回:
    - str: 校验后的绝对重定向 URL
    """
    return resolve_outbound_redirect(
        current_url,
        location,
        trusted_hosts=trusted_hosts,
    ).url


class _PinnedResolver(AbstractResolver):
    """仅返回预先校验地址的 aiohttp 解析器"""

    def __init__(self, target: ResolvedOutboundTarget) -> None:
        """
        初始化固定地址解析器

        参数:
        - target: 已校验的出站目标
        """
        self._target = target

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        """
        返回目标在校验阶段取得的固定地址

        参数:
        - host: aiohttp 请求的主机名
        - port: aiohttp 请求的端口
        - family: aiohttp 请求的地址族

        返回:
        - list[ResolveResult]: 不触发二次 DNS 查询的地址记录
        """
        if normalize_hostname(host) != self._target.hostname or port not in {0, self._target.port}:
            raise OSError("请求目标与已校验地址不一致")
        results: list[ResolveResult] = []
        for address_family, address in self._target.addresses:
            if family not in {socket.AF_UNSPEC, address_family}:
                continue
            results.append(
                ResolveResult(
                    hostname=host,
                    host=address,
                    port=self._target.port,
                    family=address_family,
                    proto=socket.IPPROTO_TCP,
                    flags=socket.AI_NUMERICHOST,
                )
            )
        if not results:
            raise OSError("已校验地址中没有匹配的地址族")
        return results

    async def close(self) -> None:
        """关闭固定地址解析器"""


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


def _merge_query_params(url: str, params: Mapping[str, object] | None) -> str:
    """
    将结构化 GET 参数合并到绝对 URL

    参数:
    - url: 原始绝对 URL
    - params: 追加的结构化查询参数

    返回:
    - str: 合并查询参数并移除 fragment 的绝对 URL
    """
    parsed = urlsplit(url)
    query = parsed.query
    if params:
        encoded = urlencode(params, doseq=True)
        query = f"{query}&{encoded}" if query else encoded
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


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
                raise OutboundResponseTooLargeError(f"响应超过 {max_response_bytes} 字节限制")
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
    request_headers = {key: value for key, value in (headers or {}).items() if key.lower() != "host"}
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
            connection = _PinnedHTTPConnection(target.hostname, target.port, address, timeout)
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


async def _read_async_response(
    response: aiohttp.ClientResponse,
    max_response_bytes: int,
) -> bytes:
    """
    读取异步响应并强制执行字节上限

    参数:
    - response: aiohttp HTTP 响应
    - max_response_bytes: 最大允许响应字节数

    返回:
    - bytes: 未超过限制的完整响应正文
    """
    if response.content_length is not None and response.content_length > max_response_bytes:
        raise OutboundResponseTooLargeError(f"响应超过 {max_response_bytes} 字节限制")
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > max_response_bytes:
            raise OutboundResponseTooLargeError(f"响应超过 {max_response_bytes} 字节限制")
        chunks.append(chunk)
    return b"".join(chunks)


async def _async_request_once(
    target: ResolvedOutboundTarget,
    *,
    headers: Mapping[str, str] | None,
    timeout: float,
    max_response_bytes: int,
    ssl_verify: bool,
) -> OutboundHTTPResponse:
    """
    对一个已校验目标执行一次固定地址异步 GET

    参数:
    - target: 已校验并解析的出站目标
    - headers: 请求头
    - timeout: 请求总超时秒数
    - max_response_bytes: 最大允许响应字节数
    - ssl_verify: 是否验证 TLS 证书

    返回:
    - OutboundHTTPResponse: 完整内存响应
    """
    resolver = _PinnedResolver(target)
    connector = aiohttp.TCPConnector(
        resolver=resolver,
        use_dns_cache=False,
        force_close=True,
        ssl=None if ssl_verify else False,
    )
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    request_headers = {key: value for key, value in (headers or {}).items() if key.lower() != "host"}
    async with aiohttp.ClientSession(connector=connector, timeout=client_timeout) as session:
        async with session.get(target.url, headers=request_headers, allow_redirects=False) as response:
            content = await _read_async_response(response, max_response_bytes)
            return OutboundHTTPResponse(
                url=target.url,
                status_code=response.status,
                headers=dict(response.headers),
                content=content,
                encoding=response.charset or "utf-8",
            )


async def safe_async_get(
    url: str,
    *,
    params: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 15,
    allow_redirects: bool = True,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    trusted_hosts: Iterable[str] = (),
    max_redirects: int = 5,
    ssl_verify: bool = True,
    restrict_redirects_to_origin: bool = False,
) -> OutboundHTTPResponse:
    """
    异步执行地址绑定和响应限流的安全 GET 请求

    参数:
    - url: 待访问的绝对 URL
    - params: 追加的结构化查询参数
    - headers: 请求头
    - timeout: 请求总超时秒数, 默认 15
    - allow_redirects: 是否自动跟随重定向, 默认 True
    - max_response_bytes: 最大允许响应字节数
    - trusted_hosts: 允许访问私网地址的显式主机名
    - max_redirects: 最大重定向次数, 默认 5
    - ssl_verify: 是否验证 TLS 证书, 默认 True
    - restrict_redirects_to_origin: 是否限制重定向不得改变来源, 默认 False

    返回:
    - OutboundHTTPResponse: 最终响应或未自动跟随的首个响应
    """
    merged_url = _merge_query_params(url, params)
    initial_url = merged_url
    current = resolve_outbound_http_url(merged_url, trusted_hosts=trusted_hosts)
    for redirect_count in range(max_redirects + 1):
        response = await _async_request_once(
            current,
            headers=headers,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
            ssl_verify=ssl_verify,
        )
        if not allow_redirects or not response.is_redirect:
            return response
        if redirect_count >= max_redirects:
            raise UnsafeOutboundURLError("重定向次数超过限制")
        next_target = resolve_outbound_redirect(
            current.url,
            response.headers.get("Location", ""),
            trusted_hosts=trusted_hosts,
        )
        if restrict_redirects_to_origin and not same_origin(initial_url, next_target.url):
            raise UnsafeOutboundURLError("重定向不得离开原始来源")
        current = next_target
    raise UnsafeOutboundURLError("重定向次数超过限制")


def same_origin(first_url: str, second_url: str) -> bool:
    """
    判断两个 HTTP URL 是否具有相同协议, 主机和有效端口

    参数:
    - first_url: 第一个 URL
    - second_url: 第二个 URL

    返回:
    - bool: 是否属于同一来源
    """
    first = urlsplit(first_url)
    second = urlsplit(second_url)
    try:
        first_port = first.port or (443 if first.scheme.lower() == "https" else 80)
        second_port = second.port or (443 if second.scheme.lower() == "https" else 80)
    except ValueError:
        return False
    return (
        first.scheme.lower() == second.scheme.lower()
        and first.hostname is not None
        and second.hostname is not None
        and normalize_hostname(first.hostname) == normalize_hostname(second.hostname)
        and first_port == second_port
    )
