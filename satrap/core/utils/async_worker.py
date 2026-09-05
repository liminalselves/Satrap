"""有界同步任务执行器, 保留取消期间的资源生命周期"""
from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")


class WorkerBusyError(RuntimeError):
    """工作线程与等待队列已满"""


class BoundedAsyncWorker:
    """将同步调用放入固定线程池, 同时限制已提交任务总数"""

    def __init__(self, name: str, workers: int, capacity: int):
        """
        创建延迟启动的有界线程池

        参数:
        - name: 工作线程名前缀
        - workers: 最大并发线程数
        - capacity: 运行中与排队中的任务总上限
        """
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._slots = threading.BoundedSemaphore(capacity)

    async def run(self, function: Callable[..., _T], *args: Any, wait_on_cancel: bool = True, **kwargs: Any) -> _T:
        """
        在线程中运行同步函数, 槽位在线程真正结束时释放

        参数:
        - function: 需要执行的同步函数
        - args: 函数位置参数
        - wait_on_cancel: 取消时是否等待底层工作结束再释放调用方持有的资源
        - kwargs: 函数关键字参数

        返回:
        - 函数结果; 队列已满时抛出 WorkerBusyError, 取消时保留取消语义
        """
        if not self._slots.acquire(blocking=False):
            raise WorkerBusyError("同步任务队列已满, 请稍后重试")
        try:
            context = contextvars.copy_context()
            submitted = self._executor.submit(context.run, partial(function, *args, **kwargs))
        except BaseException:
            self._slots.release()
            raise
        submitted.add_done_callback(lambda _: self._slots.release())
        future = asyncio.wrap_future(submitted)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if wait_on_cancel:
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue   # 重复取消也不能提前释放仍在线程中使用的会话
                    except Exception:
                        break
                if not future.cancelled():
                    future.exception()
            else:
                future.add_done_callback(lambda result: None if result.cancelled() else result.exception())
            raise

    def close(self) -> None:
        """关闭仅用于独立测试或服务终止, 调用方确保任务已经结束"""
        self._executor.shutdown(wait=True)


SESSION_WORKERS = BoundedAsyncWorker("satrap-session", workers=8, capacity=32)
DNS_WORKERS = BoundedAsyncWorker("satrap-dns", workers=4, capacity=16)
