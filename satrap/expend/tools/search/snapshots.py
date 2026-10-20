"""按工具实例隔离的进程内正文快照, 支持续读且不重新联网"""
from __future__ import annotations

from collections.abc import Callable
from collections import OrderedDict
from dataclasses import dataclass
import threading
import secrets
import time

from .extraction import ExtractedPage, MAX_TEXT_BYTES
from .utils import WebRequestError


@dataclass(frozen=True)
class _Snapshot:
    """记录实例归属和固定有效期, 不含原始响应"""

    owner: object
    request_url: str
    page: ExtractedPage
    expires_at: float
    size: int


class SnapshotStore:
    """以短锁保护全进程 LRU 和容量, 查找必须匹配原实例及原 URL"""

    def __init__(self, *, max_entries: int = 128, max_bytes: int = 32 * 1024 * 1024,
                 ttl: float = 300, clock: Callable[[], float] = time.monotonic) -> None:
        """
        初始化有界内存快照

        参数:
        - max_entries: 全进程条数上限, 默认 128
        - max_bytes: 键与内容序列化字节预算, 默认 32 MiB
        - ttl: 从首次保存起的有效秒数, 默认 300
        - clock: 单调时钟, 测试可提供可控时钟
        """
        self.max_entries, self.max_bytes, self.ttl = max_entries, max_bytes, ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._items: OrderedDict[str, _Snapshot] = OrderedDict()
        self._bytes = 0

    def _prune(self) -> None:
        """在锁内清除过期条目, 不因续读延长有效期"""
        now = self._clock()
        for key, value in list(self._items.items()):
            if value.expires_at <= now:
                self._bytes -= self._items.pop(key).size

    def save(self, owner: object, request_url: str, page: ExtractedPage) -> str:
        """
        保存正文, 淘汰旧条目后返回随机标识

        参数:
        - owner: 当前工具实例的内部身份, 不对外返回
        - request_url: 首次调用的原始 URL
        - page: 提取后的完整正文

        返回:
        - 随机 page_id, 超限不保存部分正文
        """
        token = secrets.token_urlsafe(24)
        fields = (token, request_url, page.url, page.title, page.content, page.extraction, *page.warnings)
        size = sum(len(field.encode("utf-8")) for field in fields) + 256
        if len(page.content.encode("utf-8")) > MAX_TEXT_BYTES or size > self.max_bytes:
            raise WebRequestError("extraction_limit", "正文与元数据超过快照容量", status=200)
        with self._lock:
            self._prune()
            while self._items and (len(self._items) >= self.max_entries or self._bytes + size > self.max_bytes):
                _, removed = self._items.popitem(last=False)
                self._bytes -= removed.size
            self._items[token] = _Snapshot(owner, request_url, page, self._clock() + self.ttl, size)
            self._bytes += size
        return token

    def get(self, owner: object, request_url: str, page_id: str) -> ExtractedPage:
        """
        取得原实例拥有的同一份正文, 不允许跨 URL 或实例续读

        参数:
        - owner: 当前工具实例内部身份
        - request_url: 本次调用的原始 URL
        - page_id: 首次调用返回的随机快照标识

        返回:
        - 不可变正文对象; 失效或不匹配统一返回 page_expired
        """
        with self._lock:
            self._prune()
            item = self._items.get(page_id)
            if item is None or item.owner is not owner or item.request_url != request_url:
                raise WebRequestError("page_expired", "正文快照已失效或不属于本次读取, 请从头重新抓取")
            self._items.move_to_end(page_id)
            return item.page


SNAPSHOTS = SnapshotStore()
