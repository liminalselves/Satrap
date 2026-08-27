"""标准日志实时流与有界历史缓冲"""
from __future__ import annotations

import asyncio
import re
import sys
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import TextIO, cast


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_KNOWN_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


@dataclass(frozen=True, slots=True)
class StandardLogEntry:
    """一条标准日志输出"""

    content: str
    level: str


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

    def publish(self, content: str, level: str) -> None:
        """
        发布一条标准日志

        参数:
        - content: 已格式化的标准日志文本
        - level: 日志级别
        """
        entry = StandardLogEntry(content=content, level=level)
        with self._lock:
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
        level = next(
            (item for item in _KNOWN_LEVELS if f"[{item}]" in content),
            self._default_level,
        )
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
