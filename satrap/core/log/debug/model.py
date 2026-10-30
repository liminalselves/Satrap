"""
SDK 请求边界的高级诊断

同步和异步调用保留原接口结果, 流式输出按时间与字节合并,
只观察真实响应, 不改变 SDK 自动重试或业务错误处理
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import time
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
        self.response_status: object = None
        self.response_id: object = None
        self.responses_call_slots: dict[str, int] = {}
        self.responses_mode = "input" in request and "messages" not in request
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
        from satrap.core.APICall.LLMCall.protocols.responses import field
        kind = field(chunk, "type")
        if isinstance(kind, str) and (kind.startswith("response.") or kind == "error"):
            self._responses_chunk(chunk)
            return
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

    def _responses_chunk(self, event: object) -> None:
        """
        采集 Responses 新增动作, 终态不重复写完整正文

        参数:
        - event: 原始 SSE 事件
        """
        from satrap.core.APICall.LLMCall.protocols.responses import field, text
        kind = field(event, "type")
        delta = text(field(event, "delta"))
        visible = False
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            self.parts.setdefault(0, _ChoiceDelta()).content.append(delta)
            visible = True
        elif kind == "response.reasoning_summary_text.delta":
            self.parts.setdefault(0, _ChoiceDelta()).reasoning.append(delta)
            visible = True
        elif kind == "response.output_item.added" and field(field(event, "item"), "type") == "function_call":
            item = field(event, "item")
            item_id = text(field(item, "id"))
            index = len(self.responses_call_slots)
            self.responses_call_slots[item_id] = index
            tool = self.parts.setdefault(0, _ChoiceDelta()).tools.setdefault(index, _ToolDelta())
            tool.ids.append(text(field(item, "call_id")))
            tool.names.append(text(field(item, "name")))
            visible = True
        elif kind == "response.function_call_arguments.delta":
            index = self.responses_call_slots.get(text(field(event, "item_id")))
            if index is not None:
                self.parts.setdefault(0, _ChoiceDelta()).tools.setdefault(index, _ToolDelta()).arguments.append(delta)
                visible = True
        elif kind in {"response.completed", "response.failed", "response.incomplete"}:
            response = field(event, "response")
            self.response_status = field(response, "status")
            self.response_id = field(response, "id")
            self.finish_reason = field(field(response, "incomplete_details"), "reason", self.response_status)
            self.usage = _data(field(response, "usage"))
        elif kind == "error":
            self.response_status = "failed"
        if visible:
            self.buffer_bytes += len(delta.encode("utf-8"))
            if self.first_chunk_ms is None:
                self.first_chunk_ms = (time.monotonic() - self.started) * 1000
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
            from satrap.core.APICall.LLMCall.protocols.responses import field
            self.response_status = field(response, "status", self.response_status)
            self.response_id = field(response, "id", self.response_id)
            choices = getattr(response, "choices", None)
            if isinstance(choices, (list, tuple)) and choices:
                reasons = [getattr(choice, "finish_reason", None) for choice in choices]
                self.finish_reason = reasons[0] if len(reasons) == 1 else reasons
            self.usage = _data(getattr(response, "usage", None))
        metadata: dict[str, object] = {"model": self.model, "duration_ms": (time.monotonic()-self.started)*1000,
                                       "first_chunk_ms": self.first_chunk_ms, "finish_reason": self.finish_reason,
                                       "usage": self.usage}
        status = "completed"
        if self.response_status in {"failed", "incomplete", "cancelled"}:
            status = "failed"
        elif self.responses_mode and self.response_status != "completed":
            status = "failed"
        if self.response_status is not None:
            metadata.update(response_status=self.response_status, response_id=self.response_id)
        if self.responses_mode and status == "failed":
            metadata.update(code="provider_error", reason="模型服务响应未完成或响应流提前结束")
        if error is not None:
            if isinstance(error, Exception):
                info = model_error_info(error, timeout=self.timeout)
                metadata.update(code=info.code, reason=info.message, http_status=info.status)
                status = "timeout" if info.code.endswith("timeout") else "failed"
            else:
                if not (self.responses_mode and self.response_status in {"failed", "incomplete"}):
                    status = "cancelled" if type(error).__name__ in {"CancelledError", "GeneratorExit"} else "interrupted"
                metadata["error_type"] = type(error).__name__
        record_debug("model", "end", span_id=self.span, status=status, metadata=metadata,
                     bodies={"output": _data(response)} if response is not None else None)


def complete_sync(create: Callable[..., object], request: Mapping[str, object], timeout: float | None) -> object:
    """
    兼容已有同步观察入口, 实际消费集中在共用 SDK 边界

    参数:
    - create: 原始 SDK 方法
    - request: 最终参数
    - timeout: SDK 网络超时

    返回:
    - 原始响应或观察迭代器
    """
    from satrap.core.telemetry.model import complete_sync as observe
    return observe(create, request, timeout)


async def complete_async(create: Callable[..., Awaitable[object]], request: Mapping[str, object], timeout: float | None) -> object:
    """
    兼容已有异步观察入口, 不额外包装或消费一次响应流

    参数:
    - create: 原始异步 SDK 方法
    - request: 最终参数
    - timeout: SDK 网络超时

    返回:
    - 原始响应或观察迭代器
    """
    from satrap.core.telemetry.model import complete_async as observe
    return await observe(create, request, timeout)
