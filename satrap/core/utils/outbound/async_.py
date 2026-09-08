from __future__ import annotations
from collections.abc import Iterable, Mapping
from aiohttp.abc import AbstractResolver, ResolveResult
import aiohttp
import asyncio
import socket
from satrap.core.utils.async_worker import DNS_WORKERS
from .utils import (
    DEFAULT_MAX_RESPONSE_BYTES,
    UnsafeOutboundURLError,
    OutboundResponseTooLargeError,
    ResolvedOutboundTarget,
    OutboundHTTPResponse,
    normalize_hostname,
    resolve_outbound_http_url,
    resolve_outbound_redirect,
    _merge_query_params,
    same_origin,
)


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
        if normalize_hostname(host) != self._target.hostname or port not in {
            0,
            self._target.port,
        }:
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
    if (
        response.content_length is not None
        and response.content_length > max_response_bytes
    ):
        raise OutboundResponseTooLargeError(f"响应超过 {max_response_bytes} 字节限制")
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > max_response_bytes:
            raise OutboundResponseTooLargeError(
                f"响应超过 {max_response_bytes} 字节限制"
            )
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
    request_headers = {
        key: value for key, value in (headers or {}).items() if key.lower() != "host"
    }
    async with aiohttp.ClientSession(
        connector=connector, timeout=client_timeout
    ) as session:
        async with session.get(
            target.url, headers=request_headers, allow_redirects=False
        ) as response:
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
    from . import (
        DNS_WORKERS,
        resolve_outbound_http_url,
        resolve_outbound_redirect,
        _async_request_once,
    )

    merged_url = _merge_query_params(url, params)
    initial_url = merged_url
    deadline = asyncio.get_running_loop().time() + timeout
    trusted_hosts = tuple(trusted_hosts)
    current = await asyncio.wait_for(
        DNS_WORKERS.run(
            resolve_outbound_http_url,
            merged_url,
            trusted_hosts=trusted_hosts,
            wait_on_cancel=False,
        ),
        timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
    )
    for redirect_count in range(max_redirects + 1):
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        response = await asyncio.wait_for(
            _async_request_once(
                current,
                headers=headers,
                timeout=remaining,
                max_response_bytes=max_response_bytes,
                ssl_verify=ssl_verify,
            ),
            timeout=remaining,
        )
        if not allow_redirects or not response.is_redirect:
            return response
        if redirect_count >= max_redirects:
            raise UnsafeOutboundURLError("重定向次数超过限制")
        next_target = await asyncio.wait_for(
            DNS_WORKERS.run(
                resolve_outbound_redirect,
                current.url,
                response.headers.get("Location", ""),
                trusted_hosts=trusted_hosts,
                wait_on_cancel=False,
            ),
            timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
        )
        if restrict_redirects_to_origin and not same_origin(
            initial_url, next_target.url
        ):
            raise UnsafeOutboundURLError("重定向不得离开原始来源")
        current = next_target
    raise UnsafeOutboundURLError("重定向次数超过限制")
