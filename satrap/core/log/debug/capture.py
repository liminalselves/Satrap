"""
高级诊断的请求作用域与有界后台追加

先冻结来源, 设置和有限正文, 后台只编码已采集资料;
日志故障与业务执行隔离, 丢失或超预算记录明确的不完整状态
"""
from __future__ import annotations

import atexit
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
import math
import json
import hashlib
import queue
import re
import threading
import time
from typing import cast
from uuid import uuid4

from satrap.core.log.managed import log_root, report_failure
from satrap.core.log.stream import standard_log_stream
from .contracts import DebugData, DebugEvent, DebugObject
from .policy import DebugSelection, policy_store
from .store import DebugStore
from .runtime import RuntimeLease


_SECRET_KEYS = frozenset({"api_key", "apikey", "api_token", "authorization", "password", "secret", "access_token", "refresh_token", "cookie", "set_cookie"})
_SECRET_TEXT = re.compile(r"(?i)\bBearer\s+[^\s,;]+|\bsk-[A-Za-z0-9_-]{16,}")
_SIGNED_QUERY = re.compile(r"(?i)([?&](?:token|api_key|access_token|signature|sig|key)=)[^&#\s]+")
_CURRENT: ContextVar[DebugTrace | None] = ContextVar("satrap_debug_trace", default=None)
_SPAN: ContextVar[str] = ContextVar("satrap_debug_span", default="")
_FAILURE_LOCK = threading.Lock()
_LAST_FAILURE = 0.0


def _failure(error: object) -> None:
    """
    限频报告诊断自身故障, 不递归进入诊断库

    参数:
    - error: 已去除请求正文的故障摘要
    """
    global _LAST_FAILURE
    with _FAILURE_LOCK:
        if time.monotonic() - _LAST_FAILURE < 30:
            return
        _LAST_FAILURE = time.monotonic()
    report_failure(f"高级 Debug 记录不完整: {error}")


class DebugWriter:
    """按服务限量排队, 保留关键帧入队余量并保持事实顺序"""

    def __init__(self, store: DebugStore, *, capacity: int = 512, max_pending_bytes: int = 64_000_000) -> None:
        """
        启动独立后台写入线程

        参数:
        - store: 当前服务固定诊断库
        - capacity: 普通事件数量上限
        - max_pending_bytes: 所有待写正文的原始字节预算
        """
        self.store = store
        self.capacity = capacity
        self.queue: queue.Queue[tuple[DebugEvent, int]] = queue.Queue(capacity + 64)
        self.max_pending_bytes = max_pending_bytes
        self._pending_bytes = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.last_error = ""
        self.maintenance_error = ""
        self._last_maintenance = 0.0
        self._last_quota_check = 0.0
        self._room = 512_000_000
        self.quota_blocked = False
        self.dropped_events = 0
        self.dropped_requests = 0
        self._failed_requests: set[str] = set()
        self.runtime = RuntimeLease(store.path.parent, store.service)
        self._worker = threading.Thread(target=self._run, name=f"debug-{store.service}", daemon=True)
        self._worker.start()
        atexit.register(self.close)

    def submit(self, event: DebugEvent, body_bytes: int = 0) -> bool:
        """
        非阻塞入队, 超出内存预算时仅保留元数据

        参数:
        - event: 已冻结且有界的事件
        - body_bytes: 正文原始字节估算

        返回:
        - 是否已接受事件, 正文省略由 incomplete 显式表示
        """
        if self._stop.is_set():
            return False
        if self.quota_blocked and event.category != "request":
            self.dropped_events += 1
            return False
        with self._lock:
            if self._pending_bytes + body_bytes > self.max_pending_bytes:
                event = replace(event, bodies={}, incomplete=True, metadata={**event.metadata, "omission": "writer_memory_budget"})
                body_bytes = 0
            target = self.queue
            try:
                if event.category != "request" and target.qsize() >= self.capacity:
                    raise queue.Full
                target.put_nowait((event, body_bytes))
            except queue.Full:
                self.dropped_events += 1
                _failure("诊断写入队列已满")
                return False
            self._pending_bytes += body_bytes
            return True

    def _run(self) -> None:
        """批量写入短事务, 写入失败不向业务线程抛出"""
        while not self._stop.is_set() or self.queue.unfinished_tasks:
            items: list[tuple[DebugEvent, int]] = []
            for _ in range(64):
                try:
                    items.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            if not items:
                if time.monotonic() - self._last_maintenance >= (1 if self.quota_blocked else 60) and self.store.path.is_file():
                    self._maintain()
                self._stop.wait(.05)
                continue
            try:
                if time.monotonic() - self._last_quota_check >= 1:
                    self._room = max(0, self._quota() - self.store.stats()["used_bytes"])
                    self.quota_blocked = self._room == 0
                    self._last_quota_check = time.monotonic()
                batch: list[DebugEvent] = []
                for event, count in items:
                    if count > self._room:
                        event = replace(event, bodies={}, incomplete=True, metadata={**event.metadata, "omission": "storage_budget"})
                    else:
                        self._room -= count
                    if event.request_id in self._failed_requests:
                        event = replace(event, incomplete=True, metadata={**event.metadata, "earlier_write_failed": True})
                    batch.append(event)
                self.store.append(batch)
            except Exception as error:
                self.last_error = f"{type(error).__name__}: 诊断事件写入失败"
                self._failed_requests.update(event.request_id for event, _ in items)
                if len(self._failed_requests) > 10000:
                    self._failed_requests = set(list(self._failed_requests)[-10000:])
                _failure(self.last_error)
            finally:
                with self._lock:
                    self._pending_bytes -= sum(count for _, count in items)
                for _ in items:
                    self.queue.task_done()
            if time.monotonic() - self._last_maintenance >= (1 if self.quota_blocked else 60):
                self._maintain()

    def _quota(self) -> int:
        """
        为已创建服务库均分部署总目标, 空闲服务不预留容量

        返回:
        - 当前服务有效页目标, 并发初次建库最多短暂超出一个批次
        """
        stores = sum(1 for name in ("control", "backend", "chat", "local") if (self.store.path.parent / f"{name}.sqlite").is_file())
        return policy_store().read().max_total_mb * 1_000_000 // max(1, stores)

    def _maintain(self) -> None:
        """定时回收结束请求和无引用正文, 故障不阻断后续记录"""
        self._last_maintenance = time.monotonic()
        try:
            settings = policy_store().read()
            self.store.cleanup(retention_days=settings.retention_days, target_bytes=self._quota())
            self._room = max(0, self._quota() - self.store.stats()["used_bytes"])
            self.quota_blocked = self._room == 0
            self.maintenance_error = ""
        except Exception as error:
            self.maintenance_error = type(error).__name__
            _failure(f"诊断自动清理未完成: {type(error).__name__}")

    def flush(self, timeout: float = 5) -> bool:
        """
        等待已接收记录落盘, 用于维护和验收

        参数:
        - timeout: 最多等待秒数

        返回:
        - 是否排空且未遇到最近写入故障
        """
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(.01)
        return not self.last_error

    def close(self) -> None:
        """停止接受事件并有限等待后台收尾"""
        self._stop.set()
        if self._worker is not threading.current_thread():
            self._worker.join(5)
        if self._worker.is_alive():
            _failure("服务退出前诊断队列未排空")
        else:
            self.runtime.close()
        atexit.unregister(self.close)


_WRITERS: dict[tuple[Path, str], DebugWriter] = {}
_WRITERS_LOCK = threading.Lock()


def debug_store(service: str | None = None) -> DebugStore:
    """
    取得服务自己的诊断库, 查询不必先开启采集线程

    参数:
    - service: 当前服务, 默认沿用普通日志服务标记

    返回:
    - 固定路径的独立存储
    """
    selected = service or standard_log_stream.service
    if selected not in {"control", "backend", "chat", "local"}:
        selected = "local"
    return DebugStore(log_root() / "debug" / f"{selected}.sqlite", selected)


def debug_writer(service: str | None = None) -> DebugWriter:
    """
    按需复用当前进程和路径的后台写入器

    参数:
    - service: 当前实际服务, 默认沿用普通日志服务标记

    返回:
    - 有界写入器, 关闭高级开关时不调用本函数
    """
    store = debug_store(service)
    key = store.path, store.service
    with _WRITERS_LOCK:
        writer = _WRITERS.get(key)
        if writer is None:
            writer = DebugWriter(store)
            _WRITERS[key] = writer
        return writer


def maintain_existing_debug(service: str) -> None:
    """
    服务启动时恢复已有诊断库的定时清理, 不创建空诊断库

    参数:
    - service: 宿主启动的实际服务标签
    """
    try:
        if debug_store(service).path.is_file():
            debug_writer(service)
    except Exception as error:
        _failure(f"诊断维护启动失败: {type(error).__name__}")


def debug_health(service: str) -> dict[str, object]:
    """
    汇总当前服务空间与写入器状态, 不因查询创建线程

    参数:
    - service: 当前实际服务标识

    返回:
    - 空间统计, 排队数量及可见故障
    """
    store = debug_store(service)
    with _WRITERS_LOCK:
        writer = _WRITERS.get((store.path, store.service))
    return {"service": service, **store.stats(), "pending_events": writer.queue.unfinished_tasks if writer else 0,
            "write_error": writer.last_error if writer else "", "maintenance_error": writer.maintenance_error if writer else "",
            "quota_blocked": writer.quota_blocked if writer else False, "dropped_events": writer.dropped_events if writer else 0,
            "dropped_requests": writer.dropped_requests if writer else 0}


class DebugTrace:
    """单请求冻结策略和采集预算, 可由关联线程共享"""

    def __init__(self, selection: DebugSelection, request_id: str, *, context_id: str = "", title: str = "",
                 parent_request_id: str = "", writer: DebugWriter | None = None) -> None:
        """
        初始化请求身份与正文预算

        参数:
        - selection: 已在受理边界冻结的对象设置
        - request_id: 宿主真实请求标识
        - context_id: 可选上下文, 未绑定时允许为空
        - title: 上下文展示名称
        - parent_request_id: 重试或分支来源请求
        - writer: 测试可注入隔离写入器
        """
        self.selection, self.request_id = selection, request_id
        self.context_id, self.title, self.parent_request_id = context_id, title, parent_request_id
        self.writer = writer or debug_writer()
        self._lock = threading.RLock()
        self._sequence = 0
        self._remaining = selection.max_request_mb * 1_000_000
        self._active = True
        self.incomplete = False
        self.status = "success"

    def _freeze(self, value: object, remaining: list[int], active: set[int], depth: int = 0) -> DebugData:
        """
        在采集边界按原始字节预算冻结和脱敏正文

        参数:
        - value: 原始资料, 不执行任意对象 repr
        - remaining: 本正文剩余原始预算
        - active: 当前遍历路径, 防止循环容器
        - depth: 已遍历深度

        返回:
        - 有界 JSON 数据或明确省略标记
        """
        if remaining[0] <= 0 or depth > 32:
            self.incomplete = True
            return {"omitted": True, "reason": "capture_budget"}
        if value is None or isinstance(value, (bool, int)):
            remaining[0] -= 16
            return value
        if isinstance(value, float):
            remaining[0] -= 24
            if math.isfinite(value):
                return value
            self.incomplete = True
            return {"omitted": True, "reason": "non_finite_number"}
        if isinstance(value, str):
            if value.startswith(("data:", "base64://")):
                return {"omitted": True, "reason": "inline_asset", "characters": len(value)}
            size = min(len(value), max(0, remaining[0]))
            text = _SIGNED_QUERY.sub(r"\1[REDACTED]", _SECRET_TEXT.sub("[REDACTED]", value[:size]))
            raw = text.encode("utf-8")
            allowed = max(0, remaining[0])
            clipped = raw[:allowed].decode("utf-8", errors="ignore")
            remaining[0] -= min(len(raw), allowed)
            if len(raw) > allowed or size < len(value):
                self.incomplete = True
                return {"text": clipped, "truncated": True, "original_characters": len(value)}
            return text
        if isinstance(value, (Mapping, list, tuple)):
            identity = id(value)
            if identity in active:
                self.incomplete = True
                return {"omitted": True, "reason": "recursive_value"}
            active.add(identity)
            remaining[0] -= 16
            if isinstance(value, Mapping):
                result: dict[str, DebugData] = {}
                for index, (key, item) in enumerate(value.items()):
                    if index >= 10000 or remaining[0] <= 0:
                        result["_capture_omission"] = "budget"
                        self.incomplete = True
                        break
                    if not isinstance(key, str) or len(key) > 512:
                        self.incomplete = True
                        continue
                    remaining[0] -= len(key.encode("utf-8")) + 8
                    name = key.casefold().replace("-", "_")
                    result[key] = "[REDACTED]" if name in _SECRET_KEYS else self._freeze(item, remaining, active, depth + 1)
                frozen: DebugData = result
            else:
                values: list[DebugData] = []
                for index, item in enumerate(value):
                    if index >= 10000 or remaining[0] <= 0:
                        values.append({"omitted": True, "reason": "capture_budget"})
                        self.incomplete = True
                        break
                    values.append(self._freeze(item, remaining, active, depth + 1))
                frozen = values
            active.remove(identity)
            return frozen
        self.incomplete = True
        return {"omitted": True, "reason": "non_json_value", "type": type(value).__name__}

    def emit(self, category: str, phase: str, *, status: str = "", metadata: Mapping[str, object] | None = None,
             bodies: Mapping[str, object] | None = None, span_id: str = "", parent_span_id: str = "") -> None:
        """
        冻结一条事实并非阻塞入队

        参数:
        - category: 入站, 模型, 工具等固定阶段名
        - phase: start, end 或 update
        - status: 本阶段执行状态
        - metadata: 有界控制资料
        - bodies: 仅在诊断开启时提供的正文槽位
        - span_id: 当前执行步骤标识
        - parent_span_id: 父步骤标识
        """
        try:
            self._emit(category, phase, status=status, metadata=metadata, bodies=bodies, span_id=span_id, parent_span_id=parent_span_id)
        except Exception as error:
            self.incomplete = True
            _failure(type(error).__name__)

    def _emit(self, category: str, phase: str, *, status: str, metadata: Mapping[str, object] | None,
              bodies: Mapping[str, object] | None, span_id: str, parent_span_id: str) -> None:
        """
        在异常隔离边界内冻结事件并递增序号

        参数:
        - category: 固定阶段类别
        - phase: 执行阶段
        - status: 实际执行状态
        - metadata: 有界元数据
        - bodies: 待冻结正文
        - span_id: 步骤标识
        - parent_span_id: 父步骤标识
        """
        with self._lock:
            if not self._active:
                return
            limit = [min(self._remaining, self.selection.max_body_mb * 1_000_000)]
            original_limit = limit[0]
            data: dict[str, DebugData] = {}
            for slot, value in (bodies or {}).items():
                data[slot] = self._freeze(value, limit, set())
            consumed = max(0, original_limit - max(0, limit[0]))
            self._remaining -= consumed
            meta = self._freeze(dict(metadata or {}), [16000], set())
            assert isinstance(meta, dict)
            if category == "request" and phase == "start":
                meta["runtime_id"] = self.writer.runtime.identity
            event = DebugEvent(self.request_id, self.selection.object, self.context_id, self.title, uuid4().hex,
                               self._sequence, category, phase, span_id, parent_span_id, time.time(), status,
                               meta, data, self.selection.revision, self.parent_request_id, self.incomplete)
            self._sequence += 1
            if not self.writer.submit(event, consumed):
                self.incomplete = True

    def bind_context(self, context_id: str, title: str = "") -> None:
        """
        在真实路由完成后绑定上下文, 不改变对象开关

        参数:
        - context_id: 实际上下文标识
        - title: 可选展示标题
        """
        with self._lock:
            self.context_id = context_id
            self.title = title or self.title
            self.emit("context", "update", metadata={"context_id": context_id})

    def finish(self, status: str | None = None) -> None:
        """
        只收尾一次并撤销迟到子任务的采集权

        参数:
        - status: 可选的明确终态, 默认使用执行过程中设置的状态
        """
        with self._lock:
            if self._active:
                try:
                    self.emit("request", "end", status=status or self.status, metadata={"incomplete": self.incomplete})
                finally:
                    self._active = False


def current_trace() -> DebugTrace | None:
    """
    取得当前请求的已绑定诊断作用域

    返回:
    - 未开启时为 None
    """
    trace = _CURRENT.get()
    return trace if trace is not None and trace._active else None


def record_session_start(session: object) -> None:
    """
    声明本轮会话内部的可观测范围, 自定义实现不冒充完整内置链路

    参数:
    - session: 宿主实际取得的会话对象
    """
    trace = current_trace()
    if trace is None:
        return
    try:
        from satrap.edictum import SimpleSession, AsyncSimpleSession

        builtin = isinstance(session, (SimpleSession, AsyncSimpleSession))
        if not builtin:
            trace.incomplete = True
        record_debug("session", "start", metadata={"implementation": f"{type(session).__module__}.{type(session).__qualname__}",
                     "internal_coverage": "builtin" if builtin else "custom_partial"})
    except Exception as error:
        trace.incomplete = True
        _failure(type(error).__name__)


def record_platform_action(domain: str, database: Path, account: str, action_id: str, state: str,
                           *, metadata: Mapping[str, object] | None = None) -> None:
    """
    关联群和好友的真实动作状态, 延后决定创建子请求而不复活已结束作用域

    参数:
    - domain: group 或 friend 动作命名空间
    - database: 已提交事务的实际平台数据库
    - account: 当前机器人账号
    - action_id: 真实动作 ID
    - state: 提交后实际状态
    - metadata: 已去除处理凭据的公开结果资料
    """
    try:
        if current_trace() is None:
            policy = policy_store().read()
            if not policy.default_enabled and not policy.objects:
                return
        link = hashlib.sha256(json.dumps([domain, str(database.resolve()), account, action_id]).encode("utf-8")).hexdigest()
        details = {**(metadata or {}), "domain": domain, "action_id": action_id, "action_link": link}
        if current_trace() is not None:
            record_debug("platform_approval", "update", status=state, metadata=details)
            return
        source = debug_store().action_source(link)
        if source is None:
            return
        obj, parent, context, title = source
        with debug_request(obj, "action-" + uuid4().hex, context_id=context, title=title, parent_request_id=parent) as trace:
            if trace is not None:
                trace.status = state if state in {"failed", "unknown", "cancelled", "interrupted"} else "success"
            record_debug("platform_approval", "update", status=state, metadata=details)
    except Exception as error:
        trace = current_trace()
        if trace is not None:
            trace.incomplete = True
        _failure(type(error).__name__)


def previous_chat_request(obj: DebugObject, context_id: str, version: Mapping[str, int], resume_run_id: str | None) -> str:
    """
    用真实回复版本或恢复任务寻找来源, 查询失败不阻断聊天

    参数:
    - obj: 当前 Chat 对象
    - context_id: 持久 Chat 会话 ID
    - version: Recorder 给出的当前轮次与版本
    - resume_run_id: 已校验的恢复任务, 默认无

    返回:
    - 已存在的来源请求 ID, 缺失时保留业务来源资料而不伪造节点
    """
    try:
        store = debug_store("chat")
        if resume_run_id:
            return store.find_link("run", resume_run_id)
        variant, turn = version.get("variant_index", 0), version.get("turn_id")
        if variant > 0 and turn is not None:
            return store.find_link("turn", json.dumps([obj.key, context_id, turn, variant-1]))
        return store.find_link("fork", context_id)
    except Exception as error:
        _failure(type(error).__name__)
        return ""


@contextmanager
def debug_request(obj: DebugObject, request_id: str, *, context_id: str = "", title: str = "",
                  parent_request_id: str = "", input_data: object = None) -> Iterator[DebugTrace | None]:
    """
    冻结下一请求的对象设置, 不让诊断错误中断业务

    参数:
    - obj: 当前可信对象
    - request_id: 实际请求标识
    - context_id: 可选已知上下文
    - title: 可选标题
    - parent_request_id: 可选来源请求
    - input_data: 入站正文与附件资料

    返回:
    - 开启时的诊断作用域, 关闭或采集初始化失败时为 None
    """
    trace = None
    try:
        selection = policy_store().read().resolve(obj)
        if selection.enabled:
            writer = debug_writer()
            if writer.quota_blocked:
                writer.dropped_requests += 1
                _failure("诊断容量不足, 已暂停新请求采集")
            else:
                trace = DebugTrace(selection, request_id, context_id=context_id, title=title, parent_request_id=parent_request_id, writer=writer)
                trace.emit("request", "start", bodies={"input": input_data})
    except Exception as error:
        _failure(type(error).__name__)
    token = _CURRENT.set(trace)
    span_token = _SPAN.set("")
    try:
        yield trace
    except BaseException as error:
        if trace is not None:
            info = getattr(error, "info", None)
            code = getattr(info, "code", "")
            trace.status = "cancelled" if type(error).__name__ == "CancelledError" else "timeout" if isinstance(error, TimeoutError) or str(code).endswith("timeout") else "failed"
            trace.emit("request_error", "end", status=trace.status, metadata={"error_type": type(error).__name__, "code": code})
        raise
    finally:
        try:
            if trace is not None:
                trace.finish()
        except Exception as error:
            _failure(type(error).__name__)
        _CURRENT.reset(token)
        _SPAN.reset(span_token)


def record_debug(category: str, phase: str, *, status: str = "", metadata: Mapping[str, object] | None = None,
                 bodies: Mapping[str, object] | None = None, span_id: str = "", parent_span_id: str = "") -> None:
    """
    就地记录事实, 无作用域时不启用写入线程

    参数:
    - category: 固定阶段类别
    - phase: 执行阶段
    - status: 实际状态
    - metadata: 受控元数据
    - bodies: 正文槽位
    - span_id: 当前步骤
    - parent_span_id: 父步骤, 默认从实际作用域读取
    """
    trace = current_trace()
    if trace is None:
        return
    try:
        trace.emit(category, phase, status=status, metadata=metadata, bodies=bodies,
                   span_id=span_id, parent_span_id=parent_span_id or _SPAN.get())
    except Exception as error:
        trace.incomplete = True
        _failure(type(error).__name__)


@contextmanager
def debug_span(category: str, *, metadata: Mapping[str, object] | None = None,
               bodies: Mapping[str, object] | None = None, span_id: str = "") -> Iterator[str]:
    """
    关联模型或工具步骤, 执行异常继续向原业务传播

    参数:
    - category: 当前步骤类型
    - metadata: 开始资料
    - bodies: 实际输入
    - span_id: 可选的业务调用 ID

    返回:
    - 当前诊断步骤 ID
    """
    if current_trace() is None:
        yield ""
        return
    identity = span_id or uuid4().hex
    parent = _SPAN.get()
    started = time.monotonic()
    record_debug(category, "start", metadata=metadata, bodies=bodies, span_id=identity, parent_span_id=parent)
    token = _SPAN.set(identity)
    try:
        yield identity
    except BaseException as error:
        record_debug(category, "end", status="cancelled" if type(error).__name__ == "CancelledError" else "failed",
                     metadata={**{key: value for key, value in (metadata or {}).items() if key not in {"diagnostic_level", "error_kind", "error_code", "reason"}},
                               "error_type": type(error).__name__, "duration_ms": (time.monotonic()-started)*1000},
                     span_id=identity, parent_span_id=parent)
        raise
    else:
        record_debug(category, "end", status="completed", metadata={**(metadata or {}), "duration_ms": (time.monotonic()-started)*1000},
                     span_id=identity, parent_span_id=parent)
    finally:
        _SPAN.reset(token)
