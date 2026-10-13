"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
import pytest
from typing import Any
from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap.core.state import StateStore
from typing import Callable, Optional
import time
from satrap.core.framework.SessionManager import SessionConfig, SessionManager
from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.framework.command import CommandHandler
from satrap.core.utils.paths import get_db_path
import sqlite3
from satrap.core.type import StateScope
from typing import cast
from types import SimpleNamespace
import json
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from argparse import Namespace
from satrap.cli.cmd_checkpoint import dispatch
from satrap.core.storage import StorageLayout

class _SimpleSession(Session):
    """
    最小会话子类: 一个工作流上下文, 使用真实 ContextManager 模拟

    子类测试聚焦手动/聚合检查点语义, 上下文统一关闭自动 stable 检查点以免干扰断言
    """

    def __init__(self, session_id: str, db_path: str):
        super().__init__(session_id, db_path=db_path, enable_checkpoint=True)
        self.session_ctx = ContextManager(session_id, db_path=db_path, auto_checkpoint=False)
        self.session_ctx.state_store = self._state_store
        self.wf_ctx = ContextManager(
            self.workflow_id_assign("main"), db_path=db_path, auto_checkpoint=False
        )
        self._track_workflow_context("main", self.wf_ctx)

class _AsyncSimpleSession(AsyncSession):
    """最小异步会话子类"""

    def __init__(self, session_id: str, db_path: str):
        super().__init__(session_id, db_path=db_path, enable_checkpoint=True)
        self.wf_ctx: AsyncContextManager | None = None

    async def _async_init(self):
        self.wf_ctx = AsyncContextManager(
            self.workflow_id_assign("main"),
            db_path=self.session_ctx.db_path,
            auto_checkpoint=False,
        )
        self._track_workflow_context("main", self.wf_ctx)
        await self.wf_ctx.initialize()

def test_workflow_id_assign_deduplicates_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    db = str(tmp_path / "chat_history.db")
    session = Session("s1", db_path=db)

    first = session.workflow_id_assign("main")
    second = session.workflow_id_assign("main")
    third = session.workflow_id_assign("main")

    assert first == "s1_main"
    assert second == "s1_main_2"
    assert third == "s1_main_3"
    assert session.wf_list == ["s1_main", "s1_main_2", "s1_main_3"]
    assert "工作流 ID 重复" in caplog.text

def test_session_checkpoint_rollback_roundtrip(tmp_path: Path):
    """写消息 -> 会话级检查点 -> 继续写 -> 回滚还原全部上下文"""
    db = str(tmp_path / "chat_history.db")
    session = _SimpleSession("s1", db)
    session.session_ctx.add_user_message("会话消息")
    session.wf_ctx.add_user_message("工作流消息")
    batch_id = session.create_checkpoint(name="起点")

    session.session_ctx.add_user_message("会话后续")
    session.wf_ctx.add_user_message("工作流后续")
    assert len(session.session_ctx.get_context()) == 2
    assert len(session.wf_ctx.get_context()) == 2

    session.rollback(batch_id)

    assert session.session_ctx.get_context() == [{"role": "user", "content": "会话消息"}]
    assert session.wf_ctx.get_context() == [{"role": "user", "content": "工作流消息"}]

def test_session_rollback_accepts_checkpoint_id(tmp_path: Path):
    """rollback 也接受批次内单个检查点 ID"""
    db = str(tmp_path / "chat_history.db")
    session = _SimpleSession("s1", db)
    session.wf_ctx.add_user_message("第一条")
    batch_id = session.create_checkpoint()
    session.wf_ctx.add_user_message("第二条")

    checkpoints = session.list_checkpoints()
    assert len(checkpoints) == 1
    session.rollback(checkpoints[0].checkpoint_id)

    assert session.wf_ctx.get_context() == [{"role": "user", "content": "第一条"}]
    assert session.wf_ctx.get_context()[0]["content"] == "第一条"

def test_session_list_checkpoints_deduplicates_by_batch(tmp_path: Path):
    """list_checkpoints 按批次去重, 每次聚合只出现一个代表检查点"""
    db = str(tmp_path / "chat_history.db")
    session = _SimpleSession("s1", db)
    session.create_checkpoint(name="第一批")
    session.create_checkpoint(name="第二批")

    checkpoints = session.list_checkpoints()
    assert len(checkpoints) == 2
    assert checkpoints[0].name == "第一批[session]" or checkpoints[0].batch_id
    assert all(cp.batch_id for cp in checkpoints)

def test_session_fork_returns_all_contexts(tmp_path: Path):
    """fork 返回会话共享 + 全部工作流的新上下文, 数据独立"""
    db = str(tmp_path / "chat_history.db")
    session = _SimpleSession("s1", db)
    session.session_ctx.add_user_message("会话起点")
    session.wf_ctx.add_user_message("工作流起点")
    batch_id = session.create_checkpoint(name="起点")

    forked = session.fork("bad_end", checkpoint_id=batch_id)

    assert set(forked.keys()) == {"session", "main"}
    assert forked["session"].conversation_id == "s1:fork:bad_end"
    assert forked["main"].conversation_id == "s1_main:fork:bad_end"
    assert forked["session"].get_context() == [{"role": "user", "content": "会话起点"}]
    assert forked["main"].get_context() == [{"role": "user", "content": "工作流起点"}]
    # 父会话数据保持完整
    assert session.session_ctx.get_context() == [{"role": "user", "content": "会话起点"}]
    assert session.wf_ctx.get_context() == [{"role": "user", "content": "工作流起点"}]

def test_session_checkpoint_requires_enabled_store(tmp_path: Path):
    """未启用检查点时聚合操作抛出 ValueError"""
    class _PlainSession(Session):
        def __init__(self, session_id: str):
            super().__init__(session_id)

    session = _PlainSession("plain")

    with pytest.raises(ValueError, match="未启用会话级状态检查点"):
        session.create_checkpoint()
    with pytest.raises(ValueError, match="未启用会话级状态检查点"):
        session.list_checkpoints()
    with pytest.raises(ValueError, match="未启用会话级状态检查点"):
        session.rollback("any")
    with pytest.raises(ValueError, match="未启用会话级状态检查点"):
        session.fork("alt")

def test_track_workflow_context_rejects_mismatched_db(tmp_path: Path):
    """工作流上下文与状态库不同库时明确报错, 防止静默不一致"""
    db = str(tmp_path / "chat_history.db")
    session = _SimpleSession("s1", db)

    with pytest.raises(ValueError, match="不一致"):
        session._track_workflow_context("other", ContextManager("s1_other", db_path=str(tmp_path / "other.db")))

def test_state_store_batch_rollback(tmp_path: Path):
    """同批次检查点共享 batch_id, 可一次性回滚"""
    from satrap.core.type import StateScope

    store = StateStore(db_path=str(tmp_path / "state.db"))
    scope_a = StateScope("test", "conv-a")
    scope_b = StateScope("test", "conv-b")

    batch_id = "batch-test-1"
    store.create_checkpoint(scope_a, batch_id=batch_id)
    store.create_checkpoint(scope_b, batch_id=batch_id)

    batch = store.list_checkpoints_by_batch(batch_id)
    assert len(batch) == 2
    assert {cp.scope_id for cp in batch} == {"conv-a", "conv-b"}
    assert all(cp.batch_id == batch_id for cp in batch)

    store.create_checkpoint(StateScope("test", "conv-c"))
    # 无关检查点不受批量回滚影响 (回滚后批次检查点保留, 与单检查点 rollback 语义一致)
    count = store.rollback_batch(batch_id)
    assert count == 2
    assert len(store.list_checkpoints_by_batch(batch_id)) == 2
    # 批次内检查点保留, 可再次回滚
    store.rollback_batch(batch_id)

    with pytest.raises(ValueError, match="批次不存在"):
        store.rollback_batch("batch-missing")

def test_state_store_batch_rejects_empty(tmp_path: Path):
    """空批次 ID 应被拒绝 (单检查点请用 rollback)"""
    store = StateStore(db_path=str(tmp_path / "state.db"))
    with pytest.raises(ValueError, match="batch_id 不能为空"):
        store.rollback_batch("")
    with pytest.raises(ValueError, match="batch_id 不能为空"):
        store.retry_batch("")

def _user_checkpoints(checkpoints: list[Any]) -> list[Any]:
    """过滤系统保护检查点, 仅保留用户可见检查点"""
    return [cp for cp in checkpoints if cp.source not in ("rollback_snapshot", "retry_snapshot", "edit_protect")]

def test_session_retry_keeps_future_checkpoints(tmp_path: Path):
    """retry 恢复数据但保留未来检查点, 与 rollback 语义区分"""
    db = str(tmp_path / "chat_history.db")
    session = _SimpleSession("s1", db)
    session.wf_ctx.add_user_message("第一条")
    cp1 = session.create_checkpoint(name="起点")

    session.wf_ctx.add_user_message("第二条")
    cp2 = session.create_checkpoint(name="中期")
    session.wf_ctx.add_user_message("第三条")

    session.rollback(cp1)
    # rollback 到起点会删除中期检查点 (保护检查点保留, 可撤销)
    assert session.wf_ctx.get_context() == [{"role": "user", "content": "第一条"}]
    assert len(_user_checkpoints(session.list_checkpoints())) == 1
    assert any(cp.source == "rollback_snapshot" for cp in session.list_checkpoints())

    session.wf_ctx.add_user_message("第二条")
    # retry 到起点后, 未来检查点保留
    cp2b = session.create_checkpoint(name="中期2")
    session.wf_ctx.add_user_message("第三条")
    session.retry(cp2b)
    assert session.wf_ctx.get_context() == [
        {"role": "user", "content": "第一条"},
        {"role": "user", "content": "第二条"},
    ]
    assert len(_user_checkpoints(session.list_checkpoints())) == 2   # 起点 + 中期2 都保留

    protects = [
        cp
        for cp in session.list_checkpoints()
        if cp.source == "retry_snapshot" and cp.scope_id == "s1_main"
    ]
    # 可回滚到保护检查点撤销 retry (恢复 retry 前的最新状态)
    assert protects
    session.rollback(protects[0].checkpoint_id)
    assert session.wf_ctx.get_context() == [
        {"role": "user", "content": "第一条"},
        {"role": "user", "content": "第二条"},
        {"role": "user", "content": "第三条"},
    ]

def test_context_retry_keeps_future_checkpoints(tmp_path: Path):
    """ContextManager.retry 恢复数据且未来检查点仍在"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager(
        "conv-r", db_path=db, enable_checkpoint=True, auto_checkpoint=False
    )
    ctx.add_user_message("第一条")
    cp1 = ctx.create_checkpoint(name="起点")
    ctx.add_user_message("第二条")
    cp2 = ctx.create_checkpoint(name="中期")

    ctx.retry(cp1.checkpoint_id)

    assert ctx.get_context() == [{"role": "user", "content": "第一条"}]
    remaining = _user_checkpoints(ctx.list_checkpoints())
    assert {cp.checkpoint_id for cp in remaining} == {cp1.checkpoint_id, cp2.checkpoint_id}
    # 重试前保护检查点保留, 可回滚撤销
    assert any(cp.source == "retry_snapshot" for cp in ctx.list_checkpoints())

def test_state_store_lineage_and_branches(tmp_path: Path):
    """fork 血缘: trace_lineage 回溯父链, list_child_branches 找到子分支"""
    from satrap.core.type import StateScope

    store = StateStore(db_path=str(tmp_path / "state.db"))
    scope = StateScope("conversation", "conv-1")
    root = store.create_checkpoint(scope, name="根")

    store.fork(root.checkpoint_id, "conv-1:fork:alt1")
    # fork 出两条分支
    store.fork(root.checkpoint_id, "conv-1:fork:alt2")

    children = store.list_child_branches(root.checkpoint_id)
    assert len(children) == 2
    assert {cp.scope_id for cp in children} == {"conv-1:fork:alt1", "conv-1:fork:alt2"}
    assert all(cp.parent_checkpoint_id == root.checkpoint_id for cp in children)

    lineage = store.trace_lineage(children[0].checkpoint_id)
    # 从子分支回溯血缘: 根在前
    assert [cp.checkpoint_id for cp in lineage] == [root.checkpoint_id, children[0].checkpoint_id]

    branches = store.list_branches("conv-1:fork:")
    # 前缀查询分支
    assert len(branches) == 2
    assert store.list_branches("conv-1:fork:") == branches

def test_session_list_branches_aggregates_workflows(tmp_path: Path):
    """Session.list_branches 聚合会话共享 + 工作流的分支"""
    db = str(tmp_path / "chat_history.db")
    session = _SimpleSession("s1", db)
    session.session_ctx.add_user_message("会话起点")
    session.wf_ctx.add_user_message("工作流起点")
    session.create_checkpoint()

    session.fork("alt")

    branches = session.list_branches()
    # 会话共享 + 工作流各 fork 出一条分支, 各含 fork 起点检查点
    assert len(branches) == 2
    assert {cp.scope_id for cp in branches} == {"s1:fork:alt", "s1_main:fork:alt"}

def test_trace_lineage_missing_raises(tmp_path: Path):
    """不存在的检查点回溯血缘时抛出 ValueError"""
    store = StateStore(db_path=str(tmp_path / "state.db"))
    with pytest.raises(ValueError, match="检查点不存在"):
        store.trace_lineage("missing-cp")

def test_auto_checkpoint_creates_stable_on_write(tmp_path: Path):
    """启用检查点后, 消息写入自动产生 stable 检查点"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-auto", db_path=db, enable_checkpoint=True)
    assert ctx.auto_checkpoint is True

    ctx.add_user_message("第一条")
    ctx.add_bot_message("回复一")

    checkpoints = ctx.list_checkpoints()
    stables = [cp for cp in checkpoints if cp.checkpoint_kind == "stable"]
    assert len(stables) == 2   # 每次写入一个水位
    assert all(cp.name.startswith("水位 ") for cp in stables)
    assert stables[0].position < stables[1].position

def test_auto_checkpoint_disabled_skips_stable(tmp_path: Path):
    """auto_checkpoint=False 时消息写入不产生 stable 检查点"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager(
        "conv-manual", db_path=db, enable_checkpoint=True, auto_checkpoint=False
    )

    ctx.add_user_message("第一条")
    assert ctx.list_checkpoints() == []

def test_auto_checkpoint_rollback_restores_stable_state(tmp_path: Path):
    """stable 自动保存后, rollback 能回到消息中间水位"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-stable", db_path=db, enable_checkpoint=True)

    ctx.add_user_message("第一条")
    ctx.add_bot_message("回复一")
    stables = [cp for cp in ctx.list_checkpoints() if cp.checkpoint_kind == "stable"]
    assert len(stables) == 2

    ctx.add_user_message("第二条")
    ctx.rollback(stables[0].checkpoint_id)   # 回到第一条消息后

    assert ctx.get_context() == [{"role": "user", "content": "第一条"}]

@pytest.mark.asyncio
async def test_async_auto_checkpoint_creates_stable(tmp_path: Path):
    """异步上下文消息写入自动产生 stable 检查点"""
    db = str(tmp_path / "chat_history.db")
    ctx = AsyncContextManager("conv-async-auto", db_path=db, enable_checkpoint=True)
    await ctx.initialize()

    await ctx.add_user_message("第一条")

    checkpoints = await ctx.list_checkpoints()
    assert len([cp for cp in checkpoints if cp.checkpoint_kind == "stable"]) == 1

def test_list_mutations_records_source_and_reason(tmp_path: Path):
    """list_mutations 按时间倒序返回检查点, 含 source / reason 审计字段"""
    db = str(tmp_path / "chat_history.db")
    session = _SimpleSession("s1", db)
    session.wf_ctx.add_user_message("第一条")
    session.create_checkpoint(name="起点")

    session.fork("alt")
    mutations = session.list_mutations()

    agg = [m for m in mutations if m.checkpoint_kind == "manual" and m.batch_id]
    # 会话聚合检查点带 source (会话共享 + 工作流各一条, 同一批次)
    assert len(agg) == 2
    assert len({m.batch_id for m in agg}) == 1
    assert all(m.source == "session_checkpoint" for m in agg)
    assert all("创建会话检查点" in m.reason for m in agg)

    from satrap.core.type import StateScope
    # fork 出的分支检查点在新作用域, 经 store 直查可见 fork 来源

    store = StateStore(db_path=db)
    fork_mutations = store.list_mutations(
        StateScope("conversation", "s1_main:fork:alt")
    )
    assert len(fork_mutations) == 1
    assert fork_mutations[0].source == "checkpoint_fork"

    assert mutations[0].created_at >= mutations[-1].created_at

def test_store_list_mutations_filters_by_scope(tmp_path: Path):
    """StateStore.list_mutations 仅返回指定作用域的变更记录"""
    from satrap.core.type import StateScope

    store = StateStore(db_path=str(tmp_path / "state.db"))
    scope_a = StateScope("conversation", "conv-a")
    scope_b = StateScope("conversation", "conv-b")
    store.create_checkpoint(scope_a, name="A1")
    store.create_checkpoint(scope_b, name="B1")
    store.create_checkpoint(scope_a, name="A2")

    mutations = store.list_mutations(scope_a)
    assert [cp.name for cp in mutations] == ["A2", "A1"]

def test_session_list_mutations_aggregates_all_contexts(tmp_path: Path):
    """Session.list_mutations 聚合会话共享 + 工作流的变更记录"""
    db = str(tmp_path / "chat_history.db")
    session = _SimpleSession("s1", db)
    session.session_ctx.add_user_message("会话消息")
    session.wf_ctx.add_user_message("工作流消息")
    session.create_checkpoint()

    mutations = session.list_mutations()
    # 会话共享 + 工作流各 1 个聚合检查点
    assert len([m for m in mutations if m.batch_id]) == 2
    assert {m.scope_id for m in mutations if m.batch_id} == {"s1", "s1_main"}

@pytest.mark.asyncio
async def test_async_session_checkpoint_rollback_roundtrip(tmp_path: Path):
    db = str(tmp_path / "chat_history.db")
    session = _AsyncSimpleSession("as1", db)
    await session.initialize()
    assert session.wf_ctx is not None

    await session.session_ctx.add_user_message("会话消息")
    await session.wf_ctx.add_user_message("工作流消息")
    batch_id = await session.create_checkpoint(name="起点")

    await session.session_ctx.add_user_message("会话后续")
    await session.wf_ctx.add_user_message("工作流后续")

    await session.rollback(batch_id)

    assert len(session.session_ctx.get_context()) == 1
    assert len(session.wf_ctx.get_context()) == 1
    assert session.session_ctx.get_context()[0]["content"] == "会话消息"
    assert session.wf_ctx.get_context()[0]["content"] == "工作流消息"

@pytest.mark.asyncio
async def test_async_session_fork_returns_initialized_contexts(tmp_path: Path):
    db = str(tmp_path / "chat_history.db")
    session = _AsyncSimpleSession("as1", db)
    await session.initialize()
    assert session.wf_ctx is not None

    await session.session_ctx.add_user_message("会话起点")
    await session.wf_ctx.add_user_message("工作流起点")
    batch_id = await session.create_checkpoint()

    forked = await session.fork("alt", checkpoint_id=batch_id)

    assert set(forked.keys()) == {"session", "main"}
    assert forked["session"].conversation_id == "as1:fork:alt"
    assert forked["main"].conversation_id == "as1_main:fork:alt"
    assert forked["session"].get_context()[0]["content"] == "会话起点"
    assert forked["main"].get_context()[0]["content"] == "工作流起点"

@pytest.mark.asyncio
async def test_async_session_retry_keeps_future_checkpoints(tmp_path: Path):
    db = str(tmp_path / "chat_history.db")
    session = _AsyncSimpleSession("as1", db)
    await session.initialize()
    assert session.wf_ctx is not None

    await session.wf_ctx.add_user_message("第一条")
    cp = await session.create_checkpoint(name="起点")
    await session.wf_ctx.add_user_message("第二条")
    cp2 = await session.create_checkpoint(name="中期")

    await session.retry(cp)

    assert session.wf_ctx.get_context() == [{"role": "user", "content": "第一条"}]
    assert len(_user_checkpoints(await session.list_checkpoints())) == 2

class _RecordingSession(Session):
    """记录收到的检查点构造参数"""

    received: dict[str, Any] = {}

    def __init__(
        self,
        session_id: str,
        content_callback: Optional[Callable[[str], None]] | None = None,
        command_handler: Optional[CommandHandler] | None = None,
        *,
        db_path: str = get_db_path(),
        state_store: Optional[StateStore] = None,
        enable_checkpoint: bool = False,
        **kw: Any,
    ):
        type(self).received = {
            "db_path": db_path,
            "enable_checkpoint": enable_checkpoint,
            "state_store": state_store,
        }
        super().__init__(
            session_id,
            content_callback,
            command_handler,
            db_path=db_path,
            state_store=state_store,
            enable_checkpoint=enable_checkpoint,
        )

class _CkptSession(Session):
    """真实可用检查点的会话子类 (带一个工作流上下文)"""

    def __init__(
        self,
        session_id: str,
        content_callback: Optional[Callable[[str], None]] | None = None,
        command_handler: Optional[CommandHandler] | None = None,
        *,
        db_path: str = get_db_path(),
        state_store: Optional[StateStore] = None,
        enable_checkpoint: bool = False,
        **kw: Any,
    ):
        super().__init__(
            session_id,
            content_callback,
            command_handler,
            db_path=db_path,
            state_store=state_store,
            enable_checkpoint=enable_checkpoint,
        )
        self.wf_ctx = ContextManager(
            self.workflow_id_assign("main"),
            db_path=db_path,
            state_store=self._state_store,
            enable_checkpoint=True,
            auto_checkpoint=False,
        )
        self._track_workflow_context("main", self.wf_ctx)

def _make_cfg(session_id: str, params: dict[str, Any] | None = None) -> SessionConfig:
    now = time.time()
    return SessionConfig(
        session_id=session_id,
        session_type_name="rec",
        created_at=now,
        last_used_at=now,
        message_count=0,
        session_config=params or {},
    )

def _make_mgr(tmp_path: Path, **kw: Any) -> SessionManager:
    sm = SessionManager(db_path=str(tmp_path / "session_config.db"), **kw)
    sm.registry.register("rec", _RecordingSession)
    return sm

def test_default_checkpoint_injected(tmp_path: Path):
    """default_checkpoint=True 时, 未显式配置的会话收到注入的开关与库路径"""
    db = str(tmp_path / "chat_history.db")
    sm = _make_mgr(tmp_path, default_checkpoint=True, default_checkpoint_db=db)

    entry = sm._create_entry(_make_cfg("sid-1"))

    assert entry is not None
    received = _RecordingSession.received
    assert received["enable_checkpoint"] is True
    assert received["db_path"] == db

def test_explicit_config_overrides_default(tmp_path: Path):
    """session_config 显式配置优先于 SessionManager 默认值"""
    db = str(tmp_path / "chat_history.db")
    db_custom = str(tmp_path / "custom.db")
    sm = _make_mgr(tmp_path, default_checkpoint=True, default_checkpoint_db=db)

    entry = sm._create_entry(
        _make_cfg("sid-2", {"enable_checkpoint": False, "db_path": db_custom})
    )

    assert entry is not None
    received = _RecordingSession.received
    assert received["enable_checkpoint"] is False
    assert received["db_path"] == db_custom

def test_default_off_keeps_behavior(tmp_path: Path):
    """default_checkpoint=False (默认) 时行为不变: 收到 False"""
    sm = _make_mgr(tmp_path)

    entry = sm._create_entry(_make_cfg("sid-3"))

    assert entry is not None
    assert _RecordingSession.received["enable_checkpoint"] is False

def test_default_db_only_when_configured(tmp_path: Path):
    """default_checkpoint_db=None 时 db_path 使用会话自身默认"""
    sm = _make_mgr(tmp_path, default_checkpoint=True)

    entry = sm._create_entry(_make_cfg("sid-4"))

    assert entry is not None
    assert _RecordingSession.received["db_path"] == get_db_path()

def test_injection_ignored_when_constructor_rejects(tmp_path: Path):
    """构造器不接受 enable_checkpoint 时注入被忽略, 不抛错"""

    class _StrictSession(Session):
        kw: dict[str, Any] = {}

        def __init__(self, session_id: str):
            self.session_id = session_id
            self.kw = {}

    sm = SessionManager(
        db_path=str(tmp_path / "session_config.db"),
        default_checkpoint=True,
        default_checkpoint_db=str(tmp_path / "x.db"),
    )
    sm.registry.register("strict", _StrictSession)

    now = time.time()
    cfg = SessionConfig(
        session_id="sid-5",
        session_type_name="strict",
        created_at=now,
        last_used_at=now,
        message_count=0,
        session_config={},
    )
    entry = sm._create_entry(cfg)

    assert entry is not None
    assert isinstance(entry.session, _StrictSession)
    assert entry.session.kw == {}
    assert not hasattr(entry.session, "enable_checkpoint")

def test_session_config_constructor_case_injected(tmp_path: Path):
    """case 1 (session_config 入参) 同样注入检查点默认值"""

    class _ConfigSession(Session):
        def __init__(
            self, session_config: SessionConfig, session_id: str | None = None, **kw: Any
        ):
            self.session_id = session_id if session_id is not None else (session_config.session_id or "")
            self.received = kw

    sm = SessionManager(
        db_path=str(tmp_path / "session_config.db"),
        default_checkpoint=True,
        default_checkpoint_db=str(tmp_path / "ctx.db"),
    )
    sm.registry.register("cfg", _ConfigSession)

    now = time.time()
    cfg = SessionConfig(
        session_id="sid-6",
        session_type_name="cfg",
        created_at=now,
        last_used_at=now,
        message_count=0,
        session_config={},
    )
    entry = sm._create_entry(cfg)

    assert entry is not None
    assert isinstance(entry.session, _ConfigSession)
    assert entry.session.received["enable_checkpoint"] is True
    assert entry.session.received["db_path"] == str(tmp_path / "ctx.db")

def test_end_to_end_real_session_checkpoint(tmp_path: Path):
    """真实会话子类: 注入开启后会话级检查点端到端可用"""
    db = str(tmp_path / "chat_history.db")
    sm = SessionManager(
        db_path=str(tmp_path / "session_config.db"),
        default_checkpoint=True,
        default_checkpoint_db=db,
    )
    sm.registry.register("ckpt", _CkptSession)

    now = time.time()
    cfg = SessionConfig(
        session_id="sid-7",
        session_type_name="ckpt",
        created_at=now,
        last_used_at=now,
        message_count=0,
        session_config={},
    )
    entry = sm._create_entry(cfg)

    assert entry is not None
    session = entry.session
    assert isinstance(session, _CkptSession)
    assert session._state_store is not None

    session.session_ctx.add_user_message("会话消息")
    session.wf_ctx.add_user_message("工作流消息")
    batch_id = session.create_checkpoint(name="起点")
    manuals = [cp for cp in session.list_checkpoints() if cp.checkpoint_kind == "manual"]
    assert len(manuals) == 1   # session_ctx 自动 stable 检查点不计入

    session.wf_ctx.add_user_message("后续")
    session.rollback(batch_id)
    assert session.wf_ctx.get_context() == [{"role": "user", "content": "工作流消息"}]

def test_backend_config_parses_checkpoint_fields():
    """BackendConfig 只接受检查点开关, 数据库固定由平台作用域决定"""
    cfg = BackendConfig.from_dict({"session_checkpoint": True})
    assert cfg.session_checkpoint is True

    default = BackendConfig.from_dict({})
    assert default.session_checkpoint is False

    with pytest.raises(ValueError, match="不再支持独立数据库路径"):
        BackendConfig.from_dict({"session_checkpoint_db": ".satrap/ctx.db"})

class _MiniSession(Session):
    """最小会话: 会话共享 + 一个工作流上下文, 关闭自动检查点"""

    def __init__(self, session_id: str, db_path: str):
        super().__init__(session_id, db_path=db_path, enable_checkpoint=True)
        self.session_ctx = ContextManager(session_id, db_path=db_path, auto_checkpoint=False)
        self.session_ctx.state_store = self._state_store
        self.wf_ctx = ContextManager(
            self.workflow_id_assign("main"), db_path=db_path, auto_checkpoint=False
        )
        self._track_workflow_context("main", self.wf_ctx)

def test_old_db_migration_adds_missing_columns(tmp_path: Path):
    """P3-2 之前的旧库 (无 source/reason 列) 初始化后自动迁移, 读取不崩溃"""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE state_checkpoints (
        checkpoint_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, scope_id TEXT NOT NULL,
        branch_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '', snapshot_id TEXT NOT NULL,
        batch_id TEXT NOT NULL DEFAULT '', state_revision INTEGER NOT NULL,
        position INTEGER NOT NULL DEFAULT 0, checkpoint_kind TEXT NOT NULL DEFAULT 'manual',
        parent_checkpoint_id TEXT, created_at REAL NOT NULL)"""
    )
    conn.execute(
        """CREATE TABLE state_snapshots (
        snapshot_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, scope_id TEXT NOT NULL,
        branch_id TEXT NOT NULL DEFAULT '', snapshot_json TEXT NOT NULL, created_at REAL NOT NULL)"""
    )
    conn.execute(
        """CREATE TABLE state_scopes (
        namespace TEXT NOT NULL, scope_id TEXT NOT NULL, branch_id TEXT NOT NULL DEFAULT '',
        state_revision INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (namespace, scope_id, branch_id))"""
    )
    conn.execute(
        "INSERT INTO state_checkpoints VALUES "
        "('ckpt-old', 'conversation', 'demo', '', '起点', '', 'snap-1', '', 1, 1, 'manual', NULL, 1.0)"
    )
    conn.commit()
    conn.close()

    store = StateStore(db_path=db)
    cps = store.list_checkpoints(StateScope("conversation", "demo"))
    assert len(cps) == 1
    assert cps[0].source == "manual" and cps[0].reason == ""
    assert cps[0].batch_id == ""
    # 迁移后新检查点可正常创建
    cp = store.create_checkpoint(StateScope("conversation", "demo"), name="迁移后")
    assert cp.name == "迁移后"

def test_session_fork_non_batch_checkpoint_not_silent(tmp_path: Path):
    """非批次检查点 fork 不再静默空: 有检查点的上下文正常分支"""
    db = str(tmp_path / "chat_history.db")
    session = _MiniSession("s9", db)
    session.session_ctx.add_user_message("消息")
    cp = session.session_ctx.create_checkpoint(name="单检查点")   # 非聚合, batch_id 空
    assert cp.batch_id == ""

    forked = session.fork("alt")   # 默认取最近检查点 = 单检查点

    assert "session" in forked
    assert forked["session"].conversation_id == "s9:fork:alt"
    assert forked["session"].get_context() == [{"role": "user", "content": "消息"}]
    # 工作流无检查点 -> 跳过并警告
    assert "main" not in forked

def test_pointer_checkpoint_stores_no_snapshot(tmp_path: Path):
    """stable 检查点为纯指针 (快照表零增长), 写入 20 次不膨胀快照"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-p", db_path=db, enable_checkpoint=True)
    for i in range(10):
        ctx.add_user_message(f"消息{i}")
        ctx.add_bot_message(f"回复{i}")

    cps = ctx.list_checkpoints()
    assert len(cps) == 20
    assert all(cp.checkpoint_kind == "stable" for cp in cps)
    assert all(cp.snapshot_id == "" for cp in cps)
    conn = sqlite3.connect(db)
    try:
        snap_count = conn.execute("SELECT COUNT(*) FROM state_snapshots").fetchone()[0]
    finally:
        conn.close()
    assert snap_count == 0

def test_pointer_rollback_truncates_messages(tmp_path: Path):
    """指针检查点回滚 = 截断到水位, 消息内容准确恢复"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-p2", db_path=db, enable_checkpoint=True, auto_checkpoint=False)
    ctx.add_user_message("一")
    ctx.add_user_message("二")
    cp = ctx.create_checkpoint(name="到二为止")
    assert cp.snapshot_id == ""
    assert cp.position == 2

    ctx.add_user_message("三")
    ctx.add_user_message("四")
    ctx.rollback(cp.checkpoint_id)

    assert ctx.get_context() == [
        {"role": "user", "content": "一"},
        {"role": "user", "content": "二"},
    ]

def test_edit_protects_state_and_cleans_pointer_checkpoints(tmp_path: Path):
    """编辑操作前物化保护 + 清理失效指针检查点, 回滚保护可撤销编辑"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-e", db_path=db, enable_checkpoint=True, auto_checkpoint=False)
    ctx.add_user_message("一")
    ctx.create_checkpoint(name="旧指针")
    ctx.add_user_message("二")

    ctx.del_context()

    cps = ctx.list_checkpoints()
    assert len(cps) == 1
    assert cps[0].source == "edit_protect"
    assert cps[0].snapshot_id != ""
    assert ctx.get_context() == []

    ctx.rollback(cps[0].checkpoint_id)
    # 回滚到编辑前保护检查点 = 撤销编辑
    assert len(ctx.get_context()) == 2

def test_pointer_checkpoint_after_edit_still_works(tmp_path: Path):
    """编辑后的新指针检查点基于新消息序列, 截断恢复准确"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-e2", db_path=db, enable_checkpoint=True, auto_checkpoint=False)
    ctx.add_user_message("旧一")
    ctx.del_context()   # 编辑 -> 保护 + 清空

    ctx.add_user_message("新一")
    cp = ctx.create_checkpoint(name="新起点")
    ctx.add_user_message("新二")

    ctx.rollback(cp.checkpoint_id)
    assert ctx.get_context() == [{"role": "user", "content": "新一"}]

def test_list_branches_escapes_wildcards(tmp_path: Path):
    """含下划线的会话前缀不再误匹配其他会话的分支"""
    db = str(tmp_path / "state.db")
    store = StateStore(db_path=db)
    store.create_checkpoint(StateScope("conversation", "conv_a:fork:x1"))
    store.create_checkpoint(StateScope("conversation", "convXa:fork:x1"))

    branches = store.list_branches("conv_a:fork:")
    assert [cp.scope_id for cp in branches] == ["conv_a:fork:x1"]

def test_session_create_checkpoint_compensates_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """中途失败时补偿删除残批, 不留部分作用域的不一致批次"""
    db = str(tmp_path / "chat_history.db")
    session = _MiniSession("s10", db)
    session.session_ctx.add_user_message("会话")
    session.wf_ctx.add_user_message("工作流")

    def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("模拟失败")

    monkeypatch.setattr(session.wf_ctx, "create_checkpoint", boom)
    with pytest.raises(RuntimeError, match="模拟失败"):
        session.create_checkpoint()

    assert session.list_checkpoints() == []

def test_auto_checkpoint_on_turn_messages(tmp_path: Path):
    """add_turn_messages 写入也触发自动检查点"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-t", db_path=db, enable_checkpoint=True)
    ctx.add_turn_messages([{"role": "user", "content": "批量"}])

    stables = [cp for cp in ctx.list_checkpoints() if cp.checkpoint_kind == "stable"]
    assert len(stables) == 1

def test_fork_inherits_auto_checkpoint_flag(tmp_path: Path):
    """fork 出的新上下文继承 auto_checkpoint 开关"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-f", db_path=db, enable_checkpoint=True, auto_checkpoint=False)
    ctx.add_user_message("x")
    ctx.create_checkpoint()

    forked = ctx.fork("alt")
    assert forked.auto_checkpoint is False

def test_rollback_batch_rejects_duplicate_scope(tmp_path: Path):
    """同批次同作用域多个检查点时明确拒绝, 不再半途崩溃"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-b", db_path=db, enable_checkpoint=True, auto_checkpoint=False)
    ctx.add_user_message("一")
    ctx.create_checkpoint(batch_id="dup")
    ctx.add_user_message("二")
    ctx.create_checkpoint(batch_id="dup")

    assert ctx.state_store is not None
    with pytest.raises(ValueError, match="同一作用域"):
        ctx.state_store.rollback_batch("dup")

def test_clear_memory_uses_session_db(tmp_path: Path):
    """clear_memory 清空自定义库中的工作流消息, 不落默认库"""
    db = str(tmp_path / "custom.db")
    session = _MiniSession("s-clear", db)
    session.wf_ctx.add_user_message("x")

    session.clear_memory()

    assert session.wf_ctx.get_context() == []
    assert session.session_ctx.get_context() == []

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM chat_history WHERE conversation_id = ?",
            (session.wf_ctx.conversation_id,),
        ).fetchone()
    finally:
        conn.close()
    assert rows[0] == 0

def test_clear_memory_clears_untracked_workflow_id(tmp_path: Path) -> None:
    """clear_memory 通过 wf_list 清理未显式注册的工作流上下文"""
    db_path = str(tmp_path / "untracked.db")
    session = Session("s-untracked", db_path=db_path)
    workflow_id = session.workflow_id_assign("edge")
    workflow_context = ContextManager(workflow_id, db_path=db_path)
    workflow_context.add_user_message("待清理消息")
    workflow_context.close()

    session.clear_memory()

    reloaded = ContextManager(workflow_id, db_path=db_path)
    try:
        assert reloaded.get_context() == []
    finally:
        reloaded.close()
        session.session_ctx.close()

@pytest.mark.asyncio
async def test_async_clear_memory_clears_untracked_workflow_id(tmp_path: Path) -> None:
    """异步 clear_memory 通过 wf_list 清理未显式注册的工作流上下文"""
    db_path = str(tmp_path / "async-untracked.db")
    session = AsyncSession("s-async-untracked", db_path=db_path)
    await session.initialize()
    workflow_id = session.workflow_id_assign("edge")
    workflow_context = AsyncContextManager(workflow_id, db_path=db_path)
    await workflow_context.initialize()
    await workflow_context.add_user_message("待清理消息")

    await session.clear_memory()

    reloaded = AsyncContextManager(workflow_id, db_path=db_path)
    await reloaded.initialize()
    assert reloaded.get_context() == []

def test_backend_config_bool_string_parsing():
    """字符串形式的 false/yes 正确解析为布尔"""
    cfg = BackendConfig.from_dict({"session_checkpoint": "false", "error_feedback": "yes"})
    assert cfg.session_checkpoint is False
    assert cfg.error_feedback is True
    cfg2 = BackendConfig.from_dict({"session_checkpoint": "1", "error_feedback": "0"})
    assert cfg2.session_checkpoint is True
    assert cfg2.error_feedback is False

def _make_server(db_path: str) -> BackendHTTPServer:
    # BackendManager 替身: 仅暴露 checkpoint_db_path, 边界断言集中在本工厂
    fake = SimpleNamespace(checkpoint_db_path=db_path)
    return BackendHTTPServer(cast(BackendManager, fake))

async def _route(
    server: BackendHTTPServer, method: str, path: str, body: bytes = b""
) -> tuple[int, dict[str, Any]]:
    return await server._route(method, path, body)

@pytest.mark.asyncio
async def test_checkpoint_api_full_flow(tmp_path: Path):
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-1", db_path=db, enable_checkpoint=True)
    ctx.add_user_message("你好")
    cp = ctx.create_checkpoint(name="开场")
    ctx.close()

    server = _make_server(db)

    status, data = await _route(server, "GET", "/api/checkpoints?conversation=conv-1")
    # 列表: 自动 stable + manual
    assert status == 200
    assert len(data["checkpoints"]) >= 2
    assert data["branches"] == []

    status, data = await _route(
        server, "POST", "/api/checkpoint/create",
        json.dumps({"conversation": "conv-1", "name": "第二个"}).encode(),
    )
    # 创建
    assert status == 200
    assert data["checkpoint_id"]

    status, data = await _route(server, "GET", "/api/checkpoint/audit?conversation=conv-1")
    # 审计
    assert status == 200
    assert len(data["mutations"]) >= 3

    status, data = await _route(
        server, "GET", f"/api/checkpoint/lineage?checkpoint_id={cp.checkpoint_id}"
    )
    # 血缘
    assert status == 200
    assert len(data["lineage"]) == 1

    status, data = await _route(
        server, "POST", "/api/checkpoint/fork",
        json.dumps({"conversation": "conv-1", "branch_name": "v2"}).encode(),
    )
    # fork (不指定检查点, 用最新)
    assert status == 200
    new_id = data["conversation_id"]
    assert ":fork:" in new_id

    status, data = await _route(server, "GET", "/api/checkpoints?conversation=conv-1")
    # 分支列表 (聚合接口)
    assert status == 200
    assert len(data["branches"]) == 1
    assert data["branches"][0]["scope_id"] == new_id

    conn = sqlite3.connect(db)
    # 分支检查点已存在
    rows = conn.execute(
        "SELECT COUNT(*) FROM state_checkpoints WHERE scope_id = ?", (new_id,)
    ).fetchone()
    conn.close()
    assert rows is not None and rows[0] >= 1

    status, _ = await _route(
        server, "POST", "/api/checkpoint/rollback",
        json.dumps({"conversation": "conv-1", "checkpoint_id": cp.checkpoint_id}).encode(),
    )
    # 回滚 + 重试
    assert status == 200
    status, _ = await _route(
        server, "POST", "/api/checkpoint/retry",
        json.dumps({"conversation": "conv-1", "checkpoint_id": cp.checkpoint_id}).encode(),
    )
    assert status == 200

    status, data = await _route(
        server, "POST", "/api/checkpoint/rollback",
        json.dumps({"conversation": "conv-1", "checkpoint_id": "manual-nope"}).encode(),
    )
    # 异常: 不存在的检查点
    assert status == 400

    status, _ = await _route(server, "GET", "/api/checkpoint/nope")
    # 异常: 未知子路由
    assert status == 404

@pytest.mark.asyncio
async def test_checkpoint_api_requires_conversation(tmp_path: Path):
    db = str(tmp_path / "chat_history.db")
    server = _make_server(db)
    status, data = await _route(
        server, "POST", "/api/checkpoint/create",
        json.dumps({"name": "无对话"}).encode(),
    )
    assert status == 400
    assert "error" in data

def _args(data_root: str, **overrides: Any) -> Namespace:
    base: dict[str, Any] = dict(
        action="list", conversation_id="conv-cli", checkpoint_id="",
        branch_name="", checkpoint="", name="", description="",
        platform_id="local", data_root=data_root,
    )
    base.update(overrides)
    return Namespace(**base)

def _seed_conv(db: str) -> str:
    """准备带一个检查点的对话, 返回检查点 ID"""
    ctx = ContextManager("conv-cli", db_path=db, enable_checkpoint=True)
    try:
        ctx.add_user_message("一")
        return ctx.create_checkpoint(name="起点").checkpoint_id
    finally:
        ctx.close()

def test_dispatch_business_error_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """业务错误 (检查点不存在) 以退出码 1 结束, 输出友好信息"""
    data_root = str(tmp_path / "data")
    db = str(StorageLayout(data_root).platform_db("local"))
    _seed_conv(db)

    args = _args(data_root, action="rollback", checkpoint_id="not-exist")
    with pytest.raises(SystemExit) as ei:
        dispatch(args)
    assert ei.value.code == 1
    assert "错误:" in capsys.readouterr().out

def test_dispatch_success_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """create / list / lineage 成功路径不抛异常"""
    data_root = str(tmp_path / "data")
    db = str(StorageLayout(data_root).platform_db("local"))
    cp_id = _seed_conv(db)

    dispatch(_args(data_root, action="list"))
    dispatch(_args(data_root, action="create", name="新检查点"))
    dispatch(_args(data_root, action="lineage", checkpoint_id=cp_id))
    capsys.readouterr()   # 吞掉输出
