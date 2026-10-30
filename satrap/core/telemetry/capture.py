"""
有界后台统计采集

宿主绑定可信调用归属, 消息和模型热路径只入队, 不等待文件锁或 SQLite
"""
from __future__ import annotations

from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import replace
from typing import Literal
from uuid import uuid4
import hashlib
from contextlib import contextmanager
import threading
import asyncio
from pathlib import Path
import atexit
import queue
import time
import os

from satrap.core.storage.layout import StorageLayout
from satrap.core.call_context import CallOrigin, is_group_origin, is_private_origin
from satrap.core.log.managed import LogFileLock, report_failure
from satrap.core.log.stream import standard_log_stream
from .contracts import ActivityEvent, JsonValue, MessageMetric, MetricScope, ModelCall, Service, TelemetrySettings
from .health import ServiceRuntime
from .store import SettingsStore, TelemetryStore, json_text


TelemetryEvent = ModelCall | MessageMetric | ActivityEvent
_SCOPE: ContextVar[MetricScope] = ContextVar("telemetry_scope", default=MetricScope())
_SERVICE: ContextVar[Service | None] = ContextVar("telemetry_service", default=None)
_WRITERS: dict[tuple[Path, Service], TelemetryWriter] = {}
_LOCK = threading.RLock()


@contextmanager
def bind_scope(scope: MetricScope | None = None, *, service: Service | None = None, **changes: str) -> Iterator[None]:
    """
    在宿主执行边界传递统计归属, 子调用退出后恢复上一层

    参数:
    - scope: 新的可信归属, 默认继承当前范围
    - service: 可选服务身份, 默认继承
    - changes: 当前范围内的固定字段覆盖, 不接受模型参数

    返回:
    - 当前调用内有效的统计作用域
    """
    value = scope or _SCOPE.get()
    if changes:
        value = replace(value, **changes)
    token = _SCOPE.set(value)
    service_token = _SERVICE.set(service or _SERVICE.get())
    try:
        from satrap.core.APICall.LLMCall.observation import bind_observers
        from . import model
        with bind_observers(model):
            yield
    finally:
        _SCOPE.reset(token)
        _SERVICE.reset(service_token)


def current_scope() -> MetricScope:
    """
    读取当前可信的统计归属

    返回:
    - 不包含授权凭据的不可变范围
    """
    return _SCOPE.get()


def scope_from_origin(origin: CallOrigin | None, *, session_id: str = "", agent: str = "") -> MetricScope:
    """
    将宿主已冻结的真实来源转换为计量归属, 不推断管理员权限

    参数:
    - origin: 本次真实平台来源, CLI 可为空
    - session_id: 当前实际执行实例
    - agent: 当前实际 Agent 配置身份

    返回:
    - 仅包含统计所需身份的不可变范围
    """
    if origin is None:
        return MetricScope(platform_id="local", session_id=session_id, agent=agent)
    kind = origin.conversation_kind or ("group" if is_group_origin(origin) else "private" if is_private_origin(origin) else "")
    return MetricScope(platform_id=origin.adapter_id, self_id=origin.self_id, conversation_kind=kind, target_id=origin.chat_id,
                       agent=agent, session_id=session_id, context_id=origin.conversation_id, request_id=origin.request_id)


def record_message(scope: MetricScope, message_id: str, *, received_at: float | None = None,
                   outbound: bool = False, command: bool = False) -> None:
    """
    在准入或确认发送边界提交去重计数, 历史查询不走本入口

    参数:
    - scope: 宿主核验的消息范围
    - message_id: 真实平台消息或 Chat 轮次身份
    - received_at: 实际接收时间, 缺少时使用当前墙钟
    - outbound: 是否确认出站
    - command: 是否已识别为命令消息
    """
    writer = current_writer()
    if writer is None:
        return
    if not message_id:
        writer._gap("message_identity_unavailable", count=1)
        return
    writer.submit(MessageMetric(message_id, scope, time.time() if received_at is None else received_at,
                                "outbound" if outbound else "inbound", command))


def current_writer() -> TelemetryWriter | None:
    """
    查询已由宿主启动的写入器, 不隐式创建线程或目录

    返回:
    - 当前服务写入器, 未启动时为 None
    """
    service = _SERVICE.get() or standard_log_stream.service
    with _LOCK:
        matches = [writer for (_, name), writer in _WRITERS.items() if name == service and not writer.closed]
        return matches[0] if len(matches) == 1 else None


class TelemetryWriter:
    """单服务的有界写入器, 与平台业务库隔离"""

    def __init__(self, store: TelemetryStore, runtime: ServiceRuntime, *, settings_store: SettingsStore | None = None,
                 capacity: int = 4096) -> None:
        """
        启动后台线程, 不在构造线程初始化数据库

        参数:
        - store: 当前服务独立库
        - runtime: 实际服务运行身份
        - settings_store: 可注入的策略文件
        - capacity: 有界队列容量, 默认 4096
        """
        self.store, self.runtime = store, runtime
        self.settings_store = settings_store or SettingsStore()
        self.settings = TelemetrySettings()
        self.queue: queue.Queue[TelemetryEvent] = queue.Queue(maxsize=capacity)
        self.closed = False
        self.dropped_events = 0
        self.last_error = ""
        self.maintenance_error = ""
        self._gaps: list[dict[str, JsonValue]] = []
        self._disabled: dict[str, float] = {}
        self._reported_failures: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"telemetry-{store.service}", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def _gap(self, reason: str, *, count: int = 0) -> None:
        """
        合并同类采集缺口, 只保留有界诊断资料

        参数:
        - reason: 固定错误原因
        - count: 本次未保存项数
        """
        now = time.time()
        with self._lock:
            first_failure = reason not in self._reported_failures
            self._reported_failures.add(reason)
            self.dropped_events += count
            if self._gaps and self._gaps[-1]["reason_code"] == reason:
                self._gaps[-1]["to"] = now
            else:
                self._gaps.append({"from": now, "to": now, "reason_code": reason})
                if len(self._gaps) > 100:
                    first, second = self._gaps[:2]
                    self._gaps[:2] = [{"from": first["from"], "to": second["to"], "reason_code": "multiple_collection_gaps"}]
        if first_failure:
            report_failure(f"统计采集出现缺口: {reason}")

    def submit(self, event: TelemetryEvent) -> bool:
        """
        非阻塞提交已冻结的元数据, 开关与容量都不能影响业务返回

        参数:
        - event: 无正文的模型, 消息或活动记录

        返回:
        - 是否接受, 未接受时记录覆盖缺口
        """
        if self.closed:
            return False
        if isinstance(event, ModelCall) and not self.settings.usage_enabled:
            return False
        if isinstance(event, ActivityEvent) and not self.settings.activity_enabled:
            return False
        try:
            self.queue.put_nowait(event)
            return True
        except queue.Full:
            self._gap("queue_full", count=1)
            return False

    def _run(self) -> None:
        """在后台读配置, 批写和清理, 故障只损失观测并明确留下缺口"""
        pending: list[TelemetryEvent] = []
        last_policy = last_cleanup = 0.0
        initialized = False
        lease = LogFileLock(self.store.path.with_suffix(".writer.lock"))
        try:
            if not lease.acquire():
                raise RuntimeError("当前统计库已有写入器")
            while not self._stop.is_set() or not self.queue.empty() or pending:
                clock = time.monotonic()
                try:
                    if clock - last_policy >= 5:
                        try:
                            updated = self.settings_store.read()[0]
                            with self._lock:
                                for field, reason in (("usage_enabled", "usage_disabled"), ("activity_enabled", "activity_disabled")):
                                    if not getattr(updated, field):
                                        self._disabled.setdefault(reason, time.time())
                                    elif reason in self._disabled:
                                        self._gaps.append({"from": self._disabled.pop(reason), "to": time.time(), "reason_code": reason})
                                self.settings = updated
                        except (OSError, ValueError, TimeoutError) as error:
                            self._gap("settings_unavailable")
                            report_failure(f"统计设置读取失败, 保留最后有效设置: {type(error).__name__}")
                        last_policy = clock
                    if not initialized:
                        self.store.begin_run(self.runtime.runtime_id, os.getpid(), self.runtime.started_at)
                        initialized = True
                        self.ready.set()
                    if not pending:
                        try:
                            pending.append(self.queue.get(timeout=1 if not self._stop.is_set() else .01))
                        except queue.Empty:
                            pass
                    while len(pending) < 128:
                        try:
                            pending.append(self.queue.get_nowait())
                        except queue.Empty:
                            break
                    with self._lock:
                        while len(self._gaps) + len(self._disabled) > 100:
                            first, second = self._gaps[:2]
                            self._gaps[:2] = [{"from": first["from"], "to": second["to"], "reason_code": "multiple_collection_gaps"}]
                        gaps = [dict(value) for value in self._gaps]
                        gaps.extend({"from": start, "to": time.time(), "reason_code": reason} for reason, start in self._disabled.items())
                        dropped = self.dropped_events
                    self.store.append(self.runtime.runtime_id, pending, dropped=dropped, gaps=gaps)
                    for _ in pending:
                        self.queue.task_done()
                    pending = []
                    self.last_error = ""
                    if clock - last_cleanup >= 3600:
                        try:
                            self.store.cleanup(self.settings)
                            self.maintenance_error = ""
                        except Exception as error:
                            self.maintenance_error = type(error).__name__
                            report_failure(f"统计存储维护失败: {type(error).__name__}")
                        last_cleanup = clock
                except Exception as error:
                    if self.last_error != type(error).__name__:
                        report_failure(f"统计写入失败: {type(error).__name__}")
                    self.last_error = type(error).__name__
                    self._gap("storage_unavailable", count=len(pending))
                    for _ in pending:
                        self.queue.task_done()
                    pending = []
                    self.ready.set()
                    if self._stop.is_set():
                        while True:
                            try:
                                self.queue.get_nowait()
                                self.queue.task_done()
                                self._gap("shutdown_not_drained", count=1)
                            except queue.Empty:
                                break
                        break
                    self._stop.wait(.25)
            if initialized:
                self.store.finish_run(self.runtime.runtime_id, clean=not self.last_error and self.queue.empty())
        except Exception as error:
            self.last_error = type(error).__name__
            if not initialized:
                self.closed = True
            report_failure(f"统计写入器退出: {type(error).__name__}")
        finally:
            lease.release()
            self.ready.set()

    def close(self) -> None:
        """停止接收并最多等待五秒排空, 未完成时保持异常退出证据"""
        if self.closed:
            return
        self.closed = True
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(5)
        if self._thread.is_alive():
            self.last_error = "shutdown_not_drained"
        with _LOCK:
            key = (self.store.path, self.store.service)
            if _WRITERS.get(key) is self:
                _WRITERS.pop(key, None)
        atexit.unregister(self.close)


def start_capture(service: Service, runtime: ServiceRuntime, layout: StorageLayout) -> TelemetryWriter:
    """
    在宿主启动工作线程内初始化唯一服务写入器

    参数:
    - service: 当前服务
    - runtime: 真实服务身份
    - layout: 当前配置已绑定的数据根

    返回:
    - 当前配置目录的复用写入器, 初始化故障通过 last_error 公开
    """
    store = TelemetryStore(layout.root / "telemetry" / f"{service}.sqlite", service)
    key = (store.path, service)
    with _LOCK:
        writer = _WRITERS.get(key)
        if writer is None or writer.closed:
            writer = TelemetryWriter(store, runtime)
            _WRITERS[key] = writer
    writer.ready.wait(3)
    return writer


def record(event: TelemetryEvent) -> None:
    """
    在已启用宿主内提交观测, 不因采集异常中断业务

    参数:
    - event: 已按固定结构构造的元数据
    """
    writer = current_writer()
    if writer is not None:
        try:
            writer.submit(event)
        except Exception:
            writer._gap("capture_failed", count=1)


def record_activity(kind: str, title: str, *, scope: MetricScope | None = None, tone: Literal["info", "warning", "error"] = "info",
                    event_key: str = "", reference: dict[str, JsonValue] | None = None) -> None:
    """
    在业务状态提交后登记摘要, 不保存正文或平台处理凭据

    参数:
    - kind: 固定业务事件类别
    - title: 简短的宿主生成标题
    - scope: 可信对象身份, 默认继承当前范围
    - tone: 正常, 提醒或错误分类
    - event_key: 可重放通知的稳定业务键, 缺少时创建唯一事件
    - reference: 固定业务引用, 不接受任意跳转 URL
    """
    writer = current_writer()
    if writer is None:
        return
    try:
        identity = hashlib.sha256((writer.store.service + ":" + event_key).encode("utf-8")).hexdigest() if event_key else uuid4().hex
        record(ActivityEvent(identity, time.time(), kind, tone, title[:240], scope or current_scope(), reference))
    except Exception:
        writer._gap("activity_capture_failed", count=1)


async def start_service_capture(service: Service, runtime: ServiceRuntime, layout: StorageLayout) -> TelemetryWriter | None:
    """
    在启动阶段将目录和数据库初始化移出事件循环

    参数:
    - service: 当前真实服务
    - runtime: 服务身份
    - layout: 当前配置的数据根

    返回:
    - 写入器或 None, 采集故障不使业务服务启动失败
    """
    try:
        return await asyncio.to_thread(start_capture, service, runtime, layout)
    except Exception as error:
        report_failure(f"统计采集启动失败: {type(error).__name__}")
        return None


def record_approval(source: Literal['group', 'invitation', 'friend', 'memory'], action_id: str, state: str, scope: MetricScope) -> None:
    """
    登记已经提交的待审或人工决定, 只引用完整业务身份

    参数:
    - source: 原业务账本来源
    - action_id: 原动作或提案 ID
    - state: 事务提交后的实际状态
    - scope: 宿主提供的平台, 账号和对象身份
    """
    if not scope.platform_id or not scope.self_id:
        return
    pending = state == 'pending'
    key: dict[str, JsonValue] = {'platform_id': scope.platform_id, 'self_id': scope.self_id, 'source': source, 'action_id': action_id}
    record_activity('approval.pending' if pending else 'approval.decided', '新增待审批事项' if pending else '审批决定已记录', scope=scope,
                    tone='warning' if pending or state in {'failed', 'unknown', 'conflicted', 'expired'} else 'info',
                    event_key=json_text(('approval', scope.platform_id, scope.self_id, source, action_id, state)),
                    reference={'kind': 'approval', 'key': key})


def record_session_completed() -> None:
    """按实际对象与 60 秒桶合并普通完成活动, 不复制输入或输出"""
    writer = current_writer()
    scope = current_scope()
    if writer is None or not scope.session_id:
        return
    try:
        identity = (scope.platform_id, scope.self_id, scope.conversation_kind, scope.target_id or scope.session_id, int(time.time() // 60))
        event_id = hashlib.sha256(json_text(('session.completed', writer.store.service, *identity)).encode()).hexdigest()
        record(ActivityEvent(event_id, time.time(), 'session.completed', 'info', '会话处理完成', scope,
                             {'kind': 'session', 'platform_id': scope.platform_id, 'session_id': scope.session_id}))
    except Exception:
        writer._gap("activity_capture_failed", count=1)
