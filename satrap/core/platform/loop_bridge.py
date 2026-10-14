"""同步工具线程向来源平台事件循环提交协程的共享桥接, 超时由调用方决定"""
from __future__ import annotations

from collections.abc import Coroutine
from typing import Any, TypeVar
import asyncio

T = TypeVar("T")


class PlatformLoopUnavailable(RuntimeError):
    """平台循环缺失, 未运行, 或调用方正处于该循环线程内, 协程已关闭且从未提交"""

    def __init__(self, reason: str, message: str) -> None:
        """
        记录拒绝原因

        参数:
        - reason: missing 表示循环缺失或未运行, same_loop 表示在平台循环线程内阻塞调用
        - message: 面向日志与工具结果的说明
        """
        super().__init__(message)
        self.reason = reason


def run_on_platform_loop(coroutine: Coroutine[Any, Any, T], loop: asyncio.AbstractEventLoop | None, timeout: float) -> T:
    """
    把协程提交到平台循环并阻塞等待结果, 只能从非平台循环线程调用

    参数:
    - coroutine: 需要在平台循环执行的协程, 拒绝提交时会被关闭
    - loop: 来源平台事件循环
    - timeout: 等待秒数

    返回:
    - 协程结果; 拒绝提交抛出 PlatformLoopUnavailable, 超时先取消协程再抛出 TimeoutError, 协程自身异常原样抛出
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is None or loop.is_closed() or not loop.is_running():
        coroutine.close()
        raise PlatformLoopUnavailable("missing", "来源平台事件循环不可用")
    if running is loop:
        # 在平台循环线程内等待提交到同一循环的协程只会阻塞到超时, 必须在提交前拒绝
        coroutine.close()
        raise PlatformLoopUnavailable("same_loop", "同步工具不能在平台事件循环内阻塞调用")
    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        # 超时后取消协程, 避免写动作在平台循环里继续生效却被报为失败
        future.cancel()
        raise
