"""
P1-P3 审查缺陷修复回归测试

覆盖:
- H1: 旧库缺 source/reason 列自动迁移, 读取不崩溃
- H2: Session.fork 对非批次检查点不再静默空结果
- M1: 指针式检查点零快照 + stable 不膨胀 + 指针截断恢复 + 编辑保护
- M2: list_branches LIKE 通配符转义
- M3: Session.create_checkpoint 中途失败补偿残批
- M4: auto_checkpoint 钩子补齐 (add_turn_messages) + fork 透传开关
- L1: rollback_batch 同作用域重复检查点拒绝
- L4: clear_memory 使用会话自定义库
- L6: BackendConfig 字符串布尔解析
"""
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.state import StateStore
from satrap.core.type import StateScope
from satrap.core.utils.context import AsyncContextManager, ContextManager


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


# ---------- H1: 旧库迁移 ----------


def test_old_db_migration_adds_missing_columns(tmp_path: Path):
    """
    P3-2 之前的旧库 (无 source/reason 列) 初始化后自动迁移, 读取不崩溃

    参数:
    - tmp_path: tmp路径
    """
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


# ---------- H2: Session.fork 非批次检查点回退 ----------


def test_session_fork_non_batch_checkpoint_not_silent(tmp_path: Path):
    """
    非批次检查点 fork 不再静默空: 有检查点的上下文正常分支

    参数:
    - tmp_path: tmp路径
    """
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


# ---------- M1: 指针式检查点 ----------


def test_pointer_checkpoint_stores_no_snapshot(tmp_path: Path):
    """
    stable 检查点为纯指针 (快照表零增长), 写入 20 次不膨胀快照

    参数:
    - tmp_path: tmp路径
    """
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
    """
    指针检查点回滚 = 截断到水位, 消息内容准确恢复

    参数:
    - tmp_path: tmp路径
    """
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
    """
    编辑操作前物化保护 + 清理失效指针检查点, 回滚保护可撤销编辑

    参数:
    - tmp_path: tmp路径
    """
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
    """
    编辑后的新指针检查点基于新消息序列, 截断恢复准确

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-e2", db_path=db, enable_checkpoint=True, auto_checkpoint=False)
    ctx.add_user_message("旧一")
    ctx.del_context()   # 编辑 -> 保护 + 清空

    ctx.add_user_message("新一")
    cp = ctx.create_checkpoint(name="新起点")
    ctx.add_user_message("新二")

    ctx.rollback(cp.checkpoint_id)
    assert ctx.get_context() == [{"role": "user", "content": "新一"}]


# ---------- M2: list_branches 通配符转义 ----------


def test_list_branches_escapes_wildcards(tmp_path: Path):
    """
    含下划线的会话前缀不再误匹配其他会话的分支

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "state.db")
    store = StateStore(db_path=db)
    store.create_checkpoint(StateScope("conversation", "conv_a:fork:x1"))
    store.create_checkpoint(StateScope("conversation", "convXa:fork:x1"))

    branches = store.list_branches("conv_a:fork:")
    assert [cp.scope_id for cp in branches] == ["conv_a:fork:x1"]


# ---------- M3: 聚合检查点失败补偿 ----------


def test_session_create_checkpoint_compensates_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    中途失败时补偿删除残批, 不留部分作用域的不一致批次

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
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


# ---------- M4: auto_checkpoint 钩子补齐 + fork 透传 ----------


def test_auto_checkpoint_on_turn_messages(tmp_path: Path):
    """
    add_turn_messages 写入也触发自动检查点

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-t", db_path=db, enable_checkpoint=True)
    ctx.add_turn_messages([{"role": "user", "content": "批量"}])

    stables = [cp for cp in ctx.list_checkpoints() if cp.checkpoint_kind == "stable"]
    assert len(stables) == 1


def test_fork_inherits_auto_checkpoint_flag(tmp_path: Path):
    """
    fork 出的新上下文继承 auto_checkpoint 开关

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-f", db_path=db, enable_checkpoint=True, auto_checkpoint=False)
    ctx.add_user_message("x")
    ctx.create_checkpoint()

    forked = ctx.fork("alt")
    assert forked.auto_checkpoint is False


# ---------- L1: 批次作用域重复拒绝 ----------


def test_rollback_batch_rejects_duplicate_scope(tmp_path: Path):
    """
    同批次同作用域多个检查点时明确拒绝, 不再半途崩溃

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-b", db_path=db, enable_checkpoint=True, auto_checkpoint=False)
    ctx.add_user_message("一")
    ctx.create_checkpoint(batch_id="dup")
    ctx.add_user_message("二")
    ctx.create_checkpoint(batch_id="dup")

    assert ctx.state_store is not None
    with pytest.raises(ValueError, match="同一作用域"):
        ctx.state_store.rollback_batch("dup")


# ---------- L4: clear_memory 使用会话库 ----------


def test_clear_memory_uses_session_db(tmp_path: Path):
    """
    clear_memory 清空自定义库中的工作流消息, 不落默认库

    参数:
    - tmp_path: tmp路径
    """
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
    """
    clear_memory 通过 wf_list 清理未显式注册的工作流上下文

    参数:
    - tmp_path: 临时目录
    """
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
    """
    异步 clear_memory 通过 wf_list 清理未显式注册的工作流上下文

    参数:
    - tmp_path: 临时目录
    """
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


# ---------- L6: BackendConfig 字符串布尔解析 ----------


def test_backend_config_bool_string_parsing():
    """字符串形式的 false/yes 正确解析为布尔"""
    cfg = BackendConfig.from_dict({"session_checkpoint": "false", "error_feedback": "yes"})
    assert cfg.session_checkpoint is False
    assert cfg.error_feedback is True
    cfg2 = BackendConfig.from_dict({"session_checkpoint": "1", "error_feedback": "0"})
    assert cfg2.session_checkpoint is True
    assert cfg2.error_feedback is False
