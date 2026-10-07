"""同步工具向来源平台事件循环提交协程的共享桥接契约"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any
import asyncio
import threading

import pytest

from satrap.core.platform.loop_bridge import PlatformLoopUnavailable, run_on_platform_loop


class _LoopThread:
    """在后台线程运行事件循环, 模拟来源平台工作线程"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


@pytest.fixture
def loop_thread() -> Iterator[_LoopThread]:
    thread = _LoopThread()
    yield thread
    thread.close()


def test_running_loop_returns_coroutine_result(loop_thread: _LoopThread) -> None:
    async def answer() -> str:
        return "ok"

    assert run_on_platform_loop(answer(), loop_thread.loop, 5) == "ok"


def test_coroutine_exception_propagates_unchanged(loop_thread: _LoopThread) -> None:
    async def boom() -> None:
        raise ValueError("宿主拒绝")

    with pytest.raises(ValueError, match="宿主拒绝"):
        run_on_platform_loop(boom(), loop_thread.loop, 5)


def test_timeout_cancels_coroutine_in_platform_loop(loop_thread: _LoopThread) -> None:
    cancelled = threading.Event()

    async def slow() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    with pytest.raises(TimeoutError):
        run_on_platform_loop(slow(), loop_thread.loop, 0.05)
    assert cancelled.wait(5)


def test_missing_loop_closes_coroutine() -> None:
    async def never() -> None:
        raise AssertionError("协程不应被提交")

    coroutine = never()
    with pytest.raises(PlatformLoopUnavailable) as info:
        run_on_platform_loop(coroutine, None, 5)
    assert info.value.reason == "missing"
    assert str(info.value) == "来源平台事件循环不可用"
    assert coroutine.cr_frame is None


def test_closed_loop_closes_coroutine() -> None:
    loop = asyncio.new_event_loop()
    loop.close()

    async def never() -> None:
        raise AssertionError("协程不应被提交")

    coroutine = never()
    with pytest.raises(PlatformLoopUnavailable) as info:
        run_on_platform_loop(coroutine, loop, 5)
    assert info.value.reason == "missing"
    assert coroutine.cr_frame is None


def test_not_running_loop_is_rejected() -> None:
    loop = asyncio.new_event_loop()

    async def never() -> None:
        raise AssertionError("协程不应被提交")

    coroutine = never()
    try:
        with pytest.raises(PlatformLoopUnavailable) as info:
            run_on_platform_loop(coroutine, loop, 5)
        assert info.value.reason == "missing"
        assert coroutine.cr_frame is None
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_same_loop_is_rejected_before_blocking() -> None:
    submitted: list[Any] = []

    async def never() -> None:
        submitted.append(object())

    coroutine = never()
    with pytest.raises(PlatformLoopUnavailable) as info:
        run_on_platform_loop(coroutine, asyncio.get_running_loop(), 5)
    assert info.value.reason == "same_loop"
    assert str(info.value) == "同步工具不能在平台事件循环内阻塞调用"
    assert coroutine.cr_frame is None
    await asyncio.sleep(0)
    assert submitted == []
