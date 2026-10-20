"""
网页检索专用的浏览器请求适配器

仅供 search 和 fetch_page 使用, 复用平台的公网地址校验,
将已校验地址绑定到 curl 连接, 并限制重定向, 解压正文和整次执行时间
"""
from __future__ import annotations

from curl_cffi.requests.exceptions import RequestException
from concurrent.futures import Future, ThreadPoolExecutor
from curl_cffi.requests import Response
from collections.abc import Mapping
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from urllib.parse import urljoin, urlsplit, urlunsplit
from curl_cffi import AsyncSession, Session, CurlOpt, CurlECode
import threading
import asyncio
from typing import Self, cast
from types import TracebackType

from satrap.core.utils.outbound.utils import _merge_query_params
from satrap.core.utils.outbound import (
    DEFAULT_MAX_RESPONSE_BYTES, OutboundHTTPResponse, OutboundResponseTooLargeError,
    ResolvedOutboundTarget, resolve_outbound_http_url,
)
from .utils import Deadline, WebPolicy, WebRequestError, WEB_POLICY, retry_delay, site_key


_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="satrap-web-dns")
_DNS_SLOTS = threading.BoundedSemaphore(16)


def _resolve(url: str) -> Future[ResolvedOutboundTarget]:
    """
    提交有界 DNS 校验, 超时调用不会无限堆积后台解析

    参数:
    - url: 待校验的绝对 HTTP/HTTPS URL

    返回:
    - 只负责地址校验的 Future, 不会在后台发出 HTTP 请求
    """
    if not _DNS_SLOTS.acquire(blocking=False):
        raise WebRequestError("busy", "网页 DNS 校验队列已满")
    try:
        future = _DNS_POOL.submit(resolve_outbound_http_url, url)
    except BaseException:
        _DNS_SLOTS.release()
        raise
    future.add_done_callback(lambda _: _DNS_SLOTS.release())
    return future


class _Body:
    """以解压后的实际字节数限制正文, 超限时中止底层读取"""

    def __init__(self, limit: int) -> None:
        """
        初始化单次响应缓冲

        参数:
        - limit: 最大允许字节数
        """
        self.limit, self.exceeded = limit, False
        self.content = bytearray()

    def write(self, chunk: bytes) -> int:
        """
        接收 curl 解压后的正文块

        参数:
        - chunk: 当前正文块

        返回:
        - 写入字节数, 超限时返回 curl 的显式中止标记
        """
        if len(self.content) + len(chunk) > self.limit:
            self.exceeded = True
            return CURL_WRITEFUNC_ERROR
        self.content.extend(chunk)
        return len(chunk)


def _configure(session: Session | AsyncSession, target: ResolvedOutboundTarget) -> str:
    """
    将校验地址绑定到新连接, 禁止隐式代理和旧连接复用

    参数:
    - session: 当前调用独享的 curl 会话
    - target: 公网校验通过的目标及地址列表

    返回:
    - 主机名与绑定规则一致的规范 URL, 避免末尾根标签或 IDN 触发二次解析
    """
    hostname = target.hostname.encode("idna").decode("ascii")
    host = f"[{hostname}]" if ":" in hostname else hostname
    addresses = ",".join(f"[{ip}]" if ":" in ip else ip for _, ip in target.addresses)
    rule = f"{host}:{target.port}:{addresses}".encode("ascii")
    options = cast(dict[CurlOpt, object], session.curl_options)
    # curl 的配置值按选项分别接收字节列表, 字符串和整数, 库的字典标注未覆盖这些形态
    options.clear()
    options.update({CurlOpt.RESOLVE: [rule], CurlOpt.PROXY: "", CurlOpt.FRESH_CONNECT: 1,
                    CurlOpt.FORBID_REUSE: 1})
    parsed = urlsplit(target.url)
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


def _response(response: Response, body: _Body, url: str, policy: WebPolicy) -> OutboundHTTPResponse:
    """
    转换响应并为明确拒绝建立冷却

    参数:
    - response: curl 响应, 正文由回调独立保存
    - body: 受大小限制的正文缓冲
    - url: 已校验的当前地址
    - policy: 当前共享限流策略

    返回:
    - 项目统一响应对象, 403 和 429 抛出包含冷却时间的错误
    """
    headers = {name: value for name, value in response.headers.items() if value is not None}
    for name in ("Content-Type", "Location", "Retry-After"):
        value = response.headers.get(name)
        if value is not None:
            headers[name] = value
    if response.status_code in {403, 429}:
        code = "access_denied" if response.status_code == 403 else "rate_limited"
        delay = 60.0 if response.status_code == 403 else retry_delay(headers.get("Retry-After"))
        policy.cool_down(site_key(url), code, delay)
        raise WebRequestError(code, f"HTTP {response.status_code}", status=response.status_code,
                              retry_after=delay)
    result = OutboundHTTPResponse(url, response.status_code, headers, bytes(body.content))
    result.encoding = result.apparent_encoding or "utf-8"
    return result


def _request_error(error: RequestException, body: _Body) -> Exception:
    """
    将底层异常收敛为稳定类别, 不向模型暴露 curl 的内部地址信息

    参数:
    - error: curl 请求错误
    - body: 用于识别主动超限中止的正文缓冲

    返回:
    - 响应超限, 超时或连接失败异常
    """
    if body.exceeded:
        return OutboundResponseTooLargeError(f"响应超过 {body.limit} 字节限制")
    if error.code == CurlECode.OPERATION_TIMEDOUT:
        return WebRequestError("timeout", "网页请求超时", retryable=True)
    return WebRequestError("connection_error", "网页连接失败", retryable=True)


class BrowserTransport:
    """单次同步工具调用的浏览器会话, 调用结束即丢弃 Cookie"""

    def __init__(self, deadline: Deadline, policy: WebPolicy = WEB_POLICY,
                 *, max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES) -> None:
        """
        创建独立同步会话

        参数:
        - deadline: 包括排队和重定向的工具总时限
        - policy: 默认使用跨会话共享策略
        - max_bytes: 单次解压响应上限, 默认 8 MiB
        """
        self.deadline, self.policy, self.max_bytes = deadline, policy, max_bytes
        self.session = Session(impersonate="chrome", trust_env=False)

    def __enter__(self) -> Self:
        """
        进入单次工具调用的网络会话

        返回:
        - 当前独立网络会话
        """
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        """
        无论请求成功与否都释放连接和 Cookie

        参数:
        - exc_type: 当前异常类型
        - exc: 当前异常实例
        - traceback: 当前异常堆栈
        """
        self.session.close()

    def get(self, url: str, *, params: Mapping[str, object] | None = None) -> OutboundHTTPResponse:
        """
        同步读取页面, 每次重定向重新校验并绑定地址

        参数:
        - url: 待读取的 HTTP/HTTPS 地址
        - params: 结构化查询参数, 默认不追加

        返回:
        - 最终受限响应, 最多跟随五次重定向
        """
        current = _merge_query_params(url, params)
        for hop in range(6):
            key = site_key(current)
            self.policy.acquire(key, self.deadline)
            try:
                target = _resolve(current).result(timeout=self.deadline.remaining())
                request_url = _configure(self.session, target)
                body = _Body(self.max_bytes)
                try:
                    raw = self.session.get(request_url, timeout=self.deadline.remaining(),
                                           allow_redirects=False, content_callback=body.write,
                                           headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
                except RequestException as error:
                    raise _request_error(error, body) from error
                response = _response(raw, body, request_url, self.policy)
            except TimeoutError as error:
                raise WebRequestError("timeout", "网页 DNS 校验超时", retryable=True) from error
            finally:
                self.policy.release(key)
            self.deadline.remaining()
            if not response.is_redirect:
                return response
            if hop == 5:
                raise WebRequestError("redirect_limit", "重定向次数超过限制")
            current = urljoin(target.url, response.headers["Location"])
        raise AssertionError("重定向循环未返回")


class AsyncBrowserTransport:
    """单次异步工具调用的浏览器会话, 取消时释放网络资源"""

    def __init__(self, deadline: Deadline, policy: WebPolicy = WEB_POLICY,
                 *, max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES) -> None:
        """
        创建独立异步会话

        参数:
        - deadline: 包括排队和重定向的工具总时限
        - policy: 默认使用跨会话共享策略
        - max_bytes: 单次解压响应上限, 默认 8 MiB
        """
        self.deadline, self.policy, self.max_bytes = deadline, policy, max_bytes
        self.session = AsyncSession(impersonate="chrome", trust_env=False, max_clients=1)

    async def __aenter__(self) -> Self:
        """
        进入单次工具调用的异步网络会话

        返回:
        - 当前独立异步网络会话
        """
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        traceback: TracebackType | None) -> None:
        """
        无论请求成功与否都关闭异步会话

        参数:
        - exc_type: 当前异常类型
        - exc: 当前异常实例
        - traceback: 当前异常堆栈
        """
        await self.session.close()

    async def get(self, url: str, *, params: Mapping[str, object] | None = None) -> OutboundHTTPResponse:
        """
        异步读取页面, 每次重定向重新校验并绑定地址

        参数:
        - url: 待读取的 HTTP/HTTPS 地址
        - params: 结构化查询参数, 默认不追加

        返回:
        - 最终受限响应, 最多跟随五次重定向; 取消保留 CancelledError
        """
        current = _merge_query_params(url, params)
        for hop in range(6):
            key = site_key(current)
            await self.policy.acquire_async(key, self.deadline)
            try:
                target = await asyncio.wait_for(asyncio.wrap_future(_resolve(current)),
                                                timeout=self.deadline.remaining())
                request_url = _configure(self.session, target)
                body = _Body(self.max_bytes)
                try:
                    raw = await asyncio.wait_for(
                        self.session.get(request_url, timeout=self.deadline.remaining(),
                                         allow_redirects=False, content_callback=body.write,
                                         headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}),
                        timeout=self.deadline.remaining(),
                    )
                except RequestException as error:
                    raise _request_error(error, body) from error
                response = _response(raw, body, request_url, self.policy)
            except TimeoutError as error:
                raise WebRequestError("timeout", "网页请求执行超时", retryable=True) from error
            finally:
                self.policy.release(key)
            self.deadline.remaining()
            if not response.is_redirect:
                return response
            if hop == 5:
                raise WebRequestError("redirect_limit", "重定向次数超过限制")
            current = urljoin(target.url, response.headers["Location"])
        raise AssertionError("重定向循环未返回")
