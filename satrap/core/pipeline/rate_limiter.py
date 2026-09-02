"""后端请求管线使用的令牌桶限流器"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class _TokenBucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """
    Token Bucket 限流器

    按 key (通常为 session_id) 限流, 使用 asyncio.Lock 保证并发安全
    """

    def __init__(
        self,
        rate: float = 1.0,
        burst: int = 5,
        max_buckets: int = 10_000,
        idle_ttl: float = 3600.0,
    ):
        """
        参数:
        - rate: 每秒恢复的 token 数
        - burst: 最大 burst 值 (桶大小)
        - max_buckets: 最多保留的独立 key 数
        - idle_ttl: 空闲桶淘汰时间, 单位秒
        """
        self.rate = float(rate)
        self.burst = max(0, int(burst))
        self.max_buckets = max(1, int(max_buckets))
        self.idle_ttl = max(0.0, float(idle_ttl))
        self._buckets: dict[str, _TokenBucket] = {}
        self._lock = asyncio.Lock()

    def _make_bucket_room(self, now: float) -> None:
        """
        淘汰空闲桶并在容量已满时移除最旧桶

        参数:
        - now: 当前单调时间
        """
        if len(self._buckets) < self.max_buckets:
            return
        stale_keys = [
            key for key, bucket in self._buckets.items()
            if now - bucket.last_refill >= self.idle_ttl
        ]
        for key in stale_keys:
            self._buckets.pop(key, None)
        if len(self._buckets) >= self.max_buckets:
            oldest_key = min(self._buckets, key=lambda key: self._buckets[key].last_refill)
            self._buckets.pop(oldest_key, None)

    async def check(self, key: str) -> tuple[bool, float]:
        """
        检查是否允许请求

        参数:
        - key: 密钥

        返回:
        - (True, 0.0): 允许请求
        - (False, wait): 被限流, 需等待 wait 秒

        当前调用方仅在日志中使用 wait 值; 如需排队等待可在此处 await
        """
        if self.rate <= 0:
            return (True, 0.0)

        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._make_bucket_room(now)
                self._buckets[key] = bucket = _TokenBucket(tokens=self.burst, last_refill=now)

            elapsed = now - bucket.last_refill
            bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.rate)
            bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return (True, 0.0)

            wait = (1.0 - bucket.tokens) / self.rate
            return (False, wait)
