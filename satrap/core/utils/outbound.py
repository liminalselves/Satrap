"""
出站 HTTP 地址安全校验

在网络请求发起和重定向前解析目标主机, 默认仅允许全局可路由地址,
并为显式配置的可信服务主机提供受限私网访问例外
"""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urljoin, urlsplit


class UnsafeOutboundURLError(ValueError):
    """出站 URL 违反网络访问边界"""


def normalize_hostname(hostname: str) -> str:
    """
    规范化主机名用于精确白名单比较

    参数:
    - hostname: URL 中的主机名

    返回:
    - str: 小写且移除结尾根标签的主机名
    """
    return hostname.rstrip(".").lower()


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
    if hostname in trusted:
        return normalized_url
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeOutboundURLError("禁止访问本机地址")

    try:
        addresses = {
            str(sockaddr[0])
            for _, _, _, _, sockaddr in socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as error:
        raise UnsafeOutboundURLError("目标主机无法解析") from error
    if not addresses:
        raise UnsafeOutboundURLError("目标主机没有可用地址")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as error:
            raise UnsafeOutboundURLError("目标主机解析结果无效") from error
        if not ip.is_global:
            raise UnsafeOutboundURLError(f"禁止访问非公网地址: {ip.compressed}")
    return normalized_url


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
    if not location.strip():
        raise UnsafeOutboundURLError("重定向缺少 Location")
    return validate_outbound_http_url(
        urljoin(current_url, location),
        trusted_hosts=trusted_hosts,
    )


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
