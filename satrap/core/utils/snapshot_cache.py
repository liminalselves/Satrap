"""
进程内只读资源快照缓存

按资源指纹合并同步和异步加载, 只在登记及发布时持锁,
异步等待者不占用工作线程, 取消一个等待者不取消共享加载
"""
from __future__ import annotations

from concurrent.futures import Future
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar
import threading
import traceback
import asyncio

from satrap.core.utils.async_worker import BoundedAsyncWorker

from satrap.core.log import logger

_T = TypeVar('_T')


class SnapshotChangedError(RuntimeError):
    """资源在加载过程中变化, 本轮结果不得发布"""


class SnapshotCache(Generic[_T]):
    """保存私有快照, 消费者负责复制可变结果"""

    def __init__(self, capacity: int = 64) -> None:
        """
        初始化有界缓存

        参数:
        - capacity: 最多保留的资源版本数
        """
        self._capacity = capacity
        self._lock = threading.Lock()
        self._values: OrderedDict[tuple[Hashable, Hashable, int], _T] = OrderedDict()
        self._pending: dict[tuple[Hashable, Hashable, int], Future[_T]] = {}
        self._epoch = 0

    def invalidate(self) -> None:
        """清除完成的快照并阻止旧在途加载发布"""
        with self._lock:
            self._epoch += 1
            self._values.clear()

    def _begin(self, resource: Hashable, fingerprint: Hashable) -> tuple[tuple[Hashable, Hashable, int], Future[_T], bool]:
        """
        登记本代加载或取得现有结果

        参数:
        - resource: 资源标识
        - fingerprint: 来源指纹

        返回:
        - 缓存键, 共享结果及是否由本调用负责加载
        """
        with self._lock:
            key = (resource, fingerprint, self._epoch)
            if key in self._values:
                self._values.move_to_end(key)
                ready: Future[_T] = Future()
                ready.set_result(self._values[key])
                return key, ready, False
            pending = self._pending.get(key)
            if pending is not None:
                return key, pending, False
            pending = Future()
            self._pending[key] = pending
            return key, pending, True

    def _finish(self, key: tuple[Hashable, Hashable, int], pending: Future[_T], fingerprint: Callable[[], Hashable], loader: Callable[[], _T]) -> None:
        """
        加载并核验来源后发布, 异常传给全部等待者

        参数:
        - key: 本代缓存键
        - pending: 共享结果
        - fingerprint: 重新读取来源指纹
        - loader: 同步快照构造器
        """
        try:
            value = loader()
            current = fingerprint()
            with self._lock:
                if current != key[1] or self._epoch != key[2]:
                    raise SnapshotChangedError('资源在读取期间变化, 请重试')
                self._values[key] = value
                self._values.move_to_end(key)
                while len(self._values) > self._capacity:
                    self._values.popitem(last=False)
            pending.set_result(value)
        except Exception as error:
            if not isinstance(error, SnapshotChangedError):
                logger.error(f'[资源快照] 加载失败, 类型={type(error).__name__}\n{traceback.format_exc()}')
            pending.set_exception(error)
        finally:
            with self._lock:
                self._pending.pop(key, None)

    def get(self, resource: Hashable, fingerprint: Callable[[], Hashable], loader: Callable[[], _T]) -> _T:
        """
        同步取得已核验快照, 变化期间最多重试三次

        参数:
        - resource: 资源标识
        - fingerprint: 来源指纹读取器
        - loader: 同步快照构造器

        返回:
        - 私有快照; 持续变化或加载失败时抛出异常
        """
        for _ in range(3):
            key, pending, leader = self._begin(resource, fingerprint())
            if leader:
                self._finish(key, pending, fingerprint, loader)
            try:
                return pending.result()
            except SnapshotChangedError:
                continue
        logger.warning('[资源快照] 来源持续变化, 已停止重试')
        raise SnapshotChangedError('资源持续变化, 请稍后重试')

    async def get_async(self, resource: Hashable, fingerprint: Callable[[], Hashable], loader: Callable[[], _T], worker: BoundedAsyncWorker) -> _T:
        """
        异步取得快照, 只有领头调用提交有界工作任务

        参数:
        - resource: 资源标识
        - fingerprint: 轻量来源指纹读取器
        - loader: 同步快照构造器
        - worker: 执行领头加载的有界线程池

        返回:
        - 私有快照; 队列满, 持续变化或加载失败时抛出异常
        """
        for _ in range(3):
            key, pending, leader = self._begin(resource, fingerprint())
            if leader:
                async def load() -> None:
                    try:
                        await worker.run(self._finish, key, pending, fingerprint, loader)
                    except Exception as error:
                        logger.error(f'[资源快照] 工作任务提交失败, 类型={type(error).__name__}\n{traceback.format_exc()}')
                        pending.set_exception(error)
                        with self._lock:
                            self._pending.pop(key, None)
                task = asyncio.create_task(load())
                def completed(done: asyncio.Task[None], shared: Future[_T] = pending, cache_key: tuple[Hashable, Hashable, int] = key) -> None:
                    if done.cancelled() and not shared.done():
                        logger.warning('[资源快照] 服务退出时加载被取消')
                        shared.set_exception(RuntimeError('快照加载已取消'))
                        with self._lock:
                            self._pending.pop(cache_key, None)
                    elif not done.cancelled():
                        done.exception()
                task.add_done_callback(completed)
            waiting = asyncio.wrap_future(pending)
            waiting.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            try:
                return await asyncio.shield(waiting)
            except SnapshotChangedError:
                continue
        logger.warning('[资源快照] 来源持续变化, 已停止重试')
        raise SnapshotChangedError('资源持续变化, 请稍后重试')
