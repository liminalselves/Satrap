"""
共用 SDK 观察边界

同时分发轻量计量与可选 Debug 观察, 不复制或提前消费响应流, 在取消时结算调用
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from urllib.parse import urlsplit
from dataclasses import replace
import inspect
from typing import cast
import time
from uuid import uuid4

from satrap.core.APICall.LLMCall.responses import _extract_token_usage
from satrap.core.log.debug.capture import current_trace
from satrap.core.log.managed import report_failure
from satrap.core.log.debug.model import ModelObservation
from satrap.core.type import TokenUsage
from .contracts import ModelCall
from .capture import current_scope, current_writer


class _Observation:
    """单次真实调用的独立状态, 不放在共享 LLM 实例中"""

    def __init__(self, request: Mapping[str, object], timeout: float | None, provider: str, model_config: str, context_window: int | None) -> None:
        """
        冻结计量身份, 只在高级采集开启时创建 Debug 观察者

        参数:
        - request: 最终 SDK 参数, 不保存于轻量计量
        - timeout: 原有 SDK 网络超时
        - provider: 客户端配置的 API 基址, 只提取主机名
        - model_config: 宿主传入的逻辑模型配置名
        - context_window: 当前配置声明的总窗口, 缺少时未知
        """
        self.debug: ModelObservation | None = None
        if current_trace() is not None:
            try:
                self.debug = ModelObservation(request, timeout)
            except Exception as error:
                report_failure(f"Debug 模型观察初始化失败: {type(error).__name__}")
        self.writer = current_writer()
        self.scope = replace(current_scope(), model_config=model_config or current_scope().model_config)
        self.call_id = uuid4().hex
        self.model = str(request.get("model", ""))[:512]
        self.provider = urlsplit(provider).hostname or ""   # 不保存 URL 中的凭据, 查询串或路径
        self.started_at, self.started = time.time(), time.monotonic()
        self.usage: TokenUsage | None = None
        self.finished = False
        self.context_window = context_window

    def chunk(self, value: object) -> None:
        """
        观察一个原始块, 不保留响应正文或改变消费顺序

        参数:
        - value: SDK 实际流式块
        """
        if self.debug is not None:
            try:
                self.debug.chunk(value)
            except Exception as error:
                self.debug = None
                report_failure(f"Debug 模型流观察失败: {type(error).__name__}")
        if self.writer is not None:
            try:
                usage = _extract_token_usage(value)
                if usage is not None:
                    self.usage = usage
            except Exception:
                self.writer._gap("usage_observation_failed")

    def finish(self, response: object = None, error: BaseException | None = None) -> None:
        """
        幂等记录真实终态, 缺少 usage 保持未知

        参数:
        - response: 非流式返回, 流式保持 None
        - error: 原始调用或消费异常, 轻量统计只保存结果分类
        """
        if self.finished:
            return
        self.finished = True
        if self.debug is not None:
            try:
                self.debug.finish(response, error)
            except Exception as failure:
                report_failure(f"Debug 模型终态观察失败: {type(failure).__name__}")
        if self.writer is None:
            return
        try:
            usage = _extract_token_usage(response) if response is not None else self.usage
            cancelled = error is not None and type(error).__name__ in {"CancelledError", "GeneratorExit"}
            status = "cancelled" if cancelled else "failed" if error is not None else "succeeded"
            self.writer.submit(ModelCall(self.call_id, self.scope, self.model, self.provider, self.started_at, time.time(),
                                         (time.monotonic() - self.started) * 1000, status,
                                         usage.input_tokens if usage else None, usage.output_tokens if usage else None,
                                         usage.cached_tokens if usage else None, usage.total_tokens if usage else None, self.context_window))
        except Exception:
            self.writer._gap("usage_observation_failed", count=1)


class _SyncStream(Iterator[object]):
    """显式持有 SDK 流, 即使未开始迭代也能关闭和结算"""

    def __init__(self, response: object, observation: _Observation) -> None:
        """
        保存唯一流和观察者, 不提前请求下一个块

        参数:
        - response: 原始 SDK 流
        - observation: 本次调用的观察者
        """
        self.response = response
        self.iterator = iter(cast(Iterator[object], response))
        self.observation = observation
        self.closed = False

    def __next__(self) -> object:
        """
        透传原始块, 正常结束与异常均只结算一次

        返回:
        - 原始 SDK 块, 结束时保持 StopIteration
        """
        if self.closed:
            raise StopIteration
        try:
            value = next(self.iterator)
        except StopIteration:
            self.observation.finish()
            self.close()
            raise
        except BaseException as error:
            self.observation.finish(error=error)
            self.close()
            raise
        self.observation.chunk(value)
        return value

    def close(self) -> None:
        """关闭尚未消费或提前停止的流, 保留已经记录的终态"""
        if self.closed:
            return
        self.closed = True
        self.observation.finish(error=GeneratorExit())
        close = getattr(self.response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                if self.observation.writer is not None:
                    self.observation.writer._gap("stream_close_failed")


class _AsyncStream(AsyncIterator[object]):
    """异步流的显式关闭入口, 不依赖生成器首次迭代"""

    def __init__(self, response: object, observation: _Observation) -> None:
        """
        保存异步 SDK 流与调用观察者

        参数:
        - response: 原始 SDK 流
        - observation: 本次调用的观察者
        """
        self.response = response
        self.iterator = aiter(cast(AsyncIterator[object], response))
        self.observation = observation
        self.closed = False

    async def __anext__(self) -> object:
        """
        等待并透传一个原始块

        返回:
        - 原始 SDK 块, 结束时保持 StopAsyncIteration
        """
        if self.closed:
            raise StopAsyncIteration
        try:
            value = await anext(self.iterator)
        except StopAsyncIteration:
            self.observation.finish()
            await self.aclose()
            raise
        except BaseException as error:
            self.observation.finish(error=error)
            await self.aclose()
            raise
        self.observation.chunk(value)
        return value

    async def aclose(self) -> None:
        """关闭异步 SDK 流, 未开始消费时仍记录取消"""
        if self.closed:
            return
        self.closed = True
        self.observation.finish(error=GeneratorExit())
        close = getattr(self.response, "aclose", None) or getattr(self.response, "close", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                if self.observation.writer is not None:
                    self.observation.writer._gap("stream_close_failed")


def complete_sync(create: Callable[..., object], request: Mapping[str, object], timeout: float | None,
                  *, provider: str = "", model_config: str = "", context_window: int | None = None) -> object:
    """
    在同步 SDK 边界只消费一次流, 无采集时直接透传

    参数:
    - create: 原始 SDK create
    - request: 最终调用参数
    - timeout: SDK 网络超时
    - provider: 可选配置 API 基址
    - model_config: 可选逻辑配置名
    - context_window: 可选模型总窗口

    返回:
    - 原始响应或保持块身份的同步迭代器
    """
    if current_writer() is None and current_trace() is None:
        return create(**request)
    observation = _Observation(request, timeout, provider, model_config, context_window)
    try:
        response = create(**request)
    except BaseException as error:
        observation.finish(error=error)
        raise
    if not request.get("stream"):
        observation.finish(response)
        return response

    return _SyncStream(response, observation)


async def complete_async(create: Callable[..., Awaitable[object]], request: Mapping[str, object], timeout: float | None,
                         *, provider: str = "", model_config: str = "", context_window: int | None = None) -> object:
    """
    在异步 SDK 边界分发观察, 不改变 SDK 重试策略

    参数:
    - create: 原始异步 SDK create
    - request: 最终调用参数
    - timeout: SDK 网络超时
    - provider: 可选配置 API 基址
    - model_config: 可选逻辑配置名
    - context_window: 可选模型总窗口

    返回:
    - 原始响应或保持块身份的异步迭代器
    """
    if current_writer() is None and current_trace() is None:
        return await create(**request)
    observation = _Observation(request, timeout, provider, model_config, context_window)
    try:
        response = await create(**request)
    except BaseException as error:
        observation.finish(error=error)
        raise
    if not request.get("stream"):
        observation.finish(response)
        return response

    return _AsyncStream(response, observation)
