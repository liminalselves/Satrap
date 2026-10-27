"""进程内应用任务及实际阶段观测, 不携带工具身份或缓存授权"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import re
import threading
import time
import traceback
from typing import Any

from satrap.core.log import logger


_ID = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_PHASES = {
    "config_read_ms": "config_read", "queue_and_lock_wait_ms": "waiting_lock",
    "install_ms": "install", "uninstall_ms": "uninstall", "model_build_ms": "model_build",
    "candidate_prepare_ms": "candidate_prepare", "source_check_ms": "source_check",
    "context_sync_ms": "context_sync", "apply_ms": "applying",
    "rollback_ms": "rollback",
    "dispatch_ms": "dispatch", "await_runtime_ms": "awaiting_runtime",
}


class ApplicationProgressError(ValueError):
    """携带可返回 HTTP 状态的任务输入, 冲突或容量错误"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class _Stage:
    """一个任务或实例的阶段, 只记录固定阶段和脱敏实例标识"""

    platform_id: str = ""
    session_id: str = ""
    status: str = "running"
    phase: str = "queued"
    phase_started: float = field(default_factory=time.perf_counter)
    timings_ms: dict[str, float] = field(default_factory=dict)


@dataclass
class _Operation:
    """单个应用任务, 请求正文仅计算指纹而不保存在记录中"""

    fingerprint: str
    generation: object = field(default_factory=object)
    created_at: float = field(default_factory=time.time)
    started: float = field(default_factory=time.perf_counter)
    status: str = "running"
    finished: float | None = None
    result: dict[str, Any] | None = None
    error: str = ""
    stage: _Stage = field(default_factory=_Stage)
    activity_kind: str = "config"
    instances: OrderedDict[tuple[str, str], _Stage] = field(default_factory=OrderedDict)
    instances_limited: bool = False
    task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class _Reporter:
    """仅供本应用任务的阶段观测, 任务终结后迟到的工作线程更新无效"""

    registry: ApplicationOperations
    operation_id: str
    generation: object
    instance: tuple[str, str] | None = None


_REPORTER: ContextVar[_Reporter | None] = ContextVar("application_progress", default=None)


class ApplicationOperations:
    """有界任务记录, 同 ID 同正文不会重复执行, 每个服务进程独立持有"""

    def __init__(self, *, max_active: int = 16, max_completed: int = 32, ttl: float = 900,
                 max_instances: int = 256) -> None:
        if min(max_active, max_completed, max_instances) < 1 or ttl <= 0:
            raise ValueError("应用任务容量和保留时间必须大于 0")
        self._max_active = max_active
        self._max_completed = max_completed
        self._max_instances = max_instances
        self._ttl = ttl
        self._lock = threading.RLock()
        self._operations: OrderedDict[str, _Operation] = OrderedDict()

    @staticmethod
    def validate_id(operation_id: object) -> str:
        """校验不透明客户端任务 ID, 不允许路径或任意长度的输入"""
        if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
            raise ApplicationProgressError(400, "operation_id 必须为 16-128 位字母, 数字, 下划线或连字符")
        return operation_id

    def _prune(self) -> None:
        """仅淘汰已终结记录, 活跃任务不因保留时间或查询被取消"""
        now = time.perf_counter()
        completed = sorted((key for key, item in self._operations.items() if item.finished is not None),
                           key=lambda key: self._operations[key].finished or 0)
        for key in completed:
            finished = self._operations[key].finished
            if finished is not None and now - finished > self._ttl:
                del self._operations[key]
        completed = sorted((key for key, item in self._operations.items() if item.finished is not None),
                           key=lambda key: self._operations[key].finished or 0)
        for key in completed[:-self._max_completed]:
            del self._operations[key]

    def submit(self, operation_id: object, request: Mapping[str, Any],
               action: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        """登记并启动应用任务, 请求断线不撤销已接受任务, 同 ID 不自动重放"""
        key = self.validate_id(operation_id)
        fingerprint = hashlib.sha256(json.dumps(dict(request), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        with self._lock:
            self._prune()
            existing = self._operations.get(key)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise ApplicationProgressError(409, "该 operation_id 已用于不同的应用请求")
                return self.get(key)
            if sum(item.status == "running" for item in self._operations.values()) >= self._max_active:
                raise ApplicationProgressError(503, "应用任务队列已满, 未接受本次应用")
            operation = _Operation(fingerprint)
            operation.activity_kind = "plugin" if "plugin" in str(request.get("route", "")) else "config"
            operation.stage.platform_id = request.get("platform_id", "") if isinstance(request.get("platform_id", ""), str) else ""
            operation.stage.session_id = request.get("session_id", "") if isinstance(request.get("session_id", ""), str) else ""
            self._operations[key] = operation
            operation.task = asyncio.create_task(self._run(key, operation, action), name=f"application:{key}")
            operation.task.add_done_callback(lambda task: self._task_done(key, operation, task))
            return self.get(key)

    def _task_done(self, key: str, operation: _Operation, task: asyncio.Task[None]) -> None:
        """任务还未开始就取消也须终结记录, 并取出逃逸异常避免后台警告"""
        error = None if task.cancelled() else task.exception()
        with self._lock:
            if operation.finished is not None:
                return
            operation.status = "cancelled" if task.cancelled() else "failed"
            operation.error = "应用任务启动前被取消, 生效状态未确认" if task.cancelled() else str(error or "应用任务未正常终结")
            operation.finished = time.perf_counter()
            operation.stage.status = operation.status
            operation.stage.phase = "finished"
            operation.task = None
            self._prune()
        logger.error(f"[应用任务] 任务 {key} 未正常终结: {operation.error}\n" + ("" if error is None else "".join(traceback.format_exception(error))))
        self._record_activity(key, operation)

    async def _run(self, key: str, operation: _Operation, action: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        """捕获失败和取消, 终态保留实际结果, 不把部分失败解释为成功"""
        token = _REPORTER.set(_Reporter(self, key, operation.generation))
        try:
            with progress_phase("apply_ms"):
                result = await action()
            if not isinstance(result, dict):
                raise TypeError("应用结果必须是对象")
            with self._lock:
                operation.result = deepcopy(result)
                operation.status = "completed" if result.get("ok", False) and result.get("applied", True) else "failed"
        except asyncio.CancelledError:
            with self._lock:
                operation.status = "cancelled"
                operation.error = "应用任务已被服务取消, 不能据此确认运行时生效"
            logger.warning(f"[应用任务] 服务取消任务 {key}")
            raise
        except Exception as error:
            logger.error(f"[应用任务] 任务 {key} 失败: {error}\n{traceback.format_exc()}")
            with self._lock:
                operation.status = "failed"
                operation.error = str(error)
        finally:
            with self._lock:
                operation.finished = time.perf_counter()
                operation.stage.status = operation.status
                operation.stage.phase = "finished"
                for stage in operation.instances.values():
                    if stage.status == "running":
                        stage.status = operation.status
                operation.task = None
                self._prune()
            _REPORTER.reset(token)
            self._record_activity(key, operation)

    @staticmethod
    def _record_activity(key: str, operation: _Operation) -> None:
        """
        任务终结后记录实际应用结果, 不保存配置正文或异常正文

        参数:
        - key: 宿主已验证的任务 ID
        - operation: 已更新的最终任务状态
        """
        from satrap.core.telemetry.capture import current_writer, record_activity
        from satrap.core.telemetry.contracts import MetricScope

        writer = current_writer()
        if writer is None:
            return
        success = operation.status == "completed"
        category = operation.activity_kind
        record_activity(category + (".applied" if success else ".failed"), "插件已应用" if category == "plugin" and success else "配置已应用" if success else "应用任务未完成",
                        tone="info" if success else "error", scope=MetricScope(platform_id=operation.stage.platform_id, session_id=operation.stage.session_id),
                        event_key=f"{writer.runtime.runtime_id}:{category}:{key}",
                        reference={"kind": "plugin_application", "service": writer.store.service, "operation_id": key} if category == "plugin" else None)

    def get(self, operation_id: object) -> dict[str, Any]:
        """返回独立读取快照, 不存在可能表示重启/淘汰/尚未接受, 不触发应用"""
        key = self.validate_id(operation_id)
        with self._lock:
            self._prune()
            operation = self._operations.get(key)
            if operation is None:
                raise ApplicationProgressError(404, "应用任务不存在或已不在本进程保留范围内, 生效状态未确认")
            now = operation.finished if operation.finished is not None else time.perf_counter()

            def stage_payload(stage: _Stage) -> dict[str, Any]:
                return {"platform_id": stage.platform_id, "session_id": stage.session_id, "status": stage.status,
                        "phase": stage.phase, "phase_elapsed_ms": round(max(0, now - stage.phase_started) * 1000, 3),
                        "timings_ms": {name: round(value, 3) for name, value in stage.timings_ms.items()}}

            return {"operation_id": key, "status": operation.status, "created_at": operation.created_at,
                    "elapsed_ms": round((now - operation.started) * 1000, 3), "stage": stage_payload(operation.stage),
                    "instances": [stage_payload(stage) for stage in operation.instances.values()],
                    "instances_limited": operation.instances_limited, "result": deepcopy(operation.result), "error": operation.error}

    def _stage(self, reporter: _Reporter) -> _Stage | None:
        """在锁内取得仍活跃的阶段, 不接受任务结束后的迟到更新"""
        operation = self._operations.get(reporter.operation_id)
        if operation is None or operation.status != "running" or operation.generation is not reporter.generation:
            return None
        if reporter.instance is None:
            return operation.stage
        stage = operation.instances.get(reporter.instance)
        if stage is None:
            if len(operation.instances) >= self._max_instances:
                operation.instances_limited = True
                return None
            stage = _Stage(*reporter.instance)
            operation.instances[reporter.instance] = stage
        return stage

    @contextmanager
    def phase(self, reporter: _Reporter, name: str) -> Iterator[None]:
        """工作线程和异步任务就地记录阶段, 不在磁盘读取或 await 期间持锁"""
        with self._lock:
            stage = self._stage(reporter)
            previous = (stage.phase, stage.phase_started) if stage is not None else None
            started = time.perf_counter()
            if stage is not None:
                stage.phase, stage.phase_started = ("rollback" if stage.phase == "rollback" else name), started
        try:
            yield
        finally:
            with self._lock:
                if stage is not None and self._stage(reporter) is stage and previous is not None:
                    stage.timings_ms[name] = stage.timings_ms.get(name, 0) + (time.perf_counter() - started) * 1000
                    if stage.status == "running":
                        stage.phase, stage.phase_started = previous
                    else:
                        stage.phase = "finished"

    def finish_instance(self, reporter: _Reporter, result: Mapping[str, Any] | None = None, *, failed: bool = False) -> None:
        """记录实例实际结果, Provider 已封装的失败也必须显示失败"""
        with self._lock:
            stage = self._stage(reporter)
            if stage is not None:
                if result is None and not failed and stage.status != "running":
                    return
                stage.status = "failed" if failed or result is not None and not result.get("ok", False) else "completed"
                stage.phase = "finished"

    def external(self, reporter: _Reporter, target: str, snapshot: Mapping[str, Any]) -> None:
        """
        投影已认证运行服务的实际阶段, 只接收固定观测字段

        参数:
        - reporter: 当前应用任务的观测身份
        - target: 固定服务标识, 用于区分不同服务的同名实例
        - snapshot: 已核对任务 ID 的远程进度快照
        """
        rows = snapshot.get("instances")
        root = snapshot.get("stage")
        if not isinstance(rows, list) or not isinstance(root, Mapping):
            raise ValueError("运行服务进度缺少阶段或实例列表")
        active = next((item for item in rows if isinstance(item, Mapping) and item.get("status") == "running"), root)
        selected = [(replace(reporter, instance=(target, "")), active)]
        for item in rows[:self._max_instances]:
            if not isinstance(item, Mapping) or any(not isinstance(item.get(key), str) for key in ("platform_id", "session_id")):
                raise ValueError("运行服务返回了无效实例进度")
            selected.append((replace(reporter, instance=(f"{target}/{item['platform_id']}", item["session_id"])), item))
        prepared = []
        for current, item in selected:
            phase, status = item.get("phase"), item.get("status")
            elapsed = item.get("phase_elapsed_ms")
            timings = item.get("timings_ms")
            if phase not in {*_PHASES.values(), "queued", "finished"} or status not in {"running", "completed", "failed", "cancelled"}:
                raise ValueError("运行服务返回了未知进度状态")
            if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0 or not isinstance(timings, Mapping):
                raise ValueError("运行服务返回了无效阶段耗时")
            cleaned = {name: float(value) for name, value in timings.items()
                       if name in _PHASES.values() and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0}
            prepared.append((current, phase, status, elapsed, cleaned))
        with self._lock:
            for current, phase, status, elapsed, cleaned in prepared:
                stage = self._stage(current)
                if stage is not None:
                    stage.phase, stage.status = phase, status
                    stage.phase_started = time.perf_counter() - elapsed / 1000
                    stage.timings_ms = cleaned
            operation = self._operations.get(reporter.operation_id)
            if operation is not None and operation.status == "running" and operation.generation is reporter.generation:
                operation.instances_limited |= bool(snapshot.get("instances_limited")) or len(rows) > self._max_instances


@contextmanager
def progress_phase(name: str) -> Iterator[None]:
    """观测已存在的执行区段, 无应用任务时沿原路径执行"""
    reporter = _REPORTER.get()
    phase = _PHASES.get(name)
    if reporter is None or phase is None:
        yield
    else:
        with reporter.registry.phase(reporter, phase):
            yield


@contextmanager
def progress_instance(platform_id: str, session_id: str) -> Iterator[None]:
    """给并发实例绑定独立观测范围, 不传播任何调用者身份或资源快照"""
    reporter = _REPORTER.get()
    if reporter is None:
        yield
        return
    current = replace(reporter, instance=(platform_id, session_id))
    token = _REPORTER.set(current)
    try:
        with progress_phase("apply_ms"):
            yield
        current.registry.finish_instance(current)
    except BaseException:
        current.registry.finish_instance(current, failed=True)
        raise
    finally:
        _REPORTER.reset(token)


def progress_result(result: Mapping[str, Any]) -> None:
    """已封装的实例失败由结果复核, 不仅依赖异常是否抛出"""
    reporter = _REPORTER.get()
    if reporter is not None and reporter.instance is not None:
        reporter.registry.finish_instance(reporter, result)


def progress_external(target: str, snapshot: Mapping[str, Any]) -> None:
    """
    将子服务实际进度并入当前任务, 不复制请求正文或工具调用身份

    参数:
    - target: 已选定的运行服务标识
    - snapshot: 子服务返回的同一任务快照
    """
    reporter = _REPORTER.get()
    if reporter is not None:
        reporter.registry.external(reporter, target, snapshot)


@asynccontextmanager
async def progress_locks(*locks: AbstractAsyncContextManager[Any]) -> AsyncIterator[None]:
    """观测等待原有并发槽位和会话锁, 不改变锁顺序或强制中断持锁请求"""
    async with AsyncExitStack() as stack:
        with progress_phase("queue_and_lock_wait_ms"):
            for lock in locks:
                await stack.enter_async_context(lock)
        yield


async def progress_source_check(check: Callable[[], bool]) -> bool:
    """观测原工作线程中的来源核验, 不共享或缓存核验结果"""
    with progress_phase("source_check_ms"):
        return await asyncio.to_thread(check)
