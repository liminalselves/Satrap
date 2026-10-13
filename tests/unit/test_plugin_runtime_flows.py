"""业务链路场景与集中边界契约"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
import asyncio
import pytest
from satrap.expend.plugins.memory import runtime as memory_runtime
from satrap.expend.plugins.group_chat import tools as group_chat_tools
from satrap.expend.plugins.friend_manager import tools as friend_tools
from satrap.core.framework.Base import Session
from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.core.platform import set_current_adapter_manager
from collections.abc import Iterator
import threading
from satrap.core.platform.loop_bridge import PlatformLoopUnavailable, run_on_platform_loop
from unittest.mock import AsyncMock, Mock
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.utils.async_worker import BoundedAsyncWorker, WorkerBusyError
from satrap.core.utils import outbound
from satrap.core.type import UserCall

def _origin(kind: str) -> CallOrigin:
    chat_type = "GroupMessage" if kind == "group" else "FriendMessage"
    return CallOrigin("ob", "10000", chat_type, "456", "123", "77", "r1", conversation_kind=kind)

class _ManagerStub:
    """固定来源适配器的管理器替身"""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def get_adapter(self, adapter_id: str) -> Any:
        return self._adapter

@pytest.fixture()
def _clear_manager() -> Any:
    yield
    set_current_adapter_manager(None)

def _group_chat_tool(loop: Any) -> Any:
    set_current_adapter_manager(cast(Any, _ManagerStub(SimpleNamespace(_loop=loop))))
    return next(t for t in group_chat_tools.get_tools(cast(Session, object()), {}) if t.tool_name == "group_chat_get_message")

def _never() -> Any:
    async def never() -> None:
        raise AssertionError("协程不应被提交")

    return never()

def _timeout(coroutine: Any, loop: Any, seconds: float) -> Any:
    coroutine.close()
    raise TimeoutError

@pytest.mark.usefixtures('_clear_manager')
def test_group_chat_without_loop_reports_unavailable() -> None:
    tool = _group_chat_tool(None)
    with bind_call_origin(_origin("group")):
        result = tool.execute(message_id="1")
    assert result == {"ok": False, "error": {"code": "unavailable", "message": "来源事件循环不可用", "retryable": True}}

@pytest.mark.usefixtures('_clear_manager')
@pytest.mark.asyncio
async def test_group_chat_inside_platform_loop_is_rejected() -> None:
    tool = _group_chat_tool(asyncio.get_running_loop())
    with bind_call_origin(_origin("group")):
        result = tool.execute(message_id="1")
    assert result == {"ok": False, "error": {
        "code": "unavailable", "message": "同步工具不能在平台事件循环内阻塞调用", "retryable": True}}

@pytest.mark.usefixtures('_clear_manager')
def test_group_chat_timeout_reports_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _group_chat_tool(None)
    monkeypatch.setattr(group_chat_tools, "run_on_platform_loop", _timeout)
    with bind_call_origin(_origin("group")):
        result = tool.execute(message_id="1")
    assert result == {"ok": False, "error": {"code": "unavailable", "message": "群聊宿主调用超时, 已取消", "retryable": True}}

def _friend_tool(monkeypatch: pytest.MonkeyPatch) -> Any:
    adapter = SimpleNamespace(_loop=None)
    origin = _origin("private")
    tool = next(t for t in friend_tools.get_tools(cast(Session, object()), {}) if t.tool_name == "friend_manager_list_friends")
    monkeypatch.setattr(tool, "_resolve", lambda preview=False, recheck_entry=True: (adapter, origin))
    return tool

@pytest.mark.usefixtures('_clear_manager')
def test_friend_manager_without_loop_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _friend_tool(monkeypatch)
    with bind_call_origin(_origin("private")):
        result = tool.execute()
    assert result == {"ok": False, "error": {"code": "unavailable", "message": "来源平台事件循环不可用于同步工具", "retryable": False}}

@pytest.mark.usefixtures('_clear_manager')
def test_friend_manager_timeout_keeps_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _friend_tool(monkeypatch)
    monkeypatch.setattr(friend_tools, "run_on_platform_loop", _timeout)
    with bind_call_origin(_origin("private")):
        result = tool.execute()
    assert result == {"ok": False, "error": {"code": "unconfirmed", "message": "平台未确认结果, 请核查, 不要重复执行", "retryable": True}}

@pytest.mark.usefixtures('_clear_manager')
def test_memory_without_loop_reports_unavailable_and_closes_coroutine() -> None:
    set_current_adapter_manager(None)
    coroutine = _never()
    result = memory_runtime.wait_host(coroutine)
    assert result == {"ok": False, "error": "同步记忆操作需要有效的平台工作线程", "error_type": "unavailable"}
    assert coroutine.cr_frame is None

@pytest.mark.usefixtures('_clear_manager')
def test_memory_timeout_reports_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_runtime, "run_on_platform_loop", _timeout)
    result = memory_runtime.wait_host(_never())
    assert result == {"ok": False, "error": "记忆操作超时, 已取消等待", "error_type": "unavailable"}

class _LoopThread:
    """在后台线程运行事件循环, 模拟来源平台工作线程"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()

@pytest.fixture
def loop_thread() -> Iterator[_LoopThread]:
    thread = _LoopThread()
    yield thread
    thread.close()

def test_running_loop_returns_coroutine_result(loop_thread: _LoopThread) -> None:
    async def answer() -> str:
        return "ok"

    assert run_on_platform_loop(answer(), loop_thread.loop, 5) == "ok"

def test_coroutine_exception_propagates_unchanged(loop_thread: _LoopThread) -> None:
    async def boom() -> None:
        raise ValueError("宿主拒绝")

    with pytest.raises(ValueError, match="宿主拒绝"):
        run_on_platform_loop(boom(), loop_thread.loop, 5)

def test_timeout_cancels_coroutine_in_platform_loop(loop_thread: _LoopThread) -> None:
    cancelled = threading.Event()

    async def slow() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    with pytest.raises(TimeoutError):
        run_on_platform_loop(slow(), loop_thread.loop, 0.05)
    assert cancelled.wait(5)

def test_missing_loop_closes_coroutine() -> None:
    async def never() -> None:
        raise AssertionError("协程不应被提交")

    coroutine = never()
    with pytest.raises(PlatformLoopUnavailable) as info:
        run_on_platform_loop(coroutine, None, 5)
    assert info.value.reason == "missing"
    assert str(info.value) == "来源平台事件循环不可用"
    assert coroutine.cr_frame is None

def test_closed_loop_closes_coroutine() -> None:
    loop = asyncio.new_event_loop()
    loop.close()

    async def never() -> None:
        raise AssertionError("协程不应被提交")

    coroutine = never()
    with pytest.raises(PlatformLoopUnavailable) as info:
        run_on_platform_loop(coroutine, loop, 5)
    assert info.value.reason == "missing"
    assert coroutine.cr_frame is None

def test_not_running_loop_is_rejected() -> None:
    loop = asyncio.new_event_loop()

    async def never() -> None:
        raise AssertionError("协程不应被提交")

    coroutine = never()
    try:
        with pytest.raises(PlatformLoopUnavailable) as info:
            run_on_platform_loop(coroutine, loop, 5)
        assert info.value.reason == "missing"
        assert coroutine.cr_frame is None
    finally:
        loop.close()

@pytest.mark.asyncio
async def test_same_loop_is_rejected_before_blocking() -> None:
    submitted: list[Any] = []

    async def never() -> None:
        submitted.append(object())

    coroutine = never()
    with pytest.raises(PlatformLoopUnavailable) as info:
        run_on_platform_loop(coroutine, asyncio.get_running_loop(), 5)
    assert info.value.reason == "same_loop"
    assert str(info.value) == "同步工具不能在平台事件循环内阻塞调用"
    assert coroutine.cr_frame is None
    await asyncio.sleep(0)
    assert submitted == []

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
    manager.pool = Mock(list_entries=lambda: {"audit": entry}, release=lambda value: released.append(value))
    manager._resolve_or_create_session_config = Mock(return_value=SimpleNamespace(session_id="audit"))
    manager._acquire_or_create_entry_async = AsyncMock(return_value=entry)
    manager._prepare_session_async = AsyncMock()
    manager._sync_runtime_to_store = Mock(return_value=None)
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
