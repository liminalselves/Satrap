from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class _TokenBucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """Token Bucket 限流器

    按 key (通常为 session_id) 限流, 使用 asyncio.Lock 保证并发安全
    """

    def __init__(self, rate: float = 1.0, burst: int = 5):
        """
        参数:
        - rate: 每秒恢复的 token 数
        - burst: 最大 burst 值 (桶大小)
        """
        self.rate = rate
        self.burst = burst
        self._buckets: dict[str, _TokenBucket] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> tuple[bool, float]:
        """
        检查是否允许请求

        返回:
        - (True, 0.0): 允许请求
        - (False, wait): 被限流, 需等待 wait 秒

        当前调用方仅在日志中使用 wait 值; 如需排队等待可在此处 await
        """
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._buckets[key] = bucket = _TokenBucket(tokens=self.burst, last_refill=now)

            elapsed = now - bucket.last_refill
            bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.rate)
            bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return (True, 0.0)

            wait = (1.0 - bucket.tokens) / self.rate
            return (False, wait)
