"""
SDK 请求边界的高级诊断

同步和异步调用保留原接口结果, 流式输出按时间与字节合并,
只观察真实响应, 不改变 SDK 自动重试或业务错误处理
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
import inspect
import time
from typing import cast
from uuid import uuid4

from satrap.core.APICall.LLMCall.errors import model_error_info
from .capture import current_trace, record_debug, _failure


@dataclass
class _ToolDelta:
    """当前批次同一工具槽的增量片段, 落盘时合并字段"""
    ids: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    arguments: list[str] = field(default_factory=list)


@dataclass
class _ChoiceDelta:
    """当前批次同一候选的可见输出, 不保留 Token 到达边界"""
    content: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    tools: dict[int, _ToolDelta] = field(default_factory=dict)


def _data(value: object) -> object:
    """
    读取 SDK 公开序列化接口, 不用 repr 猜测响应正文

    参数:
    - value: SDK 响应字段或简单测试响应

    返回:
    - 可由采集器有界冻结的公开资料
    """
    if isinstance(value, (list, tuple)):
        return [_data(item) for item in value[:10000]]
    if isinstance(value, (Mapping, str, int, float, bool)) or value is None:
        return value
    dump = getattr(value, "model_dump", None)
    return dump(mode="json", exclude_none=True) if callable(dump) else {"omitted": True, "type": type(value).__name__}


class ModelObservation:
    """一次实际 SDK 调用的独立观察状态, 不保存在共享 LLM 实例中"""

    def __init__(self, request: Mapping[str, object], timeout: float | None) -> None:
        """
        记录最终 SDK 参数与开始时刻

        参数:
        - request: 已完成媒体和参数处理的实际请求
        - timeout: 当前 SDK 网络超时配置
        """
        self.span = uuid4().hex
        self.timeout = timeout
        self.model = request.get("model") if isinstance(request.get("model"), str) else ""
        self.started = self.last_flush = time.monotonic()
        self.first_chunk_ms: float | None = None
        self.parts: dict[int, _ChoiceDelta] = {}
        self.buffer_bytes = 0
        self.finish_reason: object = None
        self.usage: object = None
        self.finished = False
        record_debug("model", "start", span_id=self.span, bodies={"input": dict(request)}, metadata={"network_timeout": timeout, "model": self.model})

    def chunk(self, chunk: object) -> None:
        """
        隔离诊断观察失败, 不影响原始块的消费

        参数:
        - chunk: SDK 原始增量
        """
        try:
            self._chunk(chunk)
        except Exception as error:
            trace = current_trace()
            if trace is not None:
                trace.incomplete = True
            _failure(type(error).__name__)

    def _chunk(self, chunk: object) -> None:
        """
        收集实际可见增量与工具参数, 定期落盘而不逐 Token 写入

        参数:
        - chunk: SDK 实际返回的流式块
        """
        choices = getattr(chunk, "choices", ())
        if isinstance(choices, (list, tuple)):
            for choice in choices:
                delta = getattr(choice, "delta", None)
                content, calls = getattr(delta, "content", None), getattr(delta, "tool_calls", None)
                reasoning = next((value for name in ("reasoning_content", "reasoning", "thinking")
                                  if isinstance(value := getattr(delta, name, None), str) and value), None)
                legacy = getattr(delta, "function_call", None)
                if not calls and legacy is not None:
                    calls = [legacy]
                if content or calls or reasoning:
                    if self.first_chunk_ms is None:
                        self.first_chunk_ms = (time.monotonic() - self.started) * 1000
                    choice_index = getattr(choice, "index", 0)
                    choice_index = choice_index if isinstance(choice_index, int) else 0
                    part = self.parts.setdefault(choice_index, _ChoiceDelta())
                    if isinstance(content, str):
                        part.content.append(content)
                        self.buffer_bytes += len(content.encode("utf-8"))
                    if isinstance(reasoning, str):
                        part.reasoning.append(reasoning)
                        self.buffer_bytes += len(reasoning.encode("utf-8"))
                    if isinstance(calls, list):
                        for fallback, call in enumerate(calls):
                            index = getattr(call, "index", fallback)
                            index = index if isinstance(index, int) else fallback
                            tool = part.tools.setdefault(index, _ToolDelta())
                            function = getattr(call, "function", None) or call
                            for value, target in ((getattr(call, "id", None), tool.ids),
                                                  (getattr(function, "name", None), tool.names),
                                                  (getattr(function, "arguments", None), tool.arguments)):
                                if isinstance(value, str):
                                    target.append(value)
                                    self.buffer_bytes += len(value.encode("utf-8"))
                reason = getattr(choice, "finish_reason", None)
                if reason is not None:
                    self.finish_reason = reason
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self.usage = _data(usage)
        if self.buffer_bytes >= 32768 or time.monotonic() - self.last_flush >= .25:
            self.flush()

    def flush(self) -> None:
        """追加合并后的输出块, 结束事件不重复保存完整流式正文"""
        if self.parts:
            merged = [{"index": index, "content": "".join(part.content), **({"reasoning_content": "".join(part.reasoning)} if part.reasoning else {}), "tool_calls": [
                {"index": slot, "id": "".join(tool.ids), "function": {"name": "".join(tool.names), "arguments": "".join(tool.arguments)}}
                for slot, tool in sorted(part.tools.items())]} for index, part in sorted(self.parts.items())]
            record_debug("model", "update", span_id=self.span, metadata={"model": self.model}, bodies={"output_chunk": merged})
            self.parts = {}
            self.buffer_bytes = 0
        self.last_flush = time.monotonic()

    def finish(self, response: object = None, error: BaseException | None = None) -> None:
        """
        隔离响应观察故障, 业务继续接收原始 SDK 结果

        参数:
        - response: 原始非流式响应
        - error: 原始异常或取消
        """
        try:
            self._finish(response, error)
        except Exception as observation_error:
            trace = current_trace()
            if trace is not None:
                trace.incomplete = True
            record_debug("model", "end", span_id=self.span, status="observation_incomplete",
                         metadata={"observation_error_type": type(observation_error).__name__})
            _failure(type(observation_error).__name__)

    def _finish(self, response: object = None, error: BaseException | None = None) -> None:
        """
        记录实际结束或取消, 不替代调用方错误语义

        参数:
        - response: 非流式实际响应, 流式只记录已采集块
        - error: 可选异常, 原始异常文本不直接公开
        """
        if self.finished:
            return
        self.finished = True
        self.flush()
        if response is not None:
            choices = getattr(response, "choices", None)
            if isinstance(choices, (list, tuple)) and choices:
                reasons = [getattr(choice, "finish_reason", None) for choice in choices]
                self.finish_reason = reasons[0] if len(reasons) == 1 else reasons
            self.usage = _data(getattr(response, "usage", None))
        metadata: dict[str, object] = {"model": self.model, "duration_ms": (time.monotonic()-self.started)*1000,
                                       "first_chunk_ms": self.first_chunk_ms, "finish_reason": self.finish_reason,
                                       "usage": self.usage}
        status = "completed"
        if error is not None:
            if isinstance(error, Exception):
                info = model_error_info(error, timeout=self.timeout)
                metadata.update(code=info.code, reason=info.message, http_status=info.status)
                status = "timeout" if info.code.endswith("timeout") else "failed"
            else:
                status = "cancelled" if type(error).__name__ in {"CancelledError", "GeneratorExit"} else "interrupted"
                metadata["error_type"] = type(error).__name__
        record_debug("model", "end", span_id=self.span, status=status, metadata=metadata,
                     bodies={"output": _data(response)} if response is not None else None)


def complete_sync(create: Callable[..., object], request: Mapping[str, object], timeout: float | None) -> object:
    """
    观察同步 SDK 调用并保持返回值协议

    参数:
    - create: SDK 原始 create 方法
    - request: 实际最终参数
    - timeout: 当前网络超时

    返回:
    - 非流式响应或同步流式迭代器
    """
    if current_trace() is None:
        return create(**request)
    observation = ModelObservation(request, timeout)
    try:
        response = create(**request)
    except BaseException as error:
        observation.finish(error=error)
        raise
    if not request.get("stream"):
        observation.finish(response=response)
        return response

    def chunks() -> Iterator[object]:
        """逐块透传原始同步流, 取消与中断也写入结束记录"""
        try:
            for chunk in cast(Iterator[object], response):
                observation.chunk(chunk)
                yield chunk
        except BaseException as error:
            observation.finish(error=error)
            raise
        else:
            observation.finish()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as error:
                    _failure(type(error).__name__)

    return chunks()


async def complete_async(create: Callable[..., Awaitable[object]], request: Mapping[str, object], timeout: float | None) -> object:
    """
    观察异步 SDK 调用, 等待模型时不持有诊断库事务

    参数:
    - create: SDK 原始异步 create 方法
    - request: 实际最终参数
    - timeout: 当前网络超时

    返回:
    - 非流式响应或异步流式迭代器
    """
    if current_trace() is None:
        return await create(**request)
    observation = ModelObservation(request, timeout)
    try:
        response = await create(**request)
    except BaseException as error:
        observation.finish(error=error)
        raise
    if not request.get("stream"):
        observation.finish(response=response)
        return response

    async def chunks() -> AsyncIterator[object]:
        """逐块透传异步流, 保留取消时已收到的输出"""
        try:
            async for chunk in cast(AsyncIterator[object], response):
                observation.chunk(chunk)
                yield chunk
        except BaseException as error:
            observation.finish(error=error)
            raise
        else:
            observation.finish()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    pending = close()
                    if inspect.isawaitable(pending):
                        await pending
                except Exception as error:
                    _failure(type(error).__name__)

    return chunks()
