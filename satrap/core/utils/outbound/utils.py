"""
出站 HTTP 地址校验与安全请求

在请求和重定向前解析目标主机, 默认仅允许全局可路由地址,
并将已校验的地址绑定到实际连接以阻断 DNS 重绑定窗口
"""

from __future__ import annotations
from collections.abc import Iterable, Mapping
from email.message import Message
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from dataclasses import dataclass
import http.client
import ipaddress
import socket
from http import HTTPStatus
import os


DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

"""普通出站响应的默认最大字节数"""

DEFAULT_MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024

"""媒体与文件下载的默认最大字节数"""

TRUSTED_DOWNLOAD_HOSTS_ENV_NAME = "SATRAP_TRUSTED_DOWNLOAD_HOSTS"

"""允许文件下载访问私网地址的可信主机环境变量 (逗号分隔)"""

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
        return self.status_code in _REDIRECT_STATUSES and bool(
            self.headers.get("Location")
        )

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


def trusted_hosts_from_env(
    env_name: str = TRUSTED_DOWNLOAD_HOSTS_ENV_NAME,
) -> tuple[str, ...]:
    """
    从环境变量读取逗号分隔的下载可信主机列表

    参数:
    - env_name: 环境变量名, 默认 SATRAP_TRUSTED_DOWNLOAD_HOSTS

    返回:
    - tuple[str, ...]: 规范化后的可信主机名, 未配置时为空元组
    """
    configured = os.getenv(env_name, "")
    return tuple(
        normalize_hostname(item.strip())
        for item in configured.split(",")
        if item.strip()
    )


def _resolve_addresses(
    hostname: str, port: int
) -> tuple[tuple[socket.AddressFamily, str], ...]:
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
    if hostname not in trusted and (
        hostname == "localhost" or hostname.endswith(".localhost")
    ):
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
