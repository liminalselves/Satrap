
import asyncio
from pathlib import Path

import pytest

from satrap.core.framework import AsyncSession, Session, SessionManager
from satrap.core.type import UserCall


class EchoSession(Session):
    def __init__(self, session_id: str):
        super().__init__(session_id=session_id)
        self.call_count = 0

    def run(self, query: str) -> str:
        self.call_count += 1
        self.session_ctx.add_user_message(query)
        return f"echo:{query}"


class AsyncEchoSession(AsyncSession):
    def __init__(self, session_id: str):
        super().__init__(session_id=session_id)
        self.call_count = 0

    async def run(self, query: str) -> str:
        self.call_count += 1
        await self.session_ctx.add_user_message(query)
        return f"async-echo:{query}"


class AsyncInitCountingSession(AsyncSession):
    """记录异步初始化次数的测试会话"""

    def __init__(self, session_id: str):
        super().__init__(session_id=session_id)
        self.initialize_count = 0

    async def _async_init(self):
        await asyncio.sleep(0.01)
        self.initialize_count += 1

    async def run(self, query: str) -> str:
        return query


def test_sync_handle_call_with_auto_create_and_reuse():
    manager = SessionManager(default_session_type="echo")
    manager.register_session_type("echo", EchoSession)

    first_call = UserCall(session_id=None, session_type="echo", message="hello")
    first_result = manager.handle_call(first_call)

    assert first_result == "echo:hello"
    assert first_call.session_id is not None

    second_call = UserCall(session_id=first_call.session_id, message="again")
    second_result = manager.handle_call(second_call)

    assert second_result == "echo:again"
    active = manager.pool.list_entries()[first_call.session_id]
    assert isinstance(active.session, EchoSession)
    assert active.session.call_count == 2


@pytest.mark.asyncio
async def test_async_handle_call_with_async_session():
    manager = SessionManager(default_session_type="aecho")
    manager.register_session_type("aecho", AsyncEchoSession)

    call = UserCall(session_id=None, session_type="aecho", message="hello")
    result = await manager.handle_call_async(call)

    assert result == "async-echo:hello"
    assert call.session_id is not None


@pytest.mark.asyncio
async def test_async_session_initializes_once_after_explicit_initialize():
    session = AsyncInitCountingSession("explicit-initialize")

    await session.initialize()
    await session.run("first")
    await session.run("second")

    assert session.initialize_count == 1


@pytest.mark.asyncio
async def test_async_session_initializes_once_for_concurrent_first_calls():
    session = AsyncInitCountingSession("concurrent-initialize")

    results = await asyncio.gather(
        session.run("first"),
        session.run("second"),
        session.run("third"),
    )

    assert results == ["first", "second", "third"]
    assert session.initialize_count == 1


def test_list_sessions_metadata():
    manager = SessionManager(default_session_type="echo")
    manager.register_session_type("echo", EchoSession)
    call = UserCall(session_id=None, session_type="echo", message="meta")
    manager.handle_call(call)

    sessions = manager.list_sessions()
    assert len(sessions) >= 1
    assert sessions[0].session_id == call.session_id
    assert sessions[0].session_type == "echo"
    assert sessions[0].message_count >= 1


def test_session_message_count_includes_unique_workflow_contexts(tmp_path: Path):
    session = EchoSession("message-count")
    db_path = str(tmp_path / "chat_history.db")
    session.session_ctx = type(session.session_ctx)("message-count", db_path=db_path)
    session.session_ctx.add_user_message("session-message")
    workflow_ctx = type(session.session_ctx)("message-count_main", db_path=db_path)
    workflow_ctx.add_user_message("workflow-message")
    session._track_workflow_context("main", workflow_ctx)
    session._track_workflow_context("duplicate", workflow_ctx)

    assert SessionManager._session_message_count(session) == 2


def test_cleanup_idle_sessions():
    manager = SessionManager(default_session_type="echo")
    manager.register_session_type("echo", EchoSession)
    call = UserCall(session_id=None, session_type="echo", message="idle")
    manager.handle_call(call)

    assert call.session_id is not None
    entry = manager.pool.list_entries()[call.session_id]
    entry.last_used = 0.0

    manager.cleanup_idle_sessions(max_idle_seconds=1)
    assert call.session_id not in manager.pool.list_entries()
