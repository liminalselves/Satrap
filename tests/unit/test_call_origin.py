"""验证逐次来源隔离, 清理及工具参数不能覆盖身份"""
from dataclasses import FrozenInstanceError
from typing import cast
import asyncio

import pytest

from satrap.core.framework.Base import AsyncSession, Session as FrameworkSession
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.utils.async_worker import BoundedAsyncWorker
from satrap.core.utils.TCBuilder.async_tool import AsyncTool
from satrap.core.call_context import CallOrigin, bind_call_origin, current_call_origin, require_call_origin
from satrap.core.type import UserCall


def origin(actor: str) -> CallOrigin:
    """构造同一共享群内不同成员的来源"""
    return CallOrigin("bot", "10", "group_message", "20", actor, "message", f"request-{actor}")


class IdentityTool(AsyncTool):
    """模拟接受模型传入主体字段的平台工具"""

    tool_name = "identity_probe"
    description = "读取本轮来源"
    params_dict = {"actor_id": ("string", "模型提供的普通参数")}

    async def execute(self, **kwargs):
        await asyncio.sleep(0)
        return require_call_origin().actor_id


@pytest.mark.asyncio
async def test_concurrent_shared_session_calls_keep_actor_and_ignore_model_identity():
    tool = IdentityTool()

    class SharedSession:
        async def run(self, message):
            return await tool(actor_id="forged-admin", origin={"actor_id": "forged-admin"})

    # 鸭子类型替身: 只实现 run, 不经框架基类构造
    session = cast(AsyncSession, SharedSession())
    results = await asyncio.gather(*[
        SessionManager._invoke_async_session(session, UserCall(message="hello", origin=origin(actor)))
        for actor in ["admin", "member"]
    ])
    assert results == ["admin", "member"]
    assert current_call_origin() is None
    with pytest.raises(PermissionError):
        await tool(actor_id="admin")


@pytest.mark.asyncio
async def test_background_task_cannot_reuse_identity_after_turn_finishes():
    release = asyncio.Event()
    tasks = []

    async def delayed():
        await release.wait()
        return current_call_origin()

    class Session:
        async def run(self, message):
            tasks.append(asyncio.create_task(delayed()))
            return "done"

    await SessionManager._invoke_async_session(cast(AsyncSession, Session()), UserCall(message="hello", origin=origin("admin")))
    release.set()
    assert await tasks[0] is None


@pytest.mark.asyncio
async def test_cancellation_revokes_child_context():
    started = asyncio.Event()
    release = asyncio.Event()
    children = []

    async def child():
        await release.wait()
        return current_call_origin()

    class Session:
        async def run(self, message):
            children.append(asyncio.create_task(child()))
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(SessionManager._invoke_async_session(cast(AsyncSession, Session()), UserCall(origin=origin("admin"))))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await children[0] is None


def test_sync_entry_masks_outer_identity_and_resets_after_failure():
    class Session:
        def run(self, message):
            assert current_call_origin() is None
            raise RuntimeError("failed")

    actor = origin("admin")
    with pytest.raises(FrozenInstanceError):
        setattr(actor, "actor_id", "other")
    with bind_call_origin(actor):
        assert SessionManager._invoke_sync_session(cast(FrameworkSession, Session()), UserCall(message="hello")) == ""
        assert current_call_origin() == actor
    assert current_call_origin() is None


@pytest.mark.asyncio
async def test_sync_worker_keeps_per_call_identity_without_persisting_it():
    worker = BoundedAsyncWorker("identity-test", workers=2, capacity=2)

    class Session:
        def run(self, message):
            return require_call_origin().actor_id

    try:
        results = await asyncio.gather(*[
            worker.run(SessionManager._invoke_sync_session, Session(), UserCall(message="hello", origin=origin(actor)))
            for actor in ["admin", "member"]
        ])
        assert results == ["admin", "member"]
        assert await worker.run(current_call_origin) is None
    finally:
        worker.close()
