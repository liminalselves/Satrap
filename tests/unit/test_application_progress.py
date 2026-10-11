"""应用任务隔离, 真实阶段, 幂等接受与有界保留契约"""
from __future__ import annotations

import asyncio
from contextvars import copy_context
import threading
from unittest.mock import Mock

import pytest

from satrap.core.utils.application_progress import (
    ApplicationOperations, ApplicationProgressError, progress_instance, progress_locks, progress_phase, progress_result,
)
from satrap.core.utils.performance import PhaseTimings


async def _finished(registry: ApplicationOperations, key: str) -> dict:
    """只读等候已登记的任务, 不提交第二次动作"""
    for _attempt in range(100):
        result = registry.get(key)
        if result["status"] != "running":
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("已启动任务未结束")


@pytest.mark.asyncio
async def test_thread_phase_same_id_dedup_conflict_and_result_copy() -> None:
    """工作线程实际读取期间能查询, 同 ID 在途/终态均只执行一次, 正文不暴露"""
    registry = ApplicationOperations()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def read():
        with PhaseTimings().phase("config_read_ms"):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("未恢复测试工作线程")

    async def apply():
        calls.append(True)
        await asyncio.to_thread(read)
        return {"ok": True, "sessions": [{"ok": True}]}

    key = "thread-operation-001"
    try:
        registry.submit(key, {"secret": "不得在进度中出现"}, apply)
        assert await asyncio.to_thread(entered.wait, 1)
        result = registry.get(key)
        assert result["stage"]["phase"] == "config_read" and "不得在进度中出现" not in str(result)
        assert registry.submit(key, {"secret": "不得在进度中出现"}, apply)["status"] == "running"
        with pytest.raises(ApplicationProgressError) as conflict:
            registry.submit(key, {"secret": "不同的值"}, apply)
        assert conflict.value.status == 409
    finally:
        release.set()
    result = await _finished(registry, key)
    assert result["status"] == "completed" and result["stage"]["timings_ms"]["config_read"] > 0
    result["result"]["sessions"][0]["ok"] = False
    assert registry.get(key)["result"]["sessions"][0]["ok"]
    assert registry.submit(key, {"secret": "不得在进度中出现"}, apply)["status"] == "completed" and calls == [True]


@pytest.mark.asyncio
async def test_concurrent_instances_and_wait_for_original_lock() -> None:
    """原会话锁被占用时仅该实例等候, 另一个实例显示实际安装, 封装失败不误报完成"""
    registry = ApplicationOperations()
    lock = asyncio.Lock()
    await lock.acquire()
    semaphore = asyncio.Semaphore(2)
    installing, installed = asyncio.Event(), asyncio.Event()

    async def one():
        with progress_instance("one", "locked"):
            async with progress_locks(semaphore, lock):
                progress_result({"ok": False})
                return {"ok": False}

    async def two():
        with progress_instance("two", "installing"):
            with PhaseTimings().phase("install_ms"):
                installing.set()
                await installed.wait()
            progress_result({"ok": True})
            return {"ok": True}

    async def apply():
        rows = await asyncio.gather(one(), two())
        return {"ok": False, "sessions": rows}

    key = "instances-operation-001"
    registry.submit(key, {}, apply)
    try:
        await asyncio.wait_for(installing.wait(), 1)
        rows = registry.get(key)["instances"]
        assert [(row["session_id"], row["phase"]) for row in rows] == [("locked", "waiting_lock"), ("installing", "install")]
        installed.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        lock.release()
        result = await _finished(registry, key)
        assert result["status"] == "failed" and result["result"]["sessions"] == [{"ok": False}, {"ok": True}]
        assert [row["status"] for row in result["instances"]] == ["failed", "completed"]
    finally:
        installed.set()
        if lock.locked():
            lock.release()


@pytest.mark.asyncio
async def test_closed_context_cannot_update_completed_or_another_operation() -> None:
    """复制的观测上下文终结后失效, 不改变下一次任务的阶段"""
    registry = ApplicationOperations()
    contexts = []

    async def apply():
        contexts.append(copy_context())
        return {"ok": True}

    key = "closed-operation-001"
    registry.submit(key, {}, apply)
    before = await _finished(registry, key)

    def late_update():
        with progress_instance("late", "never"):
            with progress_phase("install_ms"):
                pass

    contexts[0].run(late_update)
    assert registry.get(key) == before
    assert registry.get(key)["instances"] == []


@pytest.mark.asyncio
async def test_reused_id_cannot_receive_old_generation_progress() -> None:
    """终态被淘汰后即使显式复用 ID, 原观测上下文也不能修改新任务"""
    registry = ApplicationOperations(max_completed=1)
    old_contexts = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def completed():
        old_contexts.append(copy_context())
        return {"ok": True}

    key = "reused-operation-id-001"
    registry.submit(key, {}, completed)
    await _finished(registry, key)
    registry.submit("evict-old-operation-002", {}, completed)
    await _finished(registry, "evict-old-operation-002")

    async def active():
        entered.set()
        await release.wait()
        return {"ok": True}

    registry.submit(key, {}, active)
    await asyncio.wait_for(entered.wait(), 1)

    def old_update():
        with progress_instance("obsolete", "must-not-exist"), progress_phase("install_ms"):
            pass

    old_contexts[0].run(old_update)
    assert registry.get(key)["instances"] == [] and registry.get(key)["stage"]["phase"] == "applying"
    release.set()
    assert (await _finished(registry, key))["status"] == "completed"


@pytest.mark.asyncio
async def test_error_stack_and_nested_rollback_phase(monkeypatch) -> None:
    """回滚过程中嵌套安装仍显示回滚, 未捕获的动作失败写完整堆栈"""
    registry = ApplicationOperations()
    entered, release = asyncio.Event(), asyncio.Event()
    logger = Mock()
    monkeypatch.setattr("satrap.core.utils.application_progress.logger", logger)

    async def apply():
        with progress_instance("one", "failed"):
            with progress_phase("rollback_ms"), PhaseTimings().phase("install_ms"):
                entered.set()
                await release.wait()
            raise RuntimeError("回滚失败")

    key = "rollback-operation-001"
    registry.submit(key, {}, apply)
    await asyncio.wait_for(entered.wait(), 1)
    assert registry.get(key)["instances"][0]["phase"] == "rollback"
    release.set()
    result = await _finished(registry, key)
    assert result["status"] == "failed" and result["error"] == "回滚失败"
    assert result["instances"][0]["status"] == "failed"
    assert "Traceback" in logger.error.call_args.args[0]


@pytest.mark.asyncio
async def test_capacity_retention_and_missing_status_do_not_launch_action(monkeypatch) -> None:
    """活跃任务不被淘汰, 已终结记录有界且超期只返回未知, 不自动重放"""
    registry = ApplicationOperations(max_active=1, max_completed=1, ttl=10)
    release = asyncio.Event()

    async def apply():
        await release.wait()
        return {"ok": True}

    registry.submit("capacity-operation-001", {}, apply)
    with pytest.raises(ApplicationProgressError) as busy:
        registry.submit("capacity-operation-002", {}, apply)
    assert busy.value.status == 503
    release.set()
    await _finished(registry, "capacity-operation-001")
    registry.submit("capacity-operation-002", {}, apply)
    await _finished(registry, "capacity-operation-002")
    with pytest.raises(ApplicationProgressError) as missing:
        registry.get("capacity-operation-001")
    assert missing.value.status == 404
    now = registry._operations["capacity-operation-002"].finished
    if now is None:
        raise AssertionError("测试任务未终结")
    monkeypatch.setattr("satrap.core.utils.application_progress.time.perf_counter", lambda: now + 11)
    with pytest.raises(ApplicationProgressError):
        registry.get("capacity-operation-002")


@pytest.mark.asyncio
async def test_instance_limit_is_explicit_and_does_not_limit_execution() -> None:
    """观测记录上限只影响进度明细, 不丢应用对象或结果"""
    registry = ApplicationOperations(max_instances=1)

    async def apply():
        for sid in ("one", "two"):
            with progress_instance("platform", sid):
                progress_result({"ok": True})
        return {"ok": True, "sessions": ["one", "two"]}

    registry.submit("limited-operation-001", {}, apply)
    result = await _finished(registry, "limited-operation-001")
    assert result["instances_limited"] and len(result["instances"]) == 1
    assert result["result"]["sessions"] == ["one", "two"]


@pytest.mark.asyncio
async def test_service_cancellation_is_not_success() -> None:
    """任务取消被标为未确认, 不生成伪造成功结果"""
    registry = ApplicationOperations()
    entered = asyncio.Event()

    async def apply():
        entered.set()
        await asyncio.Event().wait()
        return {"ok": True}

    key = "cancelled-operation-001"
    registry.submit(key, {}, apply)
    await asyncio.wait_for(entered.wait(), 1)
    task = registry._operations[key].task
    if task is None:
        raise AssertionError("测试任务未启动")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = registry.get(key)
    assert result["status"] == "cancelled" and result["result"] is None


@pytest.mark.parametrize("operation_id", [None, "short", "../" * 20, "x" * 129, "测试" * 20])
def test_invalid_id_is_rejected_before_execution(operation_id) -> None:
    registry = ApplicationOperations()
    with pytest.raises(ApplicationProgressError) as invalid:
        registry.get(operation_id)
    assert invalid.value.status == 400


@pytest.mark.asyncio
async def test_cancellation_before_task_starts_releases_capacity() -> None:
    """立即取消尚未调度的任务也不会永久占据活跃槽位"""
    registry = ApplicationOperations(max_active=1)
    called = []

    async def apply():
        called.append(True)
        return {"ok": True}

    key = "early-cancel-operation-001"
    registry.submit(key, {}, apply)
    task = registry._operations[key].task
    if task is None:
        raise AssertionError("任务未登记")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert registry.get(key)["status"] == "cancelled" and not called
    registry.submit("early-cancel-operation-002", {}, apply)
    assert (await _finished(registry, "early-cancel-operation-002"))["status"] == "completed"
