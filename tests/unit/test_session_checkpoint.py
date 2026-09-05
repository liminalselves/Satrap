"""
Session 级聚合检查点 (P1) 单元测试

覆盖:
- workflow_id_assign 重复 wf_id 自动追加序号 + 警告
- Session.create_checkpoint / list_checkpoints / rollback 聚合语义
- Session.fork 对会话下全部上下文分支
- StateStore 批次 (batch_id) 能力: 查询与批量回滚
- AsyncSession 异步聚合检查点
"""
from pathlib import Path
import pytest
from typing import Any

from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap.core.state import StateStore


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


# ---------- workflow_id_assign 防呆 ----------


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


# ---------- 同步 Session 聚合检查点 ----------


def test_session_checkpoint_rollback_roundtrip(tmp_path: Path):
    """
    写消息 -> 会话级检查点 -> 继续写 -> 回滚还原全部上下文

    参数:
    - tmp_path: tmp路径
    """
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
    """
    rollback 也接受批次内单个检查点 ID

    参数:
    - tmp_path: tmp路径
    """
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
    """
    list_checkpoints 按批次去重, 每次聚合只出现一个代表检查点

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    session = _SimpleSession("s1", db)
    session.create_checkpoint(name="第一批")
    session.create_checkpoint(name="第二批")

    checkpoints = session.list_checkpoints()
    assert len(checkpoints) == 2
    assert checkpoints[0].name == "第一批[session]" or checkpoints[0].batch_id
    assert all(cp.batch_id for cp in checkpoints)


def test_session_fork_returns_all_contexts(tmp_path: Path):
    """
    fork 返回会话共享 + 全部工作流的新上下文, 数据独立

    参数:
    - tmp_path: tmp路径
    """
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
    """
    未启用检查点时聚合操作抛出 ValueError

    参数:
    - tmp_path: tmp路径
    """
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
    """
    工作流上下文与状态库不同库时明确报错, 防止静默不一致

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    session = _SimpleSession("s1", db)

    with pytest.raises(ValueError, match="不一致"):
        session._track_workflow_context("other", ContextManager("s1_other", db_path=str(tmp_path / "other.db")))


# ---------- StateStore 批次能力 ----------


def test_state_store_batch_rollback(tmp_path: Path):
    """
    同批次检查点共享 batch_id, 可一次性回滚

    参数:
    - tmp_path: tmp路径
    """
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
    """
    空批次 ID 应被拒绝 (单检查点请用 rollback)

    参数:
    - tmp_path: tmp路径
    """
    store = StateStore(db_path=str(tmp_path / "state.db"))
    with pytest.raises(ValueError, match="batch_id 不能为空"):
        store.rollback_batch("")
    with pytest.raises(ValueError, match="batch_id 不能为空"):
        store.retry_batch("")


def _user_checkpoints(checkpoints: list[Any]) -> list[Any]:
    """
    过滤系统保护检查点, 仅保留用户可见检查点

    参数:
    - checkpoints: 检查点列表

    返回:
    - list[Any]: 过滤系统保护检查点, 仅保留用户可见检查点
    """
    return [cp for cp in checkpoints if cp.source not in ("rollback_snapshot", "retry_snapshot", "edit_protect")]


# ---------- retry (保留未来检查点) ----------


def test_session_retry_keeps_future_checkpoints(tmp_path: Path):
    """
    retry 恢复数据但保留未来检查点, 与 rollback 语义区分

    参数:
    - tmp_path: tmp路径
    """
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
    """
    ContextManager.retry 恢复数据且未来检查点仍在

    参数:
    - tmp_path: tmp路径
    """
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


# ---------- 分支树查询 ----------


def test_state_store_lineage_and_branches(tmp_path: Path):
    """
    fork 血缘: trace_lineage 回溯父链, list_child_branches 找到子分支

    参数:
    - tmp_path: tmp路径
    """
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
    """
    Session.list_branches 聚合会话共享 + 工作流的分支

    参数:
    - tmp_path: tmp路径
    """
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
    """
    不存在的检查点回溯血缘时抛出 ValueError

    参数:
    - tmp_path: tmp路径
    """
    store = StateStore(db_path=str(tmp_path / "state.db"))
    with pytest.raises(ValueError, match="检查点不存在"):
        store.trace_lineage("missing-cp")


# ---------- stable 自动检查点 (消息写入后自动保存) ----------


def test_auto_checkpoint_creates_stable_on_write(tmp_path: Path):
    """
    启用检查点后, 消息写入自动产生 stable 检查点

    参数:
    - tmp_path: tmp路径
    """
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
    """
    auto_checkpoint=False 时消息写入不产生 stable 检查点

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager(
        "conv-manual", db_path=db, enable_checkpoint=True, auto_checkpoint=False
    )

    ctx.add_user_message("第一条")
    assert ctx.list_checkpoints() == []


def test_auto_checkpoint_rollback_restores_stable_state(tmp_path: Path):
    """
    stable 自动保存后, rollback 能回到消息中间水位

    参数:
    - tmp_path: tmp路径
    """
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
    """
    异步上下文消息写入自动产生 stable 检查点

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = AsyncContextManager("conv-async-auto", db_path=db, enable_checkpoint=True)
    await ctx.initialize()

    await ctx.add_user_message("第一条")

    checkpoints = await ctx.list_checkpoints()
    assert len([cp for cp in checkpoints if cp.checkpoint_kind == "stable"]) == 1


# ---------- 变更审计 (ledger) ----------


def test_list_mutations_records_source_and_reason(tmp_path: Path):
    """
    list_mutations 按时间倒序返回检查点, 含 source / reason 审计字段

    参数:
    - tmp_path: tmp路径
    """
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
    # 倒序: 最后创建的 fork 在前


def test_store_list_mutations_filters_by_scope(tmp_path: Path):
    """
    StateStore.list_mutations 仅返回指定作用域的变更记录

    参数:
    - tmp_path: tmp路径
    """
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
    """
    Session.list_mutations 聚合会话共享 + 工作流的变更记录

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    session = _SimpleSession("s1", db)
    session.session_ctx.add_user_message("会话消息")
    session.wf_ctx.add_user_message("工作流消息")
    session.create_checkpoint()

    mutations = session.list_mutations()
    # 会话共享 + 工作流各 1 个聚合检查点
    assert len([m for m in mutations if m.batch_id]) == 2
    assert {m.scope_id for m in mutations if m.batch_id} == {"s1", "s1_main"}


# ---------- 异步 Session 聚合检查点 ----------


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
