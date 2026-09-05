"""同步任务取消, 队列背压和 DNS 截止时间回归"""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from satrap.core.framework.SessionManager import SessionManager
from satrap.core.type import UserCall
from satrap.core.utils import outbound
from satrap.core.utils.async_worker import BoundedAsyncWorker, WorkerBusyError


async def wait_thread_event(event):
    async def poll():
        while not event.is_set():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 2)


async def test_cancel_keeps_session_until_sync_worker_finishes():
    entered, release = threading.Event(), threading.Event()
    released = []
    calls = []

    def run(message):
        calls.append(message)
        if message == "first":
            entered.set()
            release.wait(3)
        return message

    entry = SimpleNamespace(session=SimpleNamespace(run=run), async_operation_lock=asyncio.Lock(), sync_operation_lock=threading.RLock())
    manager = SessionManager.__new__(SessionManager)
    manager.pool = SimpleNamespace(list_entries=lambda: {"audit": entry}, release=lambda value: released.append(value))
    manager._resolve_or_create_session_config = lambda call: SimpleNamespace(session_id="audit")
    manager._acquire_or_create_entry_async = AsyncMock(return_value=entry)
    manager._prepare_session_async = AsyncMock()
    manager._sync_runtime_to_store = lambda *args: None
    manager.cleanup_idle_sessions_async = AsyncMock()
    first = asyncio.create_task(manager.handle_call_async(UserCall(session_id="audit", message="first")))
    second = None
    try:
        await wait_thread_event(entered)
        first.cancel()
        second = asyncio.create_task(manager.handle_call_async(UserCall(session_id="audit", message="second")))
        await asyncio.sleep(0.02)
        assert not first.done() and not second.done()
        assert not released and calls == ["first"]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await second == "second"
        assert len(released) == 2
    finally:
        release.set()
        await asyncio.gather(*[task for task in (first, second) if task], return_exceptions=True)


async def test_worker_bounds_queue_after_caller_abandons_result():
    worker = BoundedAsyncWorker("audit-worker", workers=1, capacity=1)
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        release.wait(3)
        return "late"

    task = asyncio.create_task(worker.run(block, wait_on_cancel=False))
    try:
        await wait_thread_event(entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(WorkerBusyError):
            await worker.run(lambda: "must not queue")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(worker.close)


@pytest.mark.parametrize("redirect", [False, True])
async def test_dns_deadline_discards_late_results(monkeypatch, redirect):
    worker = BoundedAsyncWorker("audit-dns", workers=1, capacity=2)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def resolve(url, **kwargs):
        if not redirect or url.endswith("second"):
            entered.set()
            release.wait(3)
        return SimpleNamespace(url=url)

    async def request(target, **kwargs):
        calls.append(target.url)
        await asyncio.sleep(0.02)
        return outbound.OutboundHTTPResponse(target.url, 302, {"Location": "/second"}, b"")

    monkeypatch.setattr(outbound, "DNS_WORKERS", worker)
    monkeypatch.setattr(outbound, "resolve_outbound_http_url", resolve)
    monkeypatch.setattr(outbound, "resolve_outbound_redirect", lambda url, location, **kwargs: resolve("https://audit.test/second"))
    monkeypatch.setattr(outbound, "_async_request_once", request)
    task = asyncio.create_task(outbound.safe_async_get("https://audit.test/first", timeout=0.1))
    try:
        await wait_thread_event(entered)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, 1)
        assert not release.is_set()
        assert calls == (["https://audit.test/first"] if redirect else [])
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(worker.close)
