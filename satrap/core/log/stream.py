"""
标准日志实时流与有界历史缓冲

为各服务提供按运行流和序号读取的增量快照, 同时保留 WebSocket 订阅,
标准输出捕获与日志级别解析共用发布入口
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Lock
import asyncio
from typing import TextIO, TypedDict, cast
import sys
import re
import os
import time
import secrets


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_KNOWN_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")
_LEVEL_PATTERN = "|".join(_KNOWN_LEVELS)
_LOG_HEADER = re.compile(
    r"^(?:(?:\[\d{4}-\d{2}-\d{2}[ \t]+\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\][ \t]+)?"
    rf"(?:\[(?P<bracketed>{_LEVEL_PATTERN})\][ \t]*:"
    rf"|(?P<plain>{_LEVEL_PATTERN})[ \t]+in[ \t]+[^:\r\n]+:)"
    r"|\[\d{4}-\d{2}-\d{2}[ \t]+\d{2}:\d{2}:\d{2}[ \t]+[+-]\d{4}\][ \t]+"
    rf"\[\d+\][ \t]+\[(?P<server>{_LEVEL_PATTERN})\](?:[ \t]+|$))"
)


def _parse_log_level(content: str, default_level: str) -> str:
    """
    从行首日志头解析级别, 无法识别时使用标准流默认级别

    参数:
    - content: 已去除 ANSI 颜色的日志文本
    - default_level: 未识别日志头时的回退级别

    返回:
    - str: 日志头级别或回退级别
    """
    match = _LOG_HEADER.match(content)
    if match is None:
        return default_level
    return match.group("bracketed") or match.group("plain") or match.group("server")


@dataclass(frozen=True, slots=True)
class StandardLogEntry:
    """一条标准日志输出"""

    content: str
    level: str
    sequence: int = 0
    timestamp: float = 0.0
    pid: int = 0
    truncated: bool = False


class LogEntryPayload(TypedDict):
    """增量日志公开记录"""
    content: str
    level: str
    sequence: int
    timestamp: float
    pid: int
    truncated: bool


class LogSnapshot(TypedDict):
    """按运行流与序号读取的有限日志快照"""
    service: str
    stream_id: str
    entries: list[LogEntryPayload]
    next_cursor: int
    has_more: bool
    reset: bool


@dataclass(frozen=True, slots=True)
class StandardLogSubscription:
    """标准日志订阅"""

    subscription_id: int
    queue: asyncio.Queue[StandardLogEntry]
    history: tuple[StandardLogEntry, ...]


@dataclass(frozen=True, slots=True)
class _Subscriber:
    """标准日志订阅者的内部状态"""

    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[StandardLogEntry]


class StandardLogStream:
    """保存标准日志历史并向 WebSocket 订阅者实时广播"""

    def __init__(self, history_size: int = 5000) -> None:
        """
        参数:
        - history_size: 内存中保留的最大日志条数
        """
        self._history: deque[StandardLogEntry] = deque(maxlen=max(1, history_size))
        self._subscribers: dict[int, _Subscriber] = {}
        self._next_subscription_id = 1
        self._lock = Lock()
        self._sequence = 0
        self.stream_id = secrets.token_hex(16)
        self.service = "process"

    def publish(self, content: str, level: str) -> None:
        """
        发布一条标准日志

        参数:
        - content: 已格式化的标准日志文本
        - level: 日志级别
        """
        encoded = content.encode("utf-8", errors="replace")
        truncated = len(encoded) > 16 * 1024
        if truncated:
            content = encoded[:16 * 1024].decode("utf-8", errors="ignore")
        with self._lock:
            self._sequence += 1
            entry = StandardLogEntry(content, level, self._sequence, time.time(), os.getpid(), truncated)
            self._history.append(entry)
            subscribers = tuple(self._subscribers.values())

        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(
                    self._enqueue,
                    subscriber.queue,
                    entry,
                )
            except RuntimeError:
                continue

    def snapshot(self, *, after: int | None = None, stream_id: str | None = None, limit: int = 100) -> LogSnapshot:
        """
        获取有限历史或指定游标后的日志, 不读取磁盘

        参数:
        - after: 上次已读取的序号, 默认读取最近记录
        - stream_id: 上次服务运行流标识, 变化时返回 reset
        - limit: 最大记录数, 必须为 1 至 500

        返回:
        - 携带游标, 重置标记和截断说明的快照
        """
        if isinstance(limit, bool) or not 1 <= limit <= 500 or after is not None and (isinstance(after, bool) or after < 0):
            raise ValueError("日志 limit 必须为 1 至 500, after 必须为非负整数")
        if after is not None and not stream_id:
            raise ValueError("增量读取必须提供 stream_id")
        with self._lock:
            history = tuple(self._history)
            latest = self._sequence
            identity = self.stream_id
            service = self.service
        oldest = history[0].sequence if history else latest + 1
        reset = stream_id is not None and stream_id != identity or after is not None and (after < oldest - 1 or after > latest)
        initial = after is None or reset
        cursor_after = after if after is not None else 0
        candidates = history[-limit:] if initial else tuple(entry for entry in history if entry.sequence > cursor_after)
        entries: list[LogEntryPayload] = []
        budget = 256 * 1024
        for entry in candidates[:limit]:
            data = entry.content.encode("utf-8")
            if len(data) > budget and entries:
                break
            text = data[:budget].decode("utf-8", errors="ignore")
            entries.append(LogEntryPayload(content=text, level=entry.level, sequence=entry.sequence,
                                           timestamp=entry.timestamp, pid=entry.pid, truncated=entry.truncated or len(data) > budget))
            budget -= len(text.encode("utf-8"))
        cursor = entries[-1]["sequence"] if entries else latest if initial else after
        assert cursor is not None
        return LogSnapshot(service=service, stream_id=identity, entries=entries, next_cursor=cursor,
                           has_more=bool(history and cursor < latest), reset=bool(reset))

    def subscribe(
        self,
        history_limit: int = 100,
        queue_size: int = 1000,
    ) -> StandardLogSubscription:
        """
        创建订阅并原子获取连接前的历史日志

        参数:
        - history_limit: 返回的历史日志条数
        - queue_size: 实时消息队列的最大长度

        返回:
        - StandardLogSubscription: 订阅标识, 实时队列和历史日志
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[StandardLogEntry] = asyncio.Queue(maxsize=max(1, queue_size))
        with self._lock:
            subscription_id = self._next_subscription_id
            self._next_subscription_id += 1
            self._subscribers[subscription_id] = _Subscriber(loop=loop, queue=queue)
            history = (
                tuple(self._history)[-history_limit:]
                if history_limit > 0
                else ()
            )
        return StandardLogSubscription(
            subscription_id=subscription_id,
            queue=queue,
            history=history,
        )

    def unsubscribe(self, subscription_id: int) -> None:
        """
        取消订阅

        参数:
        - subscription_id: 订阅标识
        """
        with self._lock:
            self._subscribers.pop(subscription_id, None)

    def clear(self) -> None:
        """清空历史日志, 主要用于测试隔离"""
        with self._lock:
            self._history.clear()

    @staticmethod
    def _enqueue(
        queue: asyncio.Queue[StandardLogEntry],
        entry: StandardLogEntry,
    ) -> None:
        """
        将日志加入有界队列, 队列满时丢弃最旧记录

        参数:
        - queue: 订阅者消息队列
        - entry: 待加入的日志
        """
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(entry)


standard_log_stream = StandardLogStream()


class StandardStreamCapture:
    """保留原始标准流输出并把完整文本行同步到实时日志流"""

    def __init__(
        self,
        wrapped: TextIO,
        stream: StandardLogStream,
        default_level: str,
    ) -> None:
        """
        参数:
        - wrapped: 原始标准文本流
        - stream: 标准日志实时流
        - default_level: 文本中没有级别标记时使用的日志级别
        """
        self._wrapped = wrapped
        self._stream = stream
        self._default_level = default_level
        self._buffer = ""
        self._write_lock = Lock()

    @property
    def encoding(self) -> str:
        """返回原始流编码"""
        return self._wrapped.encoding or "utf-8"

    @property
    def errors(self) -> str | None:
        """返回原始流编码错误处理方式"""
        return self._wrapped.errors

    def writable(self) -> bool:
        """返回当前流是否可写"""
        return self._wrapped.writable()

    def isatty(self) -> bool:
        """返回原始流是否连接到终端"""
        return self._wrapped.isatty()

    def fileno(self) -> int:
        """返回原始流文件描述符"""
        return self._wrapped.fileno()

    def write(self, text: str) -> int:
        """
        写入原始流并发布其中的完整文本行

        参数:
        - text: 待写入文本

        返回:
        - int: 原始流报告的写入字符数
        """
        written = self._wrapped.write(text)
        with self._write_lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self._publish_line(line.rstrip("\r"))
        return written

    def flush(self) -> None:
        """刷新原始流并发布尚未换行的剩余文本"""
        self._wrapped.flush()
        with self._write_lock:
            if self._buffer:
                self._publish_line(self._buffer.rstrip("\r"))
                self._buffer = ""

    def close(self) -> None:
        """刷新代理流但不关闭进程持有的原始标准流"""
        self.flush()

    def _publish_line(self, line: str) -> None:
        """
        发布一行已清理的标准流文本

        参数:
        - line: 标准流文本行
        """
        content = _ANSI_ESCAPE.sub("", line)
        if not content:
            return
        level = _parse_log_level(content, self._default_level)
        self._stream.publish(content, level)


_capture_lock = Lock()


def install_standard_stream_capture() -> None:
    """为当前进程安装可实时订阅的 stdout 和 stderr 代理"""
    with _capture_lock:
        if not isinstance(sys.stdout, StandardStreamCapture):
            sys.stdout = cast(
                TextIO,
                StandardStreamCapture(sys.stdout, standard_log_stream, "INFO"),
            )
        if not isinstance(sys.stderr, StandardStreamCapture):
            sys.stderr = cast(
                TextIO,
                StandardStreamCapture(sys.stderr, standard_log_stream, "ERROR"),
            )
