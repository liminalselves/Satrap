"""可选的调用观察入口, 库使用者无需导入任何后端采集模块"""

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol


class CallObservers(Protocol):
    """宿主可注入的同步与异步观察包装"""

    def complete_sync(self, create: Callable[..., object], request: Mapping[str, object], timeout: float | None,
                      *, provider: str, model_config: str, context_window: int | None) -> object:
        """
        观察同步调用

        参数:
        - create: SDK 调用入口
        - request: 已完成转换的参数
        - timeout: 网络超时
        - provider: 客户端地址
        - model_config: 逻辑配置名
        - context_window: 模型窗口

        返回:
        - 原始响应或观察流
        """
        ...

    async def complete_async(self, create: Callable[..., Awaitable[object]], request: Mapping[str, object], timeout: float | None,
                             *, provider: str, model_config: str, context_window: int | None) -> object:
        """
        观察异步调用

        参数:
        - create: SDK 异步入口
        - request: 已转换参数
        - timeout: 网络超时
        - provider: 客户端地址
        - model_config: 逻辑配置名
        - context_window: 模型窗口

        返回:
        - 原始响应或观察流
        """
        ...


_OBSERVERS: ContextVar[CallObservers | None] = ContextVar("llm_call_observers", default=None)
_PURPOSE: ContextVar[str | None] = ContextVar("llm_call_purpose", default=None)


@contextmanager
def bind_observers(observers: CallObservers) -> Iterator[None]:
    """
    在显式调用范围内接入观察器, 退出后恢复原设置

    参数:
    - observers: 宿主提供的包装, 不通过本模块创建服务或存储

    返回:
    - 当前调用范围
    """
    token = _OBSERVERS.set(observers)
    try:
        yield
    finally:
        _OBSERVERS.reset(token)


def current_observers() -> CallObservers | None:
    """
    查询当前显式注入的观察器

    返回:
    - 无宿主时为 None, 不尝试发现或启动后端
    """
    return _OBSERVERS.get()


@contextmanager
def bind_call_purpose(purpose: str) -> Iterator[None]:
    """
    标记摘要等库内部请求, 不依赖任何计量实现

    参数:
    - purpose: 固定的调用用途

    返回:
    - 当前请求范围, 退出后恢复
    """
    token = _PURPOSE.set(purpose)
    try:
        yield
    finally:
        _PURPOSE.reset(token)


def current_call_purpose() -> str | None:
    """
    读取调用方显式设置的用途

    返回:
    - 未覆盖时为 None, 由宿主保留原范围
    """
    return _PURPOSE.get()
