"""StateStore 检查点 / 回滚 / 分支 单元测试"""
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from satrap.core.state import StateStore
from satrap.core.state.mutation import current_mutation_context, state_mutation_context
from satrap.core.type import (
    JsonRow,
    RestoreOptions,
    SnapshotDomain,
    StateScope,
)
from satrap.core.utils.context import AsyncContextManager, ContextManager


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


# ── 检查点 CRUD ──

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


# ── 回滚 ──

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

        # 回滚后重新创建检查点, 审计字段应记录上下文来源
        cp2 = store.create_checkpoint(scope)
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


# ── 稳定检查点 ──

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

        # 水位变化后创建新的稳定检查点
        _write_kv(str(store.db_path), "conv-1", "b", "2")
        stable3 = store.ensure_stable_checkpoint(scope)
        assert stable3 is not None
        assert stable3.checkpoint_id != stable1.checkpoint_id
        assert len(store.list_checkpoints(scope)) == 2

    def test_stable_skips_empty_scope(self, store: StateStore):
        assert store.ensure_stable_checkpoint(StateScope("test", "empty")) is None


# ── 分支 (fork) ──

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


# ── 快照版本校验 ──

class TestSnapshotValidation:
    def test_unsupported_snapshot_version_raises(self, store: StateStore):
        scope = StateScope("test", "conv-1")
        _write_kv(str(store.db_path), "conv-1", "a", "1")
        cp = store.create_checkpoint(scope, materialize=True)

        # 手工篡改快照版本
        conn = sqlite3.connect(str(store.db_path))
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


# ── 变更审计上下文 ──

class TestMutationContext:
    def test_context_readable_inside_scope(self):
        with state_mutation_context(source="test-source", reason="测试原因") as ctx:
            assert ctx.source == "test-source"
            assert ctx.reason == "测试原因"
            assert len(ctx.change_set_id) == 32
            assert current_mutation_context() is ctx

        # 退出作用域后恢复为空
        assert current_mutation_context() is None

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


# ── ContextManager 集成 ──

class TestContextManagerCheckpoint:
    def test_checkpoint_rollback_roundtrip(self, tmp_path: Path):
        """写消息 -> 检查点 -> 继续写 -> 回滚还原"""
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
    async def test_checkpoint_rollback_roundtrip(self, tmp_path: Path):
        """异步版: 写消息 -> 检查点 -> 继续写 -> 回滚还原"""
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

        assert len(ctx.get_context()) == 2
        assert ctx.get_context()[0]["content"] == "你好"
        assert ctx.get_context()[1]["content"] == "你好呀"

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
