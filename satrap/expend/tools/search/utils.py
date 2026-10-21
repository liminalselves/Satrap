"""
网页检索的共享请求策略

同步和异步工具共用站点限流, 失败冷却与有界搜索缓存,
所有状态仅保存在当前进程内, 不保存网页正文或 Cookie
"""
from __future__ import annotations

from urllib.parse import urlsplit
from email.utils import parsedate_to_datetime
from collections import OrderedDict
from dataclasses import dataclass
import threading
from datetime import datetime, timezone
import asyncio
import math
import time

from satrap.core.utils.outbound import OutboundHTTPError


class WebRequestError(OutboundHTTPError):
    """保留可供模型判断的失败类别与重试条件"""

    def __init__(self, code: str, message: str, *, status: int | None = None,
                 retry_after: float | None = None, retryable: bool = False) -> None:
        """
        构造网页请求错误

        参数:
        - code: 稳定错误类别
        - message: 可直接展示的错误说明
        - status: HTTP 状态, 无响应时为 None
        - retry_after: 建议等待秒数, 无冷却时为 None
        - retryable: 是否允许在剩余总时限内尝试备用入口
        """
        super().__init__(message)
        self.code, self.status = code, status
        self.retry_after, self.retryable = retry_after, retryable


class Deadline:
    """让排队, DNS, 请求和重定向共用一个截止时间"""

    def __init__(self, timeout: float) -> None:
        """
        创建工具调用的总时限

        参数:
        - timeout: 正数秒数, 非有限值或非正数被拒绝
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise WebRequestError("invalid_argument", "超时必须是有限正数")
        self.end = time.monotonic() + timeout

    def remaining(self) -> float:
        """
        读取剩余秒数

        返回:
        - 剩余正数秒数, 到期时抛出 timeout 错误
        """
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise WebRequestError("timeout", "网页工具执行超时", retryable=True)
        return remaining


def site_key(url: str) -> str:
    """
    取得限流站点标识, Bing 两个入口共享配额

    参数:
    - url: 绝对 HTTP/HTTPS URL

    返回:
    - 小写主机名, Bing 搜索入口统一为 www.bing.com
    """
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    return "www.bing.com" if host in {"cn.bing.com", "www.bing.com"} else host


def retry_delay(value: str | None) -> float:
    """
    解析 Retry-After, 缺失或格式错误时回退一分钟

    参数:
    - value: 秒数或 HTTP 日期格式的响应头

    返回:
    - 至少一秒的等待时间
    """
    try:
        seconds = float(value or "")
        if math.isfinite(seconds):
            return max(1.0, seconds)
    except ValueError:
        pass   # 非秒数格式继续按 HTTP 日期解析
    try:
        date = parsedate_to_datetime(value or "")
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(1.0, (date - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return 60.0


@dataclass
class _SiteState:
    """一个站点的请求占用和下一次允许执行时间"""

    busy: bool = False
    next_start: float = 0.0
    cooldown: float = 0.0
    cooldown_code: str = "access_denied"


class WebPolicy:
    """进程内跨会话和事件循环共享的网页访问限额"""

    def __init__(self, *, interval: float = 1.0, cache_ttl: float = 60.0) -> None:
        """
        初始化有界策略状态

        参数:
        - interval: 同站点请求启动间隔, 默认一秒
        - cache_ttl: 搜索成功结果缓存时间, 默认一分钟
        """
        self.interval, self.cache_ttl = interval, cache_ttl
        self._lock = threading.Lock()
        self._sites: dict[str, _SiteState] = {}
        self._cache: OrderedDict[tuple[str, int], tuple[float, str, int]] = OrderedDict()
        self._cache_bytes = 0

    def _try_acquire(self, key: str) -> float:
        """
        尝试占用站点, 不在锁内等待

        参数:
        - key: 已归一化站点标识

        返回:
        - 取得配额时为零, 否则为建议等待秒数; 冷却或状态已满时抛出错误
        """
        with self._lock:
            now = time.monotonic()
            self._sites = {name: state for name, state in self._sites.items()
                           if state.busy or max(state.next_start, state.cooldown) > now}
            if key not in self._sites and len(self._sites) >= 256:
                raise WebRequestError("busy", "网页访问队列已满, 请稍后重试")
            state = self._sites.setdefault(key, _SiteState())
            if state.cooldown > now:
                raise WebRequestError(state.cooldown_code, "该站点暂处于访问冷却期",
                                      retry_after=math.ceil(state.cooldown - now))
            if state.busy:
                return 0.05
            if state.next_start > now:
                return min(0.05, state.next_start - now)
            state.busy, state.next_start = True, now + self.interval
            return 0.0

    def acquire(self, key: str, deadline: Deadline) -> None:
        """
        同步等待站点配额

        参数:
        - key: 已归一化站点标识
        - deadline: 当前工具调用的总时限
        """
        while True:
            remaining = deadline.remaining()
            delay = self._try_acquire(key)
            if not delay:
                return
            time.sleep(min(delay, remaining))

    async def acquire_async(self, key: str, deadline: Deadline) -> None:
        """
        异步等待站点配额, 不阻塞事件循环

        参数:
        - key: 已归一化站点标识
        - deadline: 当前工具调用的总时限
        """
        while True:
            remaining = deadline.remaining()
            delay = self._try_acquire(key)
            if not delay:
                return
            await asyncio.sleep(min(delay, remaining))

    def release(self, key: str) -> None:
        """
        释放站点配额, 从请求结束计入间隔, 避免 DNS 或冷启动耗时挤掉间隔

        参数:
        - key: 先前成功取得配额的站点标识
        """
        with self._lock:
            state = self._sites[key]
            state.busy = False
            state.next_start = max(state.next_start, time.monotonic() + self.interval)

    def cool_down(self, key: str, code: str, delay: float = 60.0) -> None:
        """
        记录站点明确拒绝或验证要求

        参数:
        - key: 已归一化站点标识
        - code: 冷却期间返回的错误类别
        - delay: 等待秒数, 默认一分钟
        """
        with self._lock:
            if key not in self._sites and len(self._sites) >= 256:
                raise WebRequestError("busy", "网页访问状态已满, 请稍后重试")
            state = self._sites.setdefault(key, _SiteState())
            state.cooldown = max(state.cooldown, time.monotonic() + delay)
            state.cooldown_code = code

    def cached(self, query: str, count: int) -> str | None:
        """
        读取未过期的搜索结果

        参数:
        - query: 规范化搜索词
        - count: 结果数量

        返回:
        - 已序列化的成功结果, 未命中或已过期时为 None
        """
        with self._lock:
            key = (query, count)
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires, result, size = entry
            if expires <= time.monotonic():
                del self._cache[key]
                self._cache_bytes -= size
                return None
            self._cache.move_to_end(key)
            return result

    def remember(self, query: str, count: int, result: str) -> None:
        """
        写入成功结果, 总容量不超过 128 条和 8 MiB

        参数:
        - query: 规范化搜索词
        - count: 结果数量
        - result: 成功结果 JSON, 不含网页原文或 Cookie
        """
        size = len(result.encode("utf-8")) + len(query.encode("utf-8"))
        if size > 8 * 1024 * 1024:
            return
        with self._lock:
            previous = self._cache.pop((query, count), None)
            if previous is not None:
                self._cache_bytes -= previous[2]
            self._cache[(query, count)] = (time.monotonic() + self.cache_ttl, result, size)
            self._cache_bytes += size
            while len(self._cache) > 128 or self._cache_bytes > 8 * 1024 * 1024:
                _, removed = self._cache.popitem(last=False)
                self._cache_bytes -= removed[2]


WEB_POLICY = WebPolicy()
