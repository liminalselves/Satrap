"""会话管理并发与生命周期回归测试"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock
import threading
import asyncio
from pathlib import Path
import pytest
from typing import cast
import time

from satrap.core.framework.SessionManager import (
    SessionConfig,
    SessionEntry,
    SessionManager,
    SessionPool,
    SessionPoolCapacityError,
)
from satrap.core.framework.UserManager import UserManager
from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.type import LLMConfig, UserCall


class _EchoSession(Session):
    """测试用同步会话"""

    def run(self, message: str) -> str:
        """
        返回输入消息

        参数:
        - message: 输入消息

        返回:
        - 原始输入消息
        """
        return message


class _BlockingAsyncSession(AsyncSession):
    """测试异步删除与活动调用互斥的会话"""

    def __init__(
        self,
        session_id: str,
        db_path: str,
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        """
        初始化可控异步会话

        参数:
        - session_id: 会话 ID
        - db_path: 上下文数据库路径
        - entered: 记录 run 已开始的事件
        - release: 允许 run 完成的事件
        """
        super().__init__(session_id, db_path=db_path)
        self.entered = entered
        self.release = release

    async def run(self, message: str) -> str:
        """
        等待测试放行后返回输入消息

        参数:
        - message: 输入消息

        返回:
        - 原始输入消息
        """
        self.entered.set()
        await self.release.wait()
        return message


def _make_manager(tmp_path: Path, max_size: int = 1000) -> SessionManager:
    """
    创建隔离的会话管理器

    参数:
    - tmp_path: 临时目录
    - max_size: 活跃池容量

    返回:
    - 测试会话管理器
    """
    return SessionManager(
        default_session_type="echo",
        default_session_class=_EchoSession,
        max_size=max_size,
        db_path=tmp_path / "platform.db",
        default_checkpoint_db=str(tmp_path / "context.db"),
    )


@pytest.mark.parametrize("_iteration", range(20))
def test_sync_calls_create_one_runtime_for_same_session(
    _iteration: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    并发同步调用同一会话时只创建一个运行时实例

    参数:
    - _iteration: 重复运行序号
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    manager = _make_manager(tmp_path)
    manager.register_session(_EchoSession, "echo", session_id="shared")
    original_create = manager._create_entry
    creation_count = 0
    count_lock = threading.Lock()
    start = threading.Barrier(2)

    def delayed_create(
        session_config: SessionConfig,
        *,
        add_to_pool: bool = True,
    ):
        """扩大创建竞态窗口并记录创建次数"""
        nonlocal creation_count
        with count_lock:
            creation_count += 1
        time.sleep(0.05)
        return original_create(session_config, add_to_pool=add_to_pool)

    monkeypatch.setattr(manager, "_create_entry", delayed_create)

    def invoke(message: str) -> str:
        """等待并发起跑后调用共享会话"""
        start.wait()
        return manager.handle_call(UserCall(session_id="shared", message=message))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, ["one", "two"]))

    assert sorted(results) == ["one", "two"]
    assert creation_count == 1
    assert list(manager.pool.list_entries()) == ["shared"]


def test_pool_does_not_collect_or_evict_leased_entry() -> None:
    """池满且所有条目忙碌时拒绝新条目并保持硬容量上限"""
    pool = SessionPool(max_size=1, idle_timeout=0)
    pool.put("busy", MagicMock(spec=Session), "echo")
    busy = pool.acquire("busy")
    assert busy is not None

    with pytest.raises(SessionPoolCapacityError, match="所有条目均在使用"):
        pool.put("new", MagicMock(spec=Session), "echo")

    assert len(pool.list_entries()) == 1
    assert pool.get("busy") is busy

    pool.release(busy)
    evicted = pool.put("new", MagicMock(spec=Session), "echo")
    assert evicted is not None and evicted[0] == "busy"
    assert list(pool.list_entries()) == ["new"]


def test_sync_remove_session_refuses_active_lease(tmp_path: Path) -> None:
    """
    同步删除不得移除仍有活动调用租约的会话

    参数:
    - tmp_path: 临时目录
    """
    manager = _make_manager(tmp_path)
    manager.register_session(_EchoSession, "echo", session_id="shared")
    session = _EchoSession("shared", db_path=str(tmp_path / "context.db"))
    manager.pool.put("shared", session, "echo")
    entry = manager.pool.acquire("shared")
    assert entry is not None

    assert manager.remove_session("shared") is False
    assert manager.pool.get("shared") is entry

    manager.pool.release(entry)
    assert manager.remove_session("shared") is True
    assert manager.pool.get("shared") is None


@pytest.mark.asyncio
async def test_async_remove_waits_for_running_session(tmp_path: Path) -> None:
    """
    异步删除等待正在执行的会话完成后再关闭运行时

    参数:
    - tmp_path: 临时目录
    """
    manager = _make_manager(tmp_path)
    manager.register_session(_EchoSession, "echo", session_id="shared")
    entered = asyncio.Event()
    release = asyncio.Event()
    session = _BlockingAsyncSession(
        "shared",
        str(tmp_path / "context.db"),
        entered,
        release,
    )
    manager.pool.put("shared", session, "echo")

    call_task = asyncio.create_task(
        manager.handle_call_async(UserCall(session_id="shared", message="answer"))
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    remove_task = asyncio.create_task(manager.remove_session_async("shared"))
    await asyncio.sleep(0.02)
    assert remove_task.done() is False

    release.set()
    assert await asyncio.wait_for(call_task, timeout=1) == "answer"
    assert await asyncio.wait_for(remove_task, timeout=1) is True
    assert manager.pool.get("shared") is None


@pytest.mark.asyncio
async def test_async_creation_lock_wait_does_not_block_event_loop(tmp_path: Path) -> None:
    """
    异步创建等待线程锁时事件循环仍可调度定时器

    参数:
    - tmp_path: 临时目录
    """
    manager = _make_manager(tmp_path)
    config = manager.register_session(_EchoSession, "echo", session_id="shared")
    lock_entered = threading.Event()
    release_lock = threading.Event()

    def hold_creation_lock() -> None:
        """持有创建锁直到异步断言完成"""
        with manager._entry_creation_lock:
            lock_entered.set()
            release_lock.wait(timeout=2)

    worker = threading.Thread(target=hold_creation_lock)
    worker.start()
    assert lock_entered.wait(timeout=1)
    timer_fired = asyncio.Event()
    asyncio.get_running_loop().call_later(0.02, timer_fired.set)
    creation_task = asyncio.create_task(manager._get_or_create_entry_async(config))

    try:
        await asyncio.wait_for(timer_fired.wait(), timeout=0.2)
        assert creation_task.done() is False
    finally:
        release_lock.set()
        worker.join(timeout=1)

    assert await asyncio.wait_for(creation_task, timeout=1) is not None


@pytest.mark.asyncio
async def test_async_model_reload_waits_for_session_operation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    异步模型重载等待正在执行的会话操作结束

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    manager = _make_manager(tmp_path)
    manager.register_session(_EchoSession, "echo", session_id="shared")
    assert manager.activate_session("shared") is True
    entry = manager.pool.get("shared")
    assert entry is not None
    manager._model_cfg_mgr = MagicMock()
    applied = asyncio.Event()

    def prepare_reload(
        _session_id: str,
        _entry: SessionEntry,
    ) -> tuple[LLM | AsyncLLM, LLMConfig]:
        """返回不访问网络的模型重载占位对象"""
        return cast(LLM, MagicMock()), cast(LLMConfig, MagicMock())

    def apply_reload(
        _session_id: str,
        _session: Session | AsyncSession,
        _new_llm: LLM | AsyncLLM,
        _llm_config: LLMConfig,
    ) -> None:
        """记录模型重载已进入应用阶段"""
        applied.set()

    monkeypatch.setattr(manager, "_prepare_model_reload", prepare_reload)
    monkeypatch.setattr(manager, "_apply_model_reload", apply_reload)

    async with entry.async_operation_lock:
        reload_task = asyncio.create_task(manager.reload_model_configs_async())
        await asyncio.sleep(0)
        assert reload_task.done() is False
        assert applied.is_set() is False

    await reload_task
    assert applied.is_set() is True


class _BlockingSessionManager:
    """用于观察 UserManager 锁范围的会话管理器替身"""

    def __init__(self) -> None:
        """初始化阻塞控制事件"""
        self.entered = threading.Event()
        self.release = threading.Event()

    def handle_call(self, user_call: UserCall) -> str:
        """
        阻塞处理调用直到测试放行

        参数:
        - user_call: 用户调用

        返回:
        - 固定响应
        """
        self.entered.set()
        self.release.wait(timeout=2)
        return user_call.message or ""


def test_user_manager_releases_global_lock_before_session_call(tmp_path: Path) -> None:
    """
    一个用户的慢会话调用不得阻塞其他用户管理操作

    参数:
    - tmp_path: 临时目录
    """
    session_manager = _BlockingSessionManager()
    user_manager = UserManager(
        cast(SessionManager, session_manager),
        db_path=tmp_path / "users.db",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        route_future = executor.submit(
            user_manager.route_call,
            UserCall(message="done"),
            "slow-user",
        )
        assert session_manager.entered.wait(timeout=1)
        create_future = executor.submit(user_manager.get_or_create_user, "other-user")
        assert create_future.result(timeout=1) is not None
        session_manager.release.set()
        assert route_future.result(timeout=1) == "done"
