from __future__ import annotations

import asyncio
from pathlib import Path
import pytest
from typing import Any, cast

from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.framework.UserManager import UserManager
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.framework.Base import Session
from satrap.core.platform.event import MessageEvent, PlatformMetadata
from satrap.cli.cmd_session import _configured_adapter_ids
from satrap.core.platform import (
    EventDispatcher,
    PlatformAdapter,
    PlatformAdapterManager,
    PlatformAdapterRegistry,
    PlatformConfig,
)
from satrap.core.type import MessageMember, PlatformMessage, PlatformMessageType


class _EchoSession(Session):
    """测试用同步会话"""

    def run(self, message: str) -> str:
        return message


class _DummyAdapter(PlatformAdapter):
    adapter_type = "dummy"

    async def run(self) -> None:
        return None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(name=self.config.id, id=self.config.id)


class _CleanupAdapter(_DummyAdapter):
    """记录 terminate 是否被调用的适配器"""

    terminated = False

    async def terminate(self) -> None:
        """记录客户端清理并执行基类终止流程"""
        self.terminated = True
        await super().terminate()


def _session_class_mgr(tmp_path: Path, params: dict[str, Any] | None = None) -> SessionClassConfigManager:
    mgr = SessionClassConfigManager(storage_path=tmp_path / "session_classes.json")
    mgr.register("dummy", _EchoSession)
    if params:
        mgr.set_config("dummy", params)
    return mgr


def _session_manager(tmp_path: Path, scm: SessionClassConfigManager) -> SessionManager:
    sm = SessionManager(default_session_type="dummy", db_path=tmp_path / "sessions.db")
    sm.register_session_type("dummy", _EchoSession)
    sm.class_cfg_mgr = scm
    return sm


def _message_event(adapter_id: str = "misskey1") -> MessageEvent:
    message = PlatformMessage()
    message.type = PlatformMessageType.FRIEND_MESSAGE
    message.self_id = "bot"
    message.session_id = "chat%user-1"
    message.message_id = "msg-1"
    message.sender = MessageMember(user_id="user-1", nickname="User")
    message.sender.user_id = "user-1"
    message.sender.nickname = "User"
    message.message = []
    message.message_str = "hello"
    message.raw_message = {}
    event = MessageEvent(
        message_str="hello",
        platform_message=message,
        platform_meta=PlatformMetadata(name=adapter_id, id=adapter_id),
        session_id=message.session_id,
        adapter=None,
        session_type="dummy",
    )
    return event


def test_same_adapter_type_can_register_multiple_instances():
    """同一 adapter type 可以注册多个不同实例 ID"""
    registry = PlatformAdapterRegistry()
    registry.register("dummy", _DummyAdapter)
    mgr = PlatformAdapterManager(registry=registry)

    first = mgr.add_adapter(PlatformConfig(id="dummy1", type="dummy"))
    second = mgr.add_adapter(PlatformConfig(id="dummy2", type="dummy"))

    assert first is not None
    assert second is not None
    assert mgr.list_adapters() == ["dummy1", "dummy2"]


@pytest.mark.asyncio
async def test_event_dispatcher_processes_platforms_concurrently():
    """一个平台的慢事件不会阻塞另一个平台, 同平台仍由单工作器保序"""
    registry = PlatformAdapterRegistry()
    registry.register("dummy", _DummyAdapter)
    manager = PlatformAdapterManager(registry=registry)
    first = manager.add_adapter(PlatformConfig(id="first", type="dummy"))
    second = manager.add_adapter(PlatformConfig(id="second", type="dummy"))
    assert first is not None and second is not None

    first_started = asyncio.Event()
    second_done = asyncio.Event()
    release_first = asyncio.Event()

    class _Scheduler:
        async def execute(self, event: MessageEvent) -> None:
            """
            按平台标识控制测试事件时序

            参数:
            - event: 待调度平台事件
            """
            if event.platform_meta.id == "first":
                first_started.set()
                await release_first.wait()
            else:
                second_done.set()

    dispatcher = EventDispatcher(manager, cast(PipelineScheduler, _Scheduler()))
    task = asyncio.create_task(dispatcher.dispatch_loop())
    await first._event_queue.put(_message_event("first"))
    await first_started.wait()
    await second._event_queue.put(_message_event("second"))
    await asyncio.wait_for(second_done.wait(), timeout=1)
    release_first.set()
    await first._event_queue.join()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_manager_stop_all_terminates_adapter_resources():
    """管理器停止平台时必须执行完整的客户端清理"""
    registry = PlatformAdapterRegistry()
    registry.register("cleanup", _CleanupAdapter)
    manager = PlatformAdapterManager(registry=registry)
    adapter = manager.add_adapter(PlatformConfig(id="cleanup1", type="cleanup"))
    assert isinstance(adapter, _CleanupAdapter)

    await manager.start_all()
    await manager.stop_all()

    assert adapter.terminated is True
    assert adapter.started is False


def test_user_manager_routes_same_user_to_different_adapter_sessions(tmp_path: Path):
    """
    同一用户在不同 adapter_id 下应拥有不同上下文

    参数:
    - tmp_path: tmp路径
    """
    scm = _session_class_mgr(tmp_path)
    sm = _session_manager(tmp_path, scm)
    um = UserManager(sm, db_path=tmp_path / "users.db")

    first = um.resolve_session("user-1", "misskey1", "dummy", scm)
    second = um.resolve_session("user-1", "misskey2", "dummy", scm)

    assert first.startswith("dummy:misskey1:user-1:")
    assert second.startswith("dummy:misskey2:user-1:")
    assert first != second


def test_pipeline_uses_source_adapter_binding(tmp_path: Path):
    """
    类级 adapter_id 不应覆盖产生事件的适配器实例

    参数:
    - tmp_path: tmp路径
    """
    scm = _session_class_mgr(tmp_path, {"adapter_id": "misskey2"})
    sm = _session_manager(tmp_path, scm)
    scheduler = PipelineScheduler(sm)

    platform_id, extra = scheduler._resolve_route_adapter(_message_event("misskey1"))

    assert platform_id == "misskey1"
    assert extra == {"adapter_id": "misskey1"}


def test_pipeline_records_source_adapter_in_session_params(tmp_path: Path):
    """
    自动创建会话时应把事件来源适配器写入实例参数

    参数:
    - tmp_path: tmp路径
    """
    scm = _session_class_mgr(tmp_path, {"adapter_id": "missing"})
    sm = _session_manager(tmp_path, scm)
    scheduler = PipelineScheduler(sm)

    platform_id, extra = scheduler._resolve_route_adapter(_message_event("misskey1"))

    assert platform_id == "misskey1"
    assert extra == {"adapter_id": "misskey1"}


def test_backend_resolves_explicit_same_name_and_default_session_types(tmp_path: Path):
    """
    平台会话类应按显式配置, 同名兼容和全局默认的顺序解析

    参数:
    - tmp_path: 临时目录
    """
    backend = BackendManager(BackendConfig(default_session_type="fallback"))
    manager = SessionClassConfigManager(storage_path=tmp_path / "session_classes.json")
    manager.register("misskey", _EchoSession)
    backend._session_cls_cfg = manager

    assert backend._resolve_platform_session_type("misskey", "assistant") == "assistant"
    assert backend._resolve_platform_session_type("misskey", "") == "misskey"
    assert backend._resolve_platform_session_type("onebot", "") == "fallback"


@pytest.mark.asyncio
async def test_backend_awaits_platform_start_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """平台初始化应在当前事件循环中等待 start_all 且只调用一次"""
    calls = 0

    async def start_all(_manager: PlatformAdapterManager) -> None:
        """
        参数:
        - _manager: 平台适配器管理器
        """
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)

    monkeypatch.setattr(PlatformAdapterManager, "start_all", start_all)
    backend = BackendManager(BackendConfig(platforms=[]))

    try:
        await backend._init_platforms()

        assert calls == 1
    finally:
        # _init_platforms 登记进程级通知 hub, 必须 stop 清理避免泄漏到其他用例
        await backend.stop()


@pytest.mark.asyncio
async def test_backend_propagates_platform_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台启动异常应直接传递给后端启动流程"""

    async def start_all(_manager: PlatformAdapterManager) -> None:
        """
        参数:
        - _manager: 平台适配器管理器
        """
        raise RuntimeError("platform start failed")

    monkeypatch.setattr(PlatformAdapterManager, "start_all", start_all)
    backend = BackendManager(BackendConfig(platforms=[]))

    try:
        with pytest.raises(RuntimeError, match="platform start failed"):
            await backend._init_platforms()
    finally:
        # 启动失败前 hub 已登记, 由 stop 负责清除
        await backend.stop()


def test_configured_adapter_ids_reads_backend_config_platforms():
    """CLI 创建会话时可从配置中读取 adapter_id 候选"""
    config = BackendConfig(
        platforms=[
            {"id": "misskey1", "type": "misskey", "settings": {}},
            {"id": "misskey2", "type": "misskey", "settings": {}},
        ],
    )

    assert _configured_adapter_ids(config) == {"misskey1", "misskey2"}


def test_backend_isolates_same_session_id_between_platform_databases(tmp_path: Path):
    """
    两个平台可拥有同名会话, 其数据库和私有目录必须完全隔离

    参数:
    - tmp_path: 临时目录
    """
    backend = BackendManager(
        BackendConfig(
            data_root=str(tmp_path / "data"),
            model_config_path=str(tmp_path / "models.json"),
            session_class_config_path=str(tmp_path / "session-classes.json"),
            edictum_config_path=str(tmp_path / "edictum.json"),
        )
    )
    backend._init_model_config()
    backend._init_session_class_config()
    backend._init_edictum_config()
    first_manager, _ = backend._ensure_platform_runtime("onebot-main")
    second_manager, _ = backend._ensure_platform_runtime("misskey-main")

    first_manager.register_session(_EchoSession, "echo", session_id="same-id")
    second_manager.register_session(_EchoSession, "echo", session_id="same-id")

    assert first_manager.store.db_path != second_manager.store.db_path
    assert first_manager.store.get("same-id") is not None
    assert second_manager.store.get("same-id") is not None
    first_root = backend._storage.session_root("onebot-main", "same-id")
    second_root = backend._storage.session_root("misskey-main", "same-id")
    assert not first_root.exists() and not second_root.exists()
    assert first_root != second_root
    (first_root / "uploads").mkdir(parents=True)
    (first_root / "uploads" / "document.txt").write_text("私有文档", encoding="utf-8")
    assert not second_root.exists()


@pytest.mark.asyncio
async def test_dispatcher_same_platform_parallel_groups_and_order():
    registry = PlatformAdapterRegistry()
    registry.register("dummy", _DummyAdapter)
    manager = PlatformAdapterManager(registry=registry)
    adapter = manager.add_adapter(PlatformConfig(id="bot", type="dummy", settings={"event_concurrency": 2}))
    assert adapter is not None
    entered = asyncio.Event()
    other_done = asyncio.Event()
    release = asyncio.Event()
    calls = []

    class Scheduler:
        async def execute(self, event):
            calls.append(event.message_str)
            if event.message_str == "a1":
                entered.set()
                await release.wait()
            if event.message_str == "b1":
                other_done.set()

    dispatcher = EventDispatcher(manager, cast(PipelineScheduler, Scheduler()))
    task = asyncio.create_task(dispatcher.dispatch_loop())
    try:
        for group, text in [("a", "a1"), ("a", "a2"), ("b", "b1")]:
            event = _message_event("bot")
            event.session_id = group
            event.message_str = text
            adapter.commit_event(event)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(other_done.wait(), 1)
        assert calls == ["a1", "b1"]
        release.set()
        await asyncio.wait_for(adapter._event_queue.join(), 1)
        assert calls == ["a1", "b1", "a2"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_dispatcher_expiry_overload_and_cleanup(tmp_path):
    registry = PlatformAdapterRegistry()
    registry.register("dummy", _DummyAdapter)
    manager = PlatformAdapterManager(registry=registry)
    adapter = manager.add_adapter(PlatformConfig(id="bot", type="dummy", settings={"event_queue_capacity": 1, "event_queue_ttl": 1}))
    assert adapter is not None
    expired = _message_event("bot")
    adapter.commit_event(expired)
    expired.queued_at -= 10
    dropped = _message_event("bot")
    asset = tmp_path / "pending.txt"
    asset.write_text("temporary", encoding="utf-8")
    dropped.track_temporary_local_file(str(asset))
    adapter.commit_event(dropped)
    assert adapter.dropped_events == 1
    assert not asset.exists()
    from unittest.mock import AsyncMock
    scheduler = AsyncMock()
    dispatcher = EventDispatcher(manager, scheduler)
    task = asyncio.create_task(dispatcher.dispatch_loop())
    try:
        await asyncio.wait_for(adapter._event_queue.join(), 1)
        scheduler.execute.assert_not_awaited()
        assert adapter.expired_events == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(adapter._event_queue.join(), 1)


@pytest.mark.asyncio
async def test_dispatcher_cancel_cleans_running_and_pending(tmp_path):
    registry = PlatformAdapterRegistry()
    registry.register("dummy", _DummyAdapter)
    manager = PlatformAdapterManager(registry=registry)
    adapter = manager.add_adapter(PlatformConfig(id="bot", type="dummy", settings={"event_concurrency": 1}))
    assert adapter is not None
    started = asyncio.Event()

    class Scheduler:
        async def execute(self, event):
            started.set()
            await asyncio.Future()

    assets = []
    for index in range(4):
        event = _message_event("bot")
        asset = tmp_path / f"{index}.txt"
        asset.write_text("temporary", encoding="utf-8")
        event.track_temporary_local_file(str(asset))
        assets.append(asset)
        adapter.commit_event(event)
    task = asyncio.create_task(EventDispatcher(manager, cast(PipelineScheduler, Scheduler())).dispatch_loop())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(adapter._event_queue.join(), 1)
    assert all(not asset.exists() for asset in assets)


@pytest.mark.parametrize("scope", ["group_member", "group"])
def test_scoped_routes_isolate_groups_accounts_and_preserve_legacy(tmp_path, scope):
    from satrap.core.conversation import ConversationRoute

    scm = _session_class_mgr(tmp_path)
    sm = _session_manager(tmp_path, scm)
    um = UserManager(sm, db_path=tmp_path / "users.db")
    legacy = um.resolve_session("123", "bot", "dummy", scm)

    def resolve(user="123", group="456", account="10000", selected=scope):
        route = ConversationRoute(user, "bot", "dummy", scope=selected, self_id=account, group_id=group)
        session_id = um.resolve_session(user, "bot", "dummy", scm, route=route)
        return route, session_id

    route, first = resolve()
    assert first != legacy
    assert resolve()[1] == first
    assert resolve(group="789")[1] != first
    assert resolve(account="20000")[1] != first
    other = resolve(user="321")[1]
    assert (other == first) == (scope == "group")
    assert um.resolve_session("123", "bot", "dummy", scm) == legacy
    assert resolve(selected="group" if scope == "group_member" else "group_member")[1] != first
    restored = UserManager(sm, db_path=tmp_path / "users.db")
    assert restored.resolve_session("123", "bot", "dummy", scm, route=route) == first
    record = restored.store.get_context_session("123", "bot", "dummy", context_key=route.key)
    assert record is not None and record.user_id == ("" if scope == "group" else "123")
    if scope == "group":
        assert first not in restored.store.list_user_sessions("123")
        assert first not in restored.store.list_user_sessions("321")


@pytest.mark.asyncio
async def test_scheduler_routes_group_members_without_changing_actor(tmp_path):
    from satrap.core.platform.onebot.adapter import OneBotAdapter
    from satrap.core.type import UserCall

    scm = _session_class_mgr(tmp_path)
    sm = _session_manager(tmp_path, scm)
    um = UserManager(sm, db_path=tmp_path / "users.db")
    calls: list[UserCall] = []

    class RecordingManager:
        class_cfg_mgr = scm

        async def handle_call_async(self, call):
            calls.append(call)
            return ""

    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", session_type="dummy", settings={"context_scope": "group_member"}))
    scheduler = PipelineScheduler(cast(SessionManager, RecordingManager()), user_manager=um)
    for group in [456, 789, 456]:
        await adapter._handle_group_message({
            "self_id": 10000, "user_id": 123, "group_id": group,
            "message_type": "group", "message": [{"type": "at", "data": {"qq": "10000"}}],
        })
        await scheduler.execute(adapter._event_queue.get_nowait())
    assert len(calls) == 3
    assert calls[0].session_id == calls[2].session_id != calls[1].session_id
    assert all(call.route is not None and call.route.user_id == "123" for call in calls)


@pytest.mark.asyncio
async def test_scoped_command_switch_updates_original_route(tmp_path, monkeypatch):
    from satrap.core.conversation import ConversationRoute
    from satrap.core.type import CommandAction, UserCall

    scm = _session_class_mgr(tmp_path)
    sm = _session_manager(tmp_path, scm)
    um = UserManager(sm, db_path=tmp_path / "users.db")
    sm._user_mgr = um
    route = ConversationRoute("123", "bot", "dummy", scope="group_member", self_id="10000", group_id="456")
    initial = um.resolve_session("123", "bot", "dummy", scm, route=route)
    target = um.resolve_session("123", "bot", "dummy", scm)
    monkeypatch.setattr(sm, "_invoke_sync_entry", lambda *args: CommandAction("switch_session", target, "switched"))
    result = await sm.handle_call_async(UserCall(session_id=initial, message="switch", route=route))
    assert result == "switched"
    assert um.resolve_session("123", "bot", "dummy", scm, route=route) == target
    untouched = ConversationRoute("123", "bot", "dummy", scope="group_member", self_id="10000", group_id="789")
    assert um.resolve_session("123", "bot", "dummy", scm, route=untouched) != target
