"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import pytest
from typing import Any, cast
from satrap.core.state.mutation import current_mutation_context, state_mutation_context
from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap.core.state import StateStore
from satrap.core.type import JsonRow, RestoreOptions, SnapshotDomain, StateScope
from satrap.core.framework.Base.execution.store import RunStore, RunConflictError
from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.framework.Base.execution.engine import run_sync, run_async
from satrap.core.framework.Base.execution.store import RunNeedsAttention
from satrap.core.framework.Base import ModelWorkflowFramework, AsyncModelWorkflowFramework
from satrap.core.type import LLMCallResponse
from satrap.core.utils.TCBuilder import Tool
from satrap.core.framework.Base.execution.flow import agent_flow, ModelStep, ToolStep
import copy
import json
from satrap.core.framework.Base.execution import codec
from satrap.core.storage.database import snapshot_session_domain, delete_session_domain_rows, restore_session_domain
from types import SimpleNamespace
from satrap.core.utils.skills.tool import SkillTool
from satrap.expend.plugins.base_take import tools as base_tools
from satrap.expend.plugins.memory import tools as memory_tools
from satrap.expend.plugins.satrap_coding import tools as coding_tools
from satrap.expend.plugins.rag import tools as rag_tools
from satrap.expend.tools import search, sandbox_tools, agent
from satrap import AsyncSimpleSession

def _kv_domain() -> SnapshotDomain:
    """KV 领域: 用于测试的简单键值数据 (含引用字段)"""
    def builder(conn: sqlite3.Connection, scope: StateScope) -> list[JsonRow]:
        """读取作用域下的全部 KV 行"""
        rows = conn.execute(
            "SELECT id, key, value, ref_col FROM kv_data "
            "WHERE scope_id = ? ORDER BY id",
            (scope.scope_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def restorer(
        conn: sqlite3.Connection,
        scope: StateScope,
        rows: list[JsonRow],
        options: RestoreOptions,
    ) -> None:
        """清空后重写 KV 行"""
        cleaner(conn, scope)
        for row in rows:
            conn.execute(
                "INSERT INTO kv_data (scope_id, key, value, ref_col) "
                "VALUES (?, ?, ?, ?)",
                (
                    scope.scope_id,
                    str(row.get("key") or ""),
                    str(row.get("value") or ""),
                    row.get("ref_col"),
                ),
            )

    def cleaner(conn: sqlite3.Connection, scope: StateScope) -> None:
        """清空作用域下的全部 KV 行"""
        conn.execute("DELETE FROM kv_data WHERE scope_id = ?", (scope.scope_id,))

    def position_provider(conn: sqlite3.Connection, scope: StateScope) -> int:
        """提供水位: 当前作用域最大行 ID"""
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS position FROM kv_data WHERE scope_id = ?",
            (scope.scope_id,),
        ).fetchone()
        return int(row["position"])

    return SnapshotDomain(
        name="kv",
        builder=builder,
        restorer=restorer,
        cleaner=cleaner,
        reference_fields=("ref_col",),
        position_provider=position_provider,
    )

def _write_kv(db_path: str, scope_id: str, key: str, value: str, ref_col: str | None = None) -> None:
    """写入一条 KV 数据 (模拟业务侧写入)"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO kv_data (scope_id, key, value, ref_col) VALUES (?, ?, ?, ?)",
            (scope_id, key, value, ref_col),
        )
        conn.commit()
    finally:
        conn.close()

def _read_kv(db_path: str, scope_id: str) -> list[dict[str, Any]]:
    """读取作用域下的全部 KV 数据"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT key, value, ref_col FROM kv_data WHERE scope_id = ? ORDER BY id",
            (scope_id,),
        ).fetchall()
        return [cast(dict[str, Any], dict(row)) for row in rows]
    finally:
        conn.close()

@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    """构建带 KV 领域的 StateStore (与数据同库)"""
    db_path = str(tmp_path / "state.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE kv_data ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "scope_id TEXT NOT NULL, key TEXT NOT NULL, "
        "value TEXT NOT NULL, ref_col TEXT)"
    )
    conn.commit()
    conn.close()
    s = StateStore(db_path=db_path)
    s.register_domain(_kv_domain())
    return s

class TestCheckpointCRUD:
    def test_create_and_list(self, store: StateStore):
        scope = StateScope("test", "conv-1")
        _write_kv(str(store.db_path), "conv-1", "a", "1")
        cp1 = store.create_checkpoint(scope, name="起点")
        cp2 = store.create_checkpoint(scope, name="中期")

        checkpoints = store.list_checkpoints(scope)
        assert len(checkpoints) == 2
        assert checkpoints[0].checkpoint_id == cp1.checkpoint_id
        assert checkpoints[1].state_revision == 2
        assert checkpoints[1].position >= 1
        assert checkpoints[1].checkpoint_kind == "manual"

    def test_get_and_delete(self, store: StateStore):
        scope = StateScope("test", "conv-2")
        cp = store.create_checkpoint(scope, name="唯一")

        assert store.get_checkpoint(cp.checkpoint_id) is not None
        assert store.get_checkpoint("not-exist") is None

        assert store.delete_checkpoint(cp.checkpoint_id) is True
        assert store.delete_checkpoint(cp.checkpoint_id) is False
        assert store.list_checkpoints(scope) == []

    def test_scope_isolation(self, store: StateStore):
        _write_kv(str(store.db_path), "conv-a", "a", "1")
        _write_kv(str(store.db_path), "conv-b", "b", "2")
        store.create_checkpoint(StateScope("test", "conv-a"))
        store.create_checkpoint(StateScope("test", "conv-b"))

        assert len(store.list_checkpoints(StateScope("test", "conv-a"))) == 1
        assert len(store.list_checkpoints(StateScope("test", "conv-b"))) == 1
        assert len(store.list_checkpoints(StateScope("test", "conv-c"))) == 0

class TestRollback:
    def test_rollback_restores_data(self, store: StateStore):
        scope = StateScope("test", "conv-1")
        _write_kv(str(store.db_path), "conv-1", "a", "1")
        cp = store.create_checkpoint(scope, name="起点", materialize=True)

        _write_kv(str(store.db_path), "conv-1", "b", "2")
        _write_kv(str(store.db_path), "conv-1", "c", "3")

        store.rollback(cp.checkpoint_id)

        rows = _read_kv(str(store.db_path), "conv-1")
        assert [row["key"] for row in rows] == ["a"]
        assert rows[0]["value"] == "1"

    def test_rollback_cleans_future_checkpoints(self, store: StateStore):
        scope = StateScope("test", "conv-1")
        _write_kv(str(store.db_path), "conv-1", "a", "1")
        cp1 = store.create_checkpoint(scope, name="起点", materialize=True)

        _write_kv(str(store.db_path), "conv-1", "b", "2")
        cp2 = store.create_checkpoint(scope, name="中期", materialize=True)

        store.rollback(cp1.checkpoint_id)

        checkpoints = store.list_checkpoints(scope)
        # 未来检查点被清理, 但回滚前保护检查点保留 (可撤销)
        assert store.get_checkpoint(cp2.checkpoint_id) is None
        remaining = [cp for cp in checkpoints if cp.source != "rollback_snapshot"]
        assert [cp.checkpoint_id for cp in remaining] == [cp1.checkpoint_id]
        assert any(cp.source == "rollback_snapshot" for cp in checkpoints)
        # 未来检查点的快照应被级联清理
        assert store.get_checkpoint(cp2.checkpoint_id) is None

    def test_rollback_unknown_checkpoint_raises(self, store: StateStore):
        with pytest.raises(ValueError, match="检查点不存在"):
            store.rollback("not-exist")

    def test_rollback_records_mutation_source(self, store: StateStore):
        scope = StateScope("test", "conv-1")
        _write_kv(str(store.db_path), "conv-1", "a", "1")
        cp = store.create_checkpoint(scope, materialize=True)

        with state_mutation_context(source="checkpoint_rollback", reason="测试回滚"):
            store.rollback(cp.checkpoint_id)

        cp2 = store.create_checkpoint(scope)
        # 回滚后重新创建检查点, 审计字段应记录上下文来源
        conn = sqlite3.connect(str(store.db_path))
        try:
            row = conn.execute(
                "SELECT source, reason FROM state_checkpoints WHERE checkpoint_id = ?",
                (cp2.checkpoint_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "manual"
        # 原检查点仍保留创建时的来源
        conn = sqlite3.connect(str(store.db_path))
        try:
            row = conn.execute(
                "SELECT source FROM state_checkpoints WHERE checkpoint_id = ?",
                (cp.checkpoint_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "manual"

class TestStableCheckpoint:
    def test_stable_dedup_by_position(self, store: StateStore):
        scope = StateScope("test", "conv-1")
        _write_kv(str(store.db_path), "conv-1", "a", "1")

        stable1 = store.ensure_stable_checkpoint(scope)
        stable2 = store.ensure_stable_checkpoint(scope)
        assert stable1 is not None
        assert stable2 is not None
        assert stable1.checkpoint_id == stable2.checkpoint_id
        assert len(store.list_checkpoints(scope)) == 1

        _write_kv(str(store.db_path), "conv-1", "b", "2")
        # 水位变化后创建新的稳定检查点
        stable3 = store.ensure_stable_checkpoint(scope)
        assert stable3 is not None
        assert stable3.checkpoint_id != stable1.checkpoint_id
        assert len(store.list_checkpoints(scope)) == 2

    def test_stable_skips_empty_scope(self, store: StateStore):
        assert store.ensure_stable_checkpoint(StateScope("test", "empty")) is None

class TestFork:
    def test_fork_creates_independent_scope(self, store: StateStore):
        scope = StateScope("test", "conv-1")
        _write_kv(str(store.db_path), "conv-1", "a", "1")
        cp = store.create_checkpoint(scope, name="起点", materialize=True)
        _write_kv(str(store.db_path), "conv-1", "b", "2")

        new_scope = store.fork(cp.checkpoint_id, "conv-1:fork:bad_end")

        assert new_scope.scope_id == "conv-1:fork:bad_end"
        # 新分支只有检查点时的数据
        rows = _read_kv(str(store.db_path), "conv-1:fork:bad_end")
        assert [row["key"] for row in rows] == ["a"]
        # 父作用域数据不受影响
        assert len(_read_kv(str(store.db_path), "conv-1")) == 2
        # 新分支自动生成起点检查点, 记录父检查点
        fork_cps = store.list_checkpoints(new_scope)
        assert len(fork_cps) == 1
        assert fork_cps[0].parent_checkpoint_id == cp.checkpoint_id

    def test_fork_remaps_reference_fields(self, store: StateStore):
        scope = StateScope("test", "conv-1")
        _write_kv(str(store.db_path), "conv-1", "a", "1", ref_col="call-123")
        cp = store.create_checkpoint(scope, materialize=True)

        store.fork(cp.checkpoint_id, "conv-1:fork:alt")

        rows = _read_kv(str(store.db_path), "conv-1:fork:alt")
        assert rows[0]["ref_col"] == "conv-1:fork:alt:call-123"
        # 父作用域引用保持不变
        assert _read_kv(str(store.db_path), "conv-1")[0]["ref_col"] == "call-123"

    def test_fork_into_existing_scope_raises(self, store: StateStore):
        scope = StateScope("test", "conv-1")
        _write_kv(str(store.db_path), "conv-1", "a", "1")
        cp = store.create_checkpoint(scope)
        # 目标作用域已有数据
        _write_kv(str(store.db_path), "conv-1:fork:dup", "x", "9")

        with pytest.raises(ValueError, match="已存在数据"):
            store.fork(cp.checkpoint_id, "conv-1:fork:dup")

class TestSnapshotValidation:
    def test_unsupported_snapshot_version_raises(self, store: StateStore):
        scope = StateScope("test", "conv-1")
        _write_kv(str(store.db_path), "conv-1", "a", "1")
        cp = store.create_checkpoint(scope, materialize=True)

        conn = sqlite3.connect(str(store.db_path))
        # 手工篡改快照版本
        try:
            conn.execute(
                "UPDATE state_snapshots SET snapshot_json = ? WHERE snapshot_id = ?",
                ('{"snapshot_version": 999}', cp.snapshot_id),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(ValueError, match="不支持的状态快照版本"):
            store.rollback(cp.checkpoint_id)

class TestMutationContext:
    def test_context_readable_inside_scope(self):
        with state_mutation_context(source="test-source", reason="测试原因") as ctx:
            assert ctx.source == "test-source"
            assert ctx.reason == "测试原因"
            assert len(ctx.change_set_id) == 32
            assert current_mutation_context() is ctx

        assert current_mutation_context() is None
        # 退出作用域后恢复为空

    def test_create_checkpoint_records_context(self, store: StateStore):
        scope = StateScope("test", "conv-1")
        _write_kv(str(store.db_path), "conv-1", "a", "1")

        with state_mutation_context(source="user_confirm", reason="用户手动保存"):
            store.create_checkpoint(scope)

        conn = sqlite3.connect(str(store.db_path))
        try:
            row = conn.execute(
                "SELECT source, reason FROM state_checkpoints LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "user_confirm"
        assert row[1] == "用户手动保存"

class TestContextManagerCheckpoint:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("asynchronous", [False, True])
    async def test_checkpoint_rollback_roundtrip(self, tmp_path: Path, asynchronous: bool):
        """写消息 -> 检查点 -> 继续写 -> 回滚还原 (同步/异步)"""
        if asynchronous:
            ctx = AsyncContextManager(
                "demo", db_path=str(tmp_path / "chat_history.db"), enable_checkpoint=True
            )
            await ctx.initialize()
            await ctx.add_user_message("你好")
            await ctx.add_bot_message("你好呀")
            cp = await ctx.create_checkpoint(name="起点")
            await ctx.add_user_message("第二句")
            await ctx.add_bot_message("第二句回复")
            assert len(ctx.get_context()) == 4
            await ctx.rollback(cp.checkpoint_id)
        else:
            ctx = ContextManager(
                "demo", db_path=str(tmp_path / "chat_history.db"), enable_checkpoint=True
            )
            ctx.add_user_message("你好")
            ctx.add_bot_message("你好呀")
            cp = ctx.create_checkpoint(name="起点")
            ctx.add_user_message("第二句")
            ctx.add_bot_message("第二句回复")
            assert len(ctx.get_context()) == 4
            ctx.rollback(cp.checkpoint_id)

        assert len(ctx.get_context()) == 2
        assert ctx.get_context()[0]["content"] == "你好"
        assert ctx.get_context()[1]["content"] == "你好呀"

    def test_fork_returns_new_context(self, tmp_path: Path):
        """从检查点 fork 新剧情线, 父对话保持完整"""
        ctx = ContextManager(
            "demo",
            db_path=str(tmp_path / "chat_history.db"),
            enable_checkpoint=True,
            auto_checkpoint=False,
        )
        ctx.add_user_message("你好")
        ctx.add_bot_message("你好呀")
        ctx.create_checkpoint(name="起点")
        ctx.add_user_message("坏结局")

        new_ctx = ctx.fork("bad_end")

        assert new_ctx.conversation_id == "demo:fork:bad_end"
        assert len(new_ctx.get_context()) == 2
        assert new_ctx.get_context()[1]["content"] == "你好呀"
        # 父对话保持完整
        assert len(ctx.get_context()) == 3
        # 新分支可独立继续写
        new_ctx.add_user_message("新分支第一句")
        assert len(new_ctx.get_context()) == 3

    def test_checkpoint_disabled_raises(self, tmp_path: Path):
        ctx = ContextManager("demo", db_path=str(tmp_path / "chat_history.db"))
        with pytest.raises(ValueError, match="未启用状态检查点"):
            ctx.create_checkpoint()

    def test_fork_without_checkpoint_raises(self, tmp_path: Path):
        ctx = ContextManager(
            "demo",
            db_path=str(tmp_path / "chat_history.db"),
            enable_checkpoint=True,
            auto_checkpoint=False,
        )
        ctx.add_user_message("没有检查点")
        with pytest.raises(ValueError, match="没有检查点"):
            ctx.fork("bad_end")

class TestAsyncContextManagerCheckpoint:
    async def test_fork_returns_initialized_context(self, tmp_path: Path):
        """异步版: fork 返回已初始化新分支, 父对话保持完整"""
        ctx = AsyncContextManager(
            "demo",
            db_path=str(tmp_path / "chat_history.db"),
            enable_checkpoint=True,
            auto_checkpoint=False,
        )
        await ctx.initialize()
        await ctx.add_user_message("你好")
        await ctx.add_bot_message("你好呀")
        await ctx.create_checkpoint(name="起点")
        await ctx.add_user_message("坏结局")

        new_ctx = await ctx.fork("bad_end")

        assert new_ctx.conversation_id == "demo:fork:bad_end"
        assert len(new_ctx.get_context()) == 2
        assert new_ctx.get_context()[1]["content"] == "你好呀"
        # 父对话保持完整
        assert len(ctx.get_context()) == 3

def _must_step(store: RunStore, run_id: str, key: str) -> dict[str, Any]:
    """读取必须存在的步骤记录"""
    step = store.step(run_id, key)
    if step is None:
        raise AssertionError(f"步骤不存在: {run_id}/{key}")
    return step

def test_reopen_preserves_completed_and_uncertain_steps(tmp_path):
    store = RunStore(tmp_path / "platform.db", "session:main")
    with store.claim():
        run = store.create({"text": "test"}, "context", "config")
        store.start_step(run, "model:0", "model", {"messages": []}, "retry")
        store.finish_step(run, "model:0", {"content": "", "tools": ["write"]})
        store.start_step(run, "tool:0:0", "tool", {"name": "write"})
    reopened = RunStore(tmp_path / "platform.db", "session:main")
    assert _must_step(reopened, run, "model:0")["status"] == "completed"
    assert _must_step(reopened, run, "tool:0:0")["status"] == "running"
    assert _must_step(reopened, run, "tool:0:0")["recovery_policy"] == "manual"

def test_scope_isolation_and_nonreentrant_claim(tmp_path):
    store = RunStore(tmp_path / "platform.db", "a")
    other = RunStore(tmp_path / "platform.db", "b")
    with store.claim():
        run = store.create({}, "c", "f")
        with pytest.raises(RunConflictError), store.claim():
            pass
        with other.claim():
            other.create({}, "c", "f")
    with pytest.raises(RunConflictError):
        other.get(run)
    with pytest.raises(RunConflictError):
        other.update(run, status="cancelled")
    with pytest.raises(RunConflictError):
        other.step(run, "model:0")

def test_explicit_retry_only_for_uncertain_tool(tmp_path):
    store = RunStore(tmp_path / "platform.db", "a")
    run = store.create({}, "c", "f")
    store.start_step(run, "tool:0:0", "tool", {"name": "write"})
    with pytest.raises(RunConflictError):
        store.authorize_retry(run, "tool:0:0")
    store.update(run, status="needs_attention")
    store.authorize_retry(run, "tool:0:0")
    assert _must_step(store, run, "tool:0:0")["recovery_policy"] == "retry"
    store.abort(run)
    assert store.get(run)["status"] == "cancelled"

def test_result_must_be_serializable_without_destroying_step(tmp_path):
    store = RunStore(tmp_path / "platform.db", "a")
    run = store.create({}, "c", "f")
    store.start_step(run, "tool:0", "tool", {})
    with pytest.raises(TypeError):
        store.finish_step(run, "tool:0", object())
    assert _must_step(store, run, "tool:0")["status"] == "running"

def test_messages_and_completion_commit_together(tmp_path):
    from satrap.core.utils.context import ContextManager

    path = tmp_path / "platform.db"
    context = ContextManager("a", db_path=str(path))
    store = RunStore(path, "a")
    try:
        run = store.create({}, store.history_signature(), "config")
        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}]
        store.commit_messages(run, messages, "world")
        store.commit_messages(run, messages, "world")
        context.load_context()
        assert [item["content"] for item in context.get_context()] == ["hello", "world"]
        assert store.get(run)["status"] == "completed"
        other = store.create({}, store.history_signature(), "config")
        context.add_user_message("new input")
        with pytest.raises(RunConflictError):
            store.commit_messages(other, messages, "world")
        assert store.get(other)["status"] == "running"
    finally:
        context.close()

def test_commit_failure_rolls_back_messages_and_status(tmp_path):
    from satrap.core.utils.context import ContextManager

    path = tmp_path / "platform.db"
    context = ContextManager("a", db_path=str(path))
    store = RunStore(path, "a")
    try:
        run = store.create({}, store.history_signature(), "config")
        with pytest.raises(KeyError):
            store.commit_messages(run, [{"role": "user", "content": "hello"}, {}], "world")
        context.load_context()
        assert context.get_context() == []
        assert store.get(run)["status"] == "running"
    finally:
        context.close()

def test_os_lock_released_after_process_exit(tmp_path):
    import os
    import subprocess
    import sys

    database = tmp_path / "platform.db"
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,os; from satrap.core.framework.Base.execution.store import RunStore; "
         "s=RunStore(sys.argv[1],'a'); c=s.claim(); c.__enter__(); "
         "print('ready',flush=True); sys.stdin.readline(); os._exit(0)", str(database)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    try:
        child_stdout = child.stdout
        if child_stdout is None:
            raise AssertionError("子进程 stdout 未重定向")
        assert child_stdout.readline().strip() == "ready"
        store = RunStore(database, "a")
        with pytest.raises(RunConflictError), store.claim():
            pass
        child.communicate("exit\n", timeout=15)
        assert child.returncode == 0
        with store.claim():
            store.create({}, "context", "config")
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=15)

def test_archive_restores_run_steps_and_rejects_foreign_run(tmp_path):
    from satrap.core.storage.database import snapshot_session_domain, delete_session_domain_rows, restore_session_domain

    database = tmp_path / "platform.db"
    store = RunStore(database, "a_main")
    run = store.create({}, "context", "config")
    store.start_step(run, "tool:0", "tool", {})
    snapshot = snapshot_session_domain(database, "a")
    delete_session_domain_rows(database, "a")
    assert store.list() == []
    restore_session_domain(database, "a", snapshot)
    assert _must_step(store, run, "tool:0")["status"] == "running"
    delete_session_domain_rows(database, "a")
    snapshot["agent_steps"][0]["run_id"] = "foreign"
    with pytest.raises(ValueError):
        restore_session_domain(database, "a", snapshot)

class Crash(BaseException):
    pass

class Model(LLM):
    def __init__(self):
        super().__init__(api_key="test", model="offline")
        self.calls = 0

    def call(self, messages, **kwargs):
        self.calls += 1
        if not any(message.get("role") == "tool" for message in messages):
            return LLMCallResponse("tools_call", "", tool_calls=[{"id": "c1", "name": "write", "arguments": {}}])
        return LLMCallResponse("message", "done")

class Writer(Tool):
    tool_name = "write"
    description = "写入测试记录"
    params_dict = {}

    def __init__(self):
        super().__init__()
        self.calls = 0

    def execute(self):
        self.calls += 1
        if self.calls == 1:
            raise Crash("工具已经发生副作用")
        return "written"

def test_uncertain_tool_requires_explicit_retry_and_reuses_model(tmp_path):
    model = Model()
    wf = ModelWorkflowFramework(model, "test", db_path=str(tmp_path / "p.db"))
    tool = Writer()
    wf.tools_manager.register_tool(tool)
    try:
        with pytest.raises(Crash):
            run_sync(wf, user_input="write", callback=False)
        run = wf.last_run_id
        if run is None:
            raise AssertionError("缺少运行记录")
        store = RunStore(wf.ctx.db_path, wf.ctx.conversation_id)
        assert model.calls == 1
        with pytest.raises(RunNeedsAttention):
            run_sync(wf, run_id=run, callback=False)
        assert tool.calls == 1
        store.authorize_retry(run, "tool:0:0")
        assert run_sync(wf, run_id=run, callback=False) == "done"
        assert model.calls == 2
        assert tool.calls == 2
        assert run_sync(wf, run_id=run, callback=False) == "done"
        assert model.calls == 2
        assert len([m for m in wf.ctx.get_context() if m["role"] == "user"]) == 1
    finally:
        wf.ctx.close()

@pytest.mark.asyncio
async def test_async_resume_reuses_saved_request(tmp_path):
    class AsyncModel(AsyncLLM):
        def __init__(self):
            super().__init__(api_key="test", model="offline")
            self.calls = 0

        async def call(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise Crash("中断")
            return LLMCallResponse("message", "done")

    model = AsyncModel()
    wf = AsyncModelWorkflowFramework(model, "test", db_path=str(tmp_path / "p.db"))
    await wf.initialize()
    with pytest.raises(Crash):
        await run_async(wf, user_input="hello", callback=False)
    assert await run_async(wf, run_id=wf.last_run_id, callback=False) == "done"
    assert wf.ctx.get_context()[-1]["content"] == "done"

@pytest.mark.parametrize("change", ["history", "model"])
def test_resume_rejects_changed_inputs_before_tool_execution(tmp_path, change):
    model = Model()
    wf = ModelWorkflowFramework(model, "test", db_path=str(tmp_path / "p.db"))
    tool = Writer()
    wf.tools_manager.register_tool(tool)
    try:
        with pytest.raises(Crash):
            run_sync(wf, user_input="write", callback=False)
        if change == "history":
            wf.ctx.add_user_message("another turn")
        else:
            model.model = "different"
        with pytest.raises(RunConflictError):
            run_sync(wf, run_id=wf.last_run_id, callback=False)
        assert model.calls == tool.calls == 1
    finally:
        wf.ctx.close()

@pytest.mark.asyncio
async def test_async_session_cancel_is_terminal(tmp_path):
    import asyncio
    from satrap import AsyncSimpleSession

    class CancelModel(AsyncLLM):
        def __init__(self):
            super().__init__(api_key="test", model="offline")

        async def call(self, messages, **kwargs):
            raise asyncio.CancelledError()

    session = AsyncSimpleSession("cancel", CancelModel(), db_path=str(tmp_path / "p.db"), recoverable=True)
    with pytest.raises(asyncio.CancelledError):
        await session.run("hello")
    runs = await session.list_runs()
    assert runs[0]["status"] == "cancelled"
    with pytest.raises(RunConflictError):
        await session.resume_run(runs[0]["id"])

def test_sync_stream_and_completed_replay_preserve_usage(tmp_path):
    from satrap.core.type import LLMCallStreamEvent, TokenUsage

    class StreamingModel(LLM):
        def __init__(self):
            super().__init__(api_key="test", model="offline")
            self.calls = 0

        def stream_call(self, messages, **kwargs):
            self.calls += 1
            yield LLMCallStreamEvent("content_delta", delta="done")
            yield LLMCallStreamEvent("done", response=LLMCallResponse("message", "done", usage=TokenUsage(10, 2, 12)))

    model = StreamingModel()
    wf = ModelWorkflowFramework(model, "test", db_path=str(tmp_path / "p.db"), recoverable=True)
    try:
        assert wf.stream_full_agent("hello", callback=False) == "done"
        original = wf.get_context_stats()
        assert original is not None
        assert run_sync(wf, run_id=wf.last_run_id, callback=False) == "done"
        assert wf.get_context_stats() == original
        assert model.calls == 1
    finally:
        wf.ctx.close()

def test_flow_reconstructs_tool_transcript():
    flow = agent_flow("hello", 2)
    first = next(flow)
    assert isinstance(first, ModelStep)
    call = {"id": "call-1", "name": "search", "arguments": {}}
    step = flow.send({"type": "tools_call", "tool_calls": [call], "content": ""})
    assert isinstance(step, ToolStep)
    step = flow.send((call, {"role": "tool", "tool_call_id": "call-1", "content": "found"}))
    assert isinstance(step, ModelStep)
    assert step.key == "model:1"
    with pytest.raises(StopIteration) as ended:
        flow.send({"type": "text", "content": "answer"})
    messages, answer = ended.value.value
    assert answer == "answer"
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[-1]["content"] == "answer"

def request(messages):
    return {"messages": messages, "tools": [{"function": {"name": "read"}}], "thinking": "high", "img_urls": ["https://example.test/image"], "_prepared": {"model": "test"}}

def test_exact_requests_across_append_truncation_summary_and_long_chain(tmp_path):
    store = RunStore(tmp_path / "p.db", "a")
    run = store.create({}, "c", "f")
    messages = [{"role": "system", "content": "系统" * 1000}, {"role": "user", "content": [{"type": "text", "text": "问题"}, {"type": "image_url", "image_url": {"url": "https://example.test/image"}}]}]
    expected = []
    for index in range(20):
        if index == 12:
            messages = [{"role": "system", "content": "摘要"}, *messages[-2:]]
        messages = [*messages, {"role": "assistant", "content": None, "reasoning_content": "推理", "tool_calls": [{"id": str(index), "type": "function", "function": {"name": "read", "arguments": "{}"}}]}, {"role": "tool", "tool_call_id": str(index), "content": str(index)}]
        value = request(copy.deepcopy(messages))
        store.start_model_step(run, f"model:{index}", value)
        store.finish_step(run, f"model:{index}", {"content": "ok"})
        expected.append(value)
    reopened = RunStore(store.database, "a")
    for index, value in enumerate(expected):
        actual, depth = reopened.model_request(run, f"model:{index}")
        assert actual == value
        assert depth <= codec.MAX_DEPTH
    with store.connect() as db:
        assert all("messages" not in json.loads(row[0])["_prepared"] for row in db.execute("SELECT input FROM agent_steps"))
        body = json.loads(db.execute("SELECT body FROM agent_step_inputs WHERE step_key='model:8'").fetchone()[0])
        assert body["depth"] == 0
    snapshot = snapshot_session_domain(store.database, "a")
    snapshot["agent_steps"].sort(key=lambda row: row["step_key"])
    delete_session_domain_rows(store.database, "a")
    restore_session_domain(store.database, "a", snapshot)
    assert reopened.model_request(run, "model:19")[0] == expected[-1]

@pytest.mark.parametrize("damage", ["missing", "forward", "digest", "version", "prefix"])
def test_corrupt_reference_stops_recovery(tmp_path, damage):
    store = RunStore(tmp_path / "p.db", "a")
    run = store.create({}, "c", "f")
    store.start_model_step(run, "model:0", request([{"role": "user", "content": "x" * 2000}]))
    with store.connect() as db:
        if damage == "missing":
            db.execute("DELETE FROM agent_step_inputs")
        else:
            value = json.loads(db.execute("SELECT body FROM agent_step_inputs").fetchone()[0])
            if damage == "forward":
                value["base"] = "model:0"
            elif damage == "digest":
                value["suffix"][0]["content"] = "changed"
            elif damage == "version":
                value["version"] = 99
            else:
                value["prefix"] = -1
            db.execute("UPDATE agent_step_inputs SET body=?", (json.dumps(value),))
    with pytest.raises(RunConflictError):
        store.model_request(run, "model:0")

def test_legacy_read_atomic_intent_and_scope(tmp_path):
    store = RunStore(tmp_path / "p.db", "a")
    run = store.create({}, "c", "f")
    legacy = request([{"role": "user", "content": "old"}])
    store.start_step(run, "model:0", "model", legacy)
    assert store.model_request(run, "model:0")[0] == legacy
    with store.connect() as db:
        db.execute("CREATE TRIGGER fail_probe BEFORE INSERT ON agent_step_inputs BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.start_model_step(run, "model:1", legacy)
    assert store.step(run, "model:1") is None
    other = RunStore(store.database, "b")
    with pytest.raises(RunConflictError):
        other.model_request(run, "model:0")
    with pytest.raises(RunConflictError):
        other.finish_step(run, "model:0", {})

def test_archive_preserves_requests_and_rejects_foreign_body(tmp_path):
    store = RunStore(tmp_path / "p.db", "a_main")
    run = store.create({}, "c", "f")
    first = request([{"role": "user", "content": "中文" * 2000}])
    second = request(first["messages"] + [{"role": "assistant", "content": "ok"}])
    store.start_model_step(run, "model:0", first)
    store.start_model_step(run, "model:1", second)
    snapshot = snapshot_session_domain(store.database, "a")
    delete_session_domain_rows(store.database, "a")
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM agent_step_inputs").fetchone()[0] == 0
    restore_session_domain(store.database, "a", snapshot)
    assert store.model_request(run, "model:1")[0] == second
    older_database = tmp_path / "older.db"
    older = RunStore(older_database, "a_main")
    with older.connect() as db:
        db.execute("DROP TABLE agent_step_inputs")
    restore_session_domain(older_database, "a", snapshot)
    assert older.model_request(run, "model:1")[0] == second
    delete_session_domain_rows(store.database, "a")
    snapshot["agent_step_inputs"][0]["run_id"] = "foreign"
    with pytest.raises(ValueError):
        restore_session_domain(store.database, "a", snapshot)

def test_pagination_stable_ties_scope_filters_and_no_payload_reads(tmp_path):
    store = RunStore(tmp_path / "p.db", "a")
    runs = [store.create({"secret": "正文"}, "c", "f") for _ in range(43)]
    for run in runs:
        store.start_step(run, "tool:0", "tool", {})
    store.update(runs[0], status="completed")
    with store.connect() as db:
        db.execute("UPDATE agent_runs SET created_at=1,payload='invalid json'")
    other = RunStore(store.database, "b")
    other.create({}, "c", "f")
    seen, cursor = [], None
    while True:
        page = store.summaries(20, cursor)
        assert len(page["runs"]) <= 20
        assert all(len(row["steps"]) == 1 for row in page["runs"])
        seen.extend(row["id"] for row in page["runs"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == sorted(runs, reverse=True)
    assert len(store.summaries()["runs"]) == 43
    assert len(store.summaries(100, unfinished=True)["runs"]) == 42
    for invalid in ("invalid", "e30=", "W10="):
        with pytest.raises(ValueError):
            store.summaries(20, invalid)
    with pytest.raises(ValueError):
        other.summaries(20, store.summaries(20)["next_cursor"])

@pytest.mark.parametrize("module,names,policy", [
    (coding_tools, ("ReadFileTool", "ListDirTool", "GlobFilesTool", "GrepFilesTool"), "retry"),
    (coding_tools, ("WriteFileTool", "EditFileTool", "SearchReplaceTool", "TodoWriteTool", "AskUserTool", "ShellTool", "SubAgentTool"), "manual"),
    (base_tools, ("ReadDocumentTool",), "retry"),
    (memory_tools, ("ListMemoriesTool", "GetMemoryTool"), "retry"),
    (memory_tools, ("AddMemoryTool", "UpdateMemoryTool", "DeleteMemoryTool"), "manual"),
    (search, ("SearchTool",), "retry"),
    (search, ("FetchPageTool",), "manual"),
    (sandbox_tools, ("CodeSandboxTool",), "manual"),
    (agent, ("SubAgent",), "manual"),
])
def test_public_sync_async_policy_boundary(module, names, policy):
    for name in names:
        assert getattr(module, name).recovery_policy == policy
        assert getattr(module, "Async" + name).recovery_policy == policy
    assert SkillTool.recovery_policy == "retry"

@pytest.mark.parametrize("asynchronous", [False, True])
def test_rag_factory_only_retries_queries(tmp_path, monkeypatch, asynchronous):
    service = SimpleNamespace(validate_references=lambda config: config)
    monkeypatch.setattr(rag_tools, "RagService", lambda *args: service)
    session: Any = AsyncSimpleSession("probe", cast(AsyncLLM, object()), db_path=str(tmp_path / "p.db")) if asynchronous else SimpleNamespace(session_id="probe")
    session.storage_layout = object()
    session.plugin_model_manager = object()
    session.storage_platform_id = "probe"
    tools = rag_tools.get_tools(session, {})
    assert {tool.tool_name: tool.recovery_policy for tool in tools} == {
        "rag_search": "retry", "rag_list": "retry", "rag_ingest": "manual",
    }
