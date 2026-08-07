"""状态检查点存储

表结构:
- state_scopes: 作用域与当前状态版本
- state_checkpoints: 检查点元数据 (含变更来源审计字段)
- state_snapshots: 独立状态快照 (JSON)

所有写操作在显式事务中执行; 领域数据由调用方注册的回调读写,
与检查点同库时享受完整事务原子性
"""
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, List, Optional, Union
import hashlib
import json
import sqlite3
import threading
import time
import uuid

from satrap.core.utils.paths import get_data_dir

from satrap.core.log import logger
from satrap.core.state.mutation import current_mutation_context
from satrap.core.state.registry import DomainRegistry
from satrap.core.state.snapshot import build_id_map, build_snapshot, restore_snapshot
from satrap.core.type import (
    RestoreOptions,
    SnapshotDomain,
    StateCheckpoint,
    StateScope,
    StateSnapshot,
)


def _json_dumps(value: object) -> str:
    """序列化快照为 JSON 文本 (保留中文可读)"""
    return json.dumps(value, ensure_ascii=False)


def _json_loads(text: str) -> dict[str, Any]:
    """反序列化快照 JSON 文本"""
    return json.loads(text)


class StateStore:
    """状态检查点存储: 检查点 CRUD、回滚与分支

    用法示例:
        store = StateStore(db_path=".satrap/chat_history.db")
        store.register_domain(messages_domain)
        store.create_checkpoint(StateScope("conversation", "demo"), name="起点")
    """
    def __init__(
        self,
        db_path: str | Path | None = None,
        registry: DomainRegistry | None = None,
    ):
        """初始化存储

        参数:
        - db_path: 数据库文件路径, 默认 ".satrap/state.db"
        - registry: 领域注册表, 默认新建
        """
        self.db_path = Path(db_path) if db_path else get_data_dir() / "state.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry = registry or DomainRegistry()
        self._lock = threading.RLock()
        self._init_tables()

    # ── 领域注册 ──

    def register_domain(self, domain: SnapshotDomain) -> None:
        """注册领域 (同名注册会覆盖)"""
        self.registry.register(domain)

    # ── 检查点管理 ──

    def create_checkpoint(
        self,
        scope: StateScope,
        name: str = "",
        description: str = "",
        checkpoint_kind: str = "manual",
        checkpoint_id: str | None = None,
        batch_id: str = "",
        materialize: bool = False,
    ) -> StateCheckpoint:
        """为作用域创建检查点: 指针式 (默认, 零快照) 或显式物化完整快照

        参数:
        - scope: 状态作用域
        - name: 检查点显示名称
        - description: 检查点说明
        - checkpoint_kind: 检查点类型, manual 或 stable
        - checkpoint_id: 检查点 ID, 默认自动生成
        - batch_id: 会话级聚合检查点的批次 ID, 同批检查点共享; 非聚合时留空
        - materialize: True 时构建完整快照 (供非追加式领域使用); 默认指针式

        返回:
        - StateCheckpoint: 创建的检查点
        """
        cid = checkpoint_id or f"{checkpoint_kind}-{uuid.uuid4().hex[:16]}"
        with self._lock:
            with self._transaction() as conn:
                return self._create_checkpoint(
                    conn, scope, cid, name, description, checkpoint_kind,
                    batch_id=batch_id, materialize=materialize,
                )

    def ensure_stable_checkpoint(self, scope: StateScope) -> Optional[StateCheckpoint]:
        """为作用域创建或刷新稳定检查点 (同水位去重, 指针式零快照)

        参数:
        - scope: 状态作用域

        返回:
        - StateCheckpoint: 稳定检查点, 作用域无数据时返回 None
        """
        with self._lock:
            with self._transaction() as conn:
                position = self._collect_position(conn, scope)
                if position <= 0:
                    return None
                existing = self._find_stable_checkpoint(conn, scope, position)
                if existing is not None:
                    self._delete_checkpoint_inner(conn, existing)
                digest = hashlib.sha256(
                    f"{scope.namespace}\0{scope.scope_id}\0"
                    f"{scope.branch_id}\0{position}".encode("utf-8")
                ).hexdigest()[:16]
                return self._create_checkpoint(
                    conn,
                    scope,
                    f"stable-{digest}",
                    name=f"水位 {position}",
                    description="自动保存的稳定状态",
                    checkpoint_kind="stable",
                    source_override="auto",
                )

    def list_checkpoints(self, scope: StateScope) -> List[StateCheckpoint]:
        """列出作用域下的全部检查点 (按创建时间升序)"""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM state_checkpoints "
                    "WHERE namespace = ? AND scope_id = ? AND branch_id = ? "
                    "ORDER BY created_at, checkpoint_id",
                    (scope.namespace, scope.scope_id, scope.branch_id),
                ).fetchall()
        return [self._checkpoint_from_row(row) for row in rows]

    def get_checkpoint(self, checkpoint_id: str) -> Optional[StateCheckpoint]:
        """按 ID 读取检查点"""
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM state_checkpoints WHERE checkpoint_id = ?",
                    (checkpoint_id,),
                ).fetchone()
        return self._checkpoint_from_row(row) if row is not None else None

    def list_checkpoints_by_batch(self, batch_id: str) -> List[StateCheckpoint]:
        """列出同一批次 (会话级聚合检查点) 的全部检查点"""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM state_checkpoints WHERE batch_id = ? "
                    "ORDER BY created_at, checkpoint_id",
                    (batch_id,),
                ).fetchall()
        return [self._checkpoint_from_row(row) for row in rows]

    # ── 分支树查询 ──

    def trace_lineage(self, checkpoint_id: str) -> List[StateCheckpoint]:
        """沿 parent_checkpoint_id 回溯检查点血缘 (根在前, 含自身)

        参数:
        - checkpoint_id: 起点检查点 ID

        返回:
        - list[StateCheckpoint]: 从根到自身的检查点链

        异常:
        - ValueError: 检查点不存在
        """
        with self._lock:
            with self._connect() as conn:
                lineage: List[StateCheckpoint] = []
                seen: set[str] = set()
                current_id: Optional[str] = checkpoint_id
                while current_id and current_id not in seen:
                    seen.add(current_id)
                    row = conn.execute(
                        "SELECT * FROM state_checkpoints WHERE checkpoint_id = ?",
                        (current_id,),
                    ).fetchone()
                    if row is None:
                        if not lineage:
                            raise ValueError(f"检查点不存在: {current_id}")
                        break
                    current = self._checkpoint_from_row(row)
                    lineage.append(current)
                    current_id = current.parent_checkpoint_id
                lineage.reverse()
        return lineage

    def list_child_branches(self, checkpoint_id: str) -> List[StateCheckpoint]:
        """列出直接子分支检查点 (parent_checkpoint_id 指向给定检查点, 跨作用域)"""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM state_checkpoints WHERE parent_checkpoint_id = ? "
                    "ORDER BY created_at, checkpoint_id",
                    (checkpoint_id,),
                ).fetchall()
        return [self._checkpoint_from_row(row) for row in rows]

    def list_branches(self, scope_prefix: str) -> List[StateCheckpoint]:
        """列出 scope_id 以给定前缀开头且带分支标记 (":fork:") 的检查点

        参数:
        - scope_prefix: 作用域 ID 前缀, 如 "conv-1:fork:" 或 "s1_main:fork:"

        返回:
        - list[StateCheckpoint]: 分支检查点列表 (按时间升序)
        """
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM state_checkpoints WHERE scope_id >= ? AND scope_id < ? "
                    "ORDER BY created_at, checkpoint_id",
                    (scope_prefix, scope_prefix + "\uffff"),
                ).fetchall()
        return [self._checkpoint_from_row(row) for row in rows]

    # ── 变更审计 ──

    def list_mutations(self, scope: StateScope) -> List[StateCheckpoint]:
        """列出作用域下的检查点变更记录 (按创建时间倒序), 含审计字段 source / reason

        参数:
        - scope: 状态作用域

        返回:
        - list[StateCheckpoint]: 变更记录列表 (最新在前)
        """
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM state_checkpoints "
                    "WHERE namespace = ? AND scope_id = ? AND branch_id = ? "
                    "ORDER BY created_at DESC, checkpoint_id DESC",
                    (scope.namespace, scope.scope_id, scope.branch_id),
                ).fetchall()
        return [self._checkpoint_from_row(row) for row in rows]

    def delete_checkpoint(self, checkpoint_id: str) -> bool:
        """删除检查点及其引用的快照

        返回:
        - bool: 是否存在并删除成功
        """
        with self._lock:
            with self._transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM state_checkpoints WHERE checkpoint_id = ?",
                    (checkpoint_id,),
                ).fetchone()
                if row is None:
                    return False
                self._delete_checkpoint_inner(conn, self._checkpoint_from_row(row))
                return True

    # ── 回滚与分支 ──

    def rollback(self, checkpoint_id: str) -> None:
        """把作用域回滚到检查点: 恢复快照并清理未来的检查点

        参数:
        - checkpoint_id: 目标检查点 ID

        异常:
        - ValueError: 检查点不存在或其快照缺失
        """
        with self._lock:
            with self._transaction() as conn:
                checkpoint = self._require_checkpoint(conn, checkpoint_id)
                scope = StateScope(
                    checkpoint.namespace, checkpoint.scope_id, checkpoint.branch_id
                )
                self._protect_current_state(
                    conn, scope, "rollback_snapshot", f"回滚前保护 ({checkpoint_id})"
                )
                self._rollback_inner(conn, checkpoint, keep_future=False)
        logger.info(f"[StateStore] 已回滚到检查点: {checkpoint_id}")

    def retry(self, checkpoint_id: str) -> None:
        """从检查点重试: 恢复快照并回退版本, 但保留未来检查点

        与 rollback 的区别: retry 不清除水位更高的未来检查点,
        重试后若走不同路线, 旧分支检查点仍可回滚

        参数:
        - checkpoint_id: 目标检查点 ID

        异常:
        - ValueError: 检查点不存在或其快照缺失
        """
        with self._lock:
            with self._transaction() as conn:
                checkpoint = self._require_checkpoint(conn, checkpoint_id)
                scope = StateScope(
                    checkpoint.namespace, checkpoint.scope_id, checkpoint.branch_id
                )
                self._protect_current_state(
                    conn, scope, "retry_snapshot", f"重试前保护 ({checkpoint_id})"
                )
                self._rollback_inner(conn, checkpoint, keep_future=True)
        logger.info(f"[StateStore] 已重试到检查点: {checkpoint_id}")

    def rollback_batch(self, batch_id: str) -> int:
        """在单个事务中回滚同一批次 (会话级聚合) 的全部检查点

        参数:
        - batch_id: 批次 ID

        返回:
        - int: 回滚的检查点数量

        异常:
        - ValueError: 批次不存在
        """
        if not batch_id:
            raise ValueError("batch_id 不能为空, 单检查点请使用 rollback()")
        with self._lock:
            with self._transaction() as conn:
                checkpoints = self._load_batch_locked(conn, batch_id)
                self._protect_batch_locked(conn, checkpoints, "rollback_snapshot", batch_id)
                for checkpoint in checkpoints:
                    self._rollback_inner(conn, checkpoint, keep_future=False)
        logger.info(f"[StateStore] 已回滚检查点批次: {batch_id} ({len(checkpoints)} 个)")
        return len(checkpoints)

    def retry_batch(self, batch_id: str) -> int:
        """在单个事务中重试同一批次 (会话级聚合) 的全部检查点, 保留未来检查点

        参数:
        - batch_id: 批次 ID

        返回:
        - int: 重试的检查点数量

        异常:
        - ValueError: 批次不存在
        """
        if not batch_id:
            raise ValueError("batch_id 不能为空, 单检查点请使用 retry()")
        with self._lock:
            with self._transaction() as conn:
                checkpoints = self._load_batch_locked(conn, batch_id)
                self._protect_batch_locked(conn, checkpoints, "retry_snapshot", batch_id)
                for checkpoint in checkpoints:
                    self._rollback_inner(conn, checkpoint, keep_future=True)
        logger.info(f"[StateStore] 已重试检查点批次: {batch_id} ({len(checkpoints)} 个)")
        return len(checkpoints)

    def fork(self, checkpoint_id: str, new_scope_id: str) -> StateScope:
        """从检查点 fork 一条新分支: 复制快照到新作用域并重映射引用

        参数:
        - checkpoint_id: 源检查点 ID
        - new_scope_id: 新作用域 ID (如 "demo:fork:bad_end")

        返回:
        - StateScope: 新分支作用域

        异常:
        - ValueError: 检查点不存在, 或目标作用域已有数据
        """
        with self._lock:
            with self._transaction() as conn:
                checkpoint = self._require_checkpoint(conn, checkpoint_id)
                new_scope = StateScope(checkpoint.namespace, new_scope_id, checkpoint.branch_id)
                self._ensure_scope_empty(conn, new_scope)
                self._set_revision(conn, new_scope, checkpoint.state_revision)
                if checkpoint.snapshot_id:
                    # 快照型源: 从快照恢复并重映射引用
                    snapshot = self._load_snapshot(conn, checkpoint)
                    id_map = build_id_map(self.registry, snapshot, new_scope)
                    restore_snapshot(
                        conn,
                        new_scope,
                        snapshot,
                        self.registry,
                        RestoreOptions(preserve_ids=False, id_map=id_map),
                    )
                    self._create_checkpoint(
                        conn,
                        new_scope,
                        f"fork-{uuid.uuid4().hex[:16]}",
                        name=f"{checkpoint.name} (fork)" if checkpoint.name else "分支起点",
                        description=f"从检查点 {checkpoint_id} 分支",
                        checkpoint_kind="manual",
                        parent_checkpoint_id=checkpoint_id,
                        materialize=True,
                    )
                else:
                    # 指针型源: 复制到水位为止的消息行 (追加式语义)
                    has_table = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chat_history'"
                    ).fetchone()
                    if has_table is not None:
                        conn.execute(
                            "INSERT INTO chat_history "
                            "(conversation_id, role, content, content_json, tool_call_id, "
                            " tool_calls, reasoning_content) "
                            "SELECT ?, role, content, content_json, tool_call_id, "
                            "tool_calls, reasoning_content FROM chat_history "
                            "WHERE conversation_id = ? AND id IN "
                            "(SELECT id FROM chat_history WHERE conversation_id = ? "
                            "ORDER BY id LIMIT ?)",
                            (new_scope_id, checkpoint.scope_id, checkpoint.scope_id, checkpoint.position),
                        )
                    else:
                        logger.warning(
                            f"[StateStore] 指针检查点 {checkpoint_id} 无消息表可复制, 分支为空"
                        )
                    self._create_checkpoint(
                        conn,
                        new_scope,
                        f"fork-{uuid.uuid4().hex[:16]}",
                        name=f"{checkpoint.name} (fork)" if checkpoint.name else "分支起点",
                        description=f"从检查点 {checkpoint_id} 分支",
                        checkpoint_kind="manual",
                        parent_checkpoint_id=checkpoint_id,
                    )
        logger.info(f"[StateStore] 已从检查点 {checkpoint_id} 分支到 {new_scope_id}")
        return new_scope

    # ── 内部实现 ──

    def _connect(self) -> sqlite3.Connection:
        """创建数据库连接"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """显式事务: 成功提交, 异常整体回滚"""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_tables(self) -> None:
        """初始化表结构 (幂等)"""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS state_scopes (
                        namespace      TEXT NOT NULL,
                        scope_id       TEXT NOT NULL,
                        branch_id      TEXT NOT NULL DEFAULT '',
                        state_revision INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (namespace, scope_id, branch_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS state_checkpoints (
                        checkpoint_id        TEXT PRIMARY KEY,
                        namespace            TEXT NOT NULL,
                        scope_id             TEXT NOT NULL,
                        branch_id            TEXT NOT NULL DEFAULT '',
                        name                 TEXT NOT NULL DEFAULT '',
                        description          TEXT NOT NULL DEFAULT '',
                        snapshot_id          TEXT NOT NULL,
                        batch_id             TEXT NOT NULL DEFAULT '',
                        state_revision       INTEGER NOT NULL,
                        position             INTEGER NOT NULL DEFAULT 0,
                        checkpoint_kind      TEXT NOT NULL DEFAULT 'manual',
                        parent_checkpoint_id TEXT,
                        source               TEXT NOT NULL DEFAULT 'manual',
                        reason               TEXT NOT NULL DEFAULT '',
                        created_at           REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_state_ckpt_scope
                    ON state_checkpoints (namespace, scope_id, branch_id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_state_ckpt_batch
                    ON state_checkpoints (batch_id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_state_ckpt_parent
                    ON state_checkpoints (parent_checkpoint_id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_state_ckpt_scope_id
                    ON state_checkpoints (scope_id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_state_ckpt_scope_time
                    ON state_checkpoints (namespace, scope_id, branch_id, created_at, checkpoint_id)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS state_snapshots (
                        snapshot_id    TEXT PRIMARY KEY,
                        namespace      TEXT NOT NULL,
                        scope_id       TEXT NOT NULL,
                        branch_id      TEXT NOT NULL DEFAULT '',
                        snapshot_json  TEXT NOT NULL,
                        created_at     REAL NOT NULL
                    )
                    """
                )
                self._ensure_columns(conn)
                conn.commit()

    _MIGRATABLE_COLUMNS = {
        "batch_id": "ALTER TABLE state_checkpoints ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''",
        "source": "ALTER TABLE state_checkpoints ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'",
        "reason": "ALTER TABLE state_checkpoints ADD COLUMN reason TEXT NOT NULL DEFAULT ''",
    }
    """增量列迁移清单: 旧库缺列时逐个 ALTER TABLE 补充"""

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        """为旧库补齐缺失的增量列 (SQLite ALTER TABLE 迁移, 幂等)"""
        cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(state_checkpoints)")}
        for name, ddl in self._MIGRATABLE_COLUMNS.items():
            if name not in cols:
                conn.execute(ddl)
        if "batch_id" not in cols:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_state_ckpt_batch ON state_checkpoints (batch_id)"
            )

    def _create_checkpoint(
        self,
        conn: sqlite3.Connection,
        scope: StateScope,
        checkpoint_id: str,
        name: str,
        description: str,
        checkpoint_kind: str,
        parent_checkpoint_id: str | None = None,
        batch_id: str = "",
        materialize: bool = False,
        source_override: str | None = None,
    ) -> StateCheckpoint:
        """事务内创建检查点: 指针式 (零快照) 或显式物化快照 + 写入元数据 + 推进版本

        参数:
        - materialize: True 时构建完整快照 (保护检查点/显式快照用); 默认指针式
        - source_override: 显式指定变更来源, 覆盖当前审计上下文 (如 auto / rollback_snapshot)
        """
        mutation = current_mutation_context()
        source = source_override or (mutation.source if mutation else "manual")
        reason = mutation.reason if mutation else ""
        snapshot_id = ""
        if materialize:
            snapshot = build_snapshot(conn, scope, self.registry)
            snapshot_id = f"state-snapshot-{uuid.uuid4().hex}"
            now = time.time()
            conn.execute(
                "INSERT INTO state_snapshots "
                "(snapshot_id, namespace, scope_id, branch_id, snapshot_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    scope.namespace,
                    scope.scope_id,
                    scope.branch_id,
                    _json_dumps(snapshot.to_dict()),
                    now,
                ),
            )
        position = self._collect_position(conn, scope)
        revision = self._next_revision(conn, scope)
        now = time.time()
        conn.execute(
            "INSERT INTO state_checkpoints "
            "(checkpoint_id, namespace, scope_id, branch_id, name, description, "
            " snapshot_id, batch_id, state_revision, position, checkpoint_kind, "
            " parent_checkpoint_id, source, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                checkpoint_id,
                scope.namespace,
                scope.scope_id,
                scope.branch_id,
                name,
                description,
                snapshot_id,
                batch_id,
                revision,
                position,
                checkpoint_kind,
                parent_checkpoint_id,
                source,
                reason,
                now,
            ),
        )
        return StateCheckpoint(
            checkpoint_id=checkpoint_id,
            namespace=scope.namespace,
            scope_id=scope.scope_id,
            branch_id=scope.branch_id,
            name=name,
            description=description,
            snapshot_id=snapshot_id,
            batch_id=batch_id,
            state_revision=revision,
            position=position,
            checkpoint_kind=checkpoint_kind,
            parent_checkpoint_id=parent_checkpoint_id,
            created_at=now,
        )

    def _rollback_inner(
        self,
        conn: sqlite3.Connection,
        checkpoint: StateCheckpoint,
        keep_future: bool = False,
    ) -> None:
        """事务内恢复单个检查点: 恢复快照或按水位截断 + 回退版本

        参数:
        - keep_future: True=重试语义, 保留未来检查点; False=回滚语义, 清理未来检查点
        """
        scope = StateScope(
            checkpoint.namespace, checkpoint.scope_id, checkpoint.branch_id
        )
        if checkpoint.snapshot_id:
            # 快照型: 从完整快照恢复 (旧检查点 / 保护检查点 / 显式快照)
            snapshot = self._load_snapshot(conn, checkpoint)
            restore_snapshot(
                conn, scope, snapshot, self.registry, RestoreOptions(preserve_ids=True)
            )
        else:
            # 指针型: 追加式消息按水位截断恢复
            self._restore_by_position(conn, checkpoint)
        self._set_revision(conn, scope, checkpoint.state_revision)
        if not keep_future:
            self._delete_future_checkpoints(conn, checkpoint)

    def _restore_by_position(
        self, conn: sqlite3.Connection, checkpoint: StateCheckpoint
    ) -> None:
        """指针式恢复: 保留消息领域水位之前的记录 (追加式语义)

        仅适用于追加式消息存储 (chat_history 行按写入顺序稳定);
        编辑类操作会改写/删除历史行, 执行前必须先物化保护检查点
        库中不存在消息表时 (非消息领域库) 跳过
        """
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chat_history'"
        ).fetchone()
        if has_table is None:
            logger.warning(
                f"[StateStore] 指针检查点 {checkpoint.checkpoint_id} 无消息表可截断, 跳过恢复"
            )
            return
        conn.execute(
            "DELETE FROM chat_history WHERE conversation_id = ? AND id NOT IN "
            "(SELECT id FROM chat_history WHERE conversation_id = ? ORDER BY id LIMIT ?)",
            (checkpoint.scope_id, checkpoint.scope_id, checkpoint.position),
        )

    def _protect_current_state(
        self,
        conn: sqlite3.Connection,
        scope: StateScope,
        source: str,
        name: str,
    ) -> StateCheckpoint:
        """事务内物化当前状态为保护检查点 (撤销回滚/重试的锚点)

        保护检查点持有完整快照, 且不会被未来删除逻辑清理
        """
        return self._create_checkpoint(
            conn,
            scope,
            f"protect-{uuid.uuid4().hex[:16]}",
            name=name,
            description="破坏性操作前的状态保护",
            checkpoint_kind="manual",
            materialize=True,
            source_override=source,
        )

    def _protect_batch_locked(
        self,
        conn: sqlite3.Connection,
        checkpoints: List[StateCheckpoint],
        source: str,
        batch_id: str,
    ) -> None:
        """事务内为批次内每个作用域物化保护检查点 (去重后逐个保护)"""
        seen_scopes: set[str] = set()
        for checkpoint in checkpoints:
            key = f"{checkpoint.namespace}\0{checkpoint.scope_id}\0{checkpoint.branch_id}"
            if key in seen_scopes:
                continue
            seen_scopes.add(key)
            scope = StateScope(
                checkpoint.namespace, checkpoint.scope_id, checkpoint.branch_id
            )
            self._protect_current_state(
                conn, scope, source, f"回滚前保护 ({batch_id})"
            )

    def materialize_edit_protection(self, scope: StateScope) -> StateCheckpoint:
        """编辑类操作前调用: 物化当前状态为保护检查点, 并清理该作用域失效的指针检查点

        指针检查点依赖追加式消息的水位截断语义, 编辑改写/删除历史行后失效,
        因此物化保护后删除; 保护检查点保留, 可撤销编辑

        返回:
        - StateCheckpoint: 编辑前状态的保护检查点
        """
        with self._lock:
            with self._transaction() as conn:
                protect = self._protect_current_state(
                    conn, scope, "edit_protect", "编辑前状态保护"
                )
                cursor = conn.execute(
                    "DELETE FROM state_checkpoints "
                    "WHERE namespace = ? AND scope_id = ? AND branch_id = ? "
                    "AND snapshot_id = '' AND checkpoint_id != ?",
                    (scope.namespace, scope.scope_id, scope.branch_id, protect.checkpoint_id),
                )
                deleted = cursor.rowcount
        if deleted:
            logger.warning(
                f"[StateStore] 编辑操作清理 {deleted} 个失效指针检查点 (scope={scope.scope_id})"
            )
        return protect

    def _load_batch_locked(
        self, conn: sqlite3.Connection, batch_id: str
    ) -> List[StateCheckpoint]:
        """事务内读取批次检查点列表, 批次不存在或作用域重复时抛出 ValueError"""
        rows = conn.execute(
            "SELECT * FROM state_checkpoints WHERE batch_id = ? "
            "ORDER BY created_at, checkpoint_id",
            (batch_id,),
        ).fetchall()
        checkpoints = [self._checkpoint_from_row(row) for row in rows]
        if not checkpoints:
            raise ValueError(f"检查点批次不存在: {batch_id}")
        scopes = {cp.scope_id for cp in checkpoints}
        if len(scopes) != len(checkpoints):
            raise ValueError(
                f"检查点批次 {batch_id} 内同一作用域存在多个检查点, 无法安全批量回滚"
            )
        return checkpoints

    def _collect_position(self, conn: sqlite3.Connection, scope: StateScope) -> int:
        """收集所有领域水位提供者的最大值"""
        positions: List[int] = []
        for domain in self.registry.all():
            provider = domain.position_provider
            if provider is not None:
                positions.append(provider(conn, scope))
        return max(positions) if positions else 0

    def _next_revision(self, conn: sqlite3.Connection, scope: StateScope) -> int:
        """获取并推进作用域状态版本"""
        revision = self._scope_revision(conn, scope) + 1
        self._set_revision(conn, scope, revision)
        return revision

    def _scope_revision(self, conn: sqlite3.Connection, scope: StateScope) -> int:
        """读取作用域当前状态版本"""
        row = conn.execute(
            "SELECT state_revision FROM state_scopes "
            "WHERE namespace = ? AND scope_id = ? AND branch_id = ?",
            (scope.namespace, scope.scope_id, scope.branch_id),
        ).fetchone()
        return int(row["state_revision"]) if row is not None else 0

    def _set_revision(self, conn: sqlite3.Connection, scope: StateScope, revision: int) -> None:
        """设置作用域状态版本 (回滚回退 / 分支继承时使用)"""
        conn.execute(
            "INSERT INTO state_scopes (namespace, scope_id, branch_id, state_revision) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(namespace, scope_id, branch_id) "
            "DO UPDATE SET state_revision = excluded.state_revision",
            (scope.namespace, scope.scope_id, scope.branch_id, revision),
        )

    def _require_checkpoint(
        self, conn: sqlite3.Connection, checkpoint_id: str
    ) -> StateCheckpoint:
        """读取检查点, 不存在时抛出 ValueError"""
        row = conn.execute(
            "SELECT * FROM state_checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"检查点不存在: {checkpoint_id}")
        return self._checkpoint_from_row(row)

    def _load_snapshot(
        self, conn: sqlite3.Connection, checkpoint: StateCheckpoint
    ) -> StateSnapshot:
        """读取检查点引用的状态快照"""
        row = conn.execute(
            "SELECT snapshot_json FROM state_snapshots WHERE snapshot_id = ?",
            (checkpoint.snapshot_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"检查点快照不存在: {checkpoint.snapshot_id}")
        return StateSnapshot.from_dict(_json_loads(row["snapshot_json"]))

    def _find_stable_checkpoint(
        self, conn: sqlite3.Connection, scope: StateScope, position: int
    ) -> Optional[StateCheckpoint]:
        """按水位查找已有稳定检查点"""
        row = conn.execute(
            "SELECT * FROM state_checkpoints "
            "WHERE namespace = ? AND scope_id = ? AND branch_id = ? "
            "AND position = ? AND checkpoint_kind = 'stable' "
            "ORDER BY created_at DESC LIMIT 1",
            (scope.namespace, scope.scope_id, scope.branch_id, position),
        ).fetchone()
        return self._checkpoint_from_row(row) if row is not None else None

    def _delete_checkpoint_inner(
        self, conn: sqlite3.Connection, checkpoint: StateCheckpoint
    ) -> None:
        """事务内删除检查点及其快照"""
        conn.execute(
            "DELETE FROM state_checkpoints WHERE checkpoint_id = ?",
            (checkpoint.checkpoint_id,),
        )
        conn.execute(
            "DELETE FROM state_snapshots WHERE snapshot_id = ?",
            (checkpoint.snapshot_id,),
        )

    def _delete_future_checkpoints(
        self, conn: sqlite3.Connection, checkpoint: StateCheckpoint
    ) -> None:
        """删除作用域内水位更高 (或同水位但创建更晚) 的未来检查点及其快照

        保护检查点 (rollback_snapshot / retry_snapshot / edit_protect) 豁免,
        保证破坏性操作后仍可撤销
        """
        rows = conn.execute(
            "SELECT snapshot_id FROM state_checkpoints "
            "WHERE namespace = ? AND scope_id = ? AND branch_id = ? "
            "AND source NOT IN ('rollback_snapshot', 'retry_snapshot', 'edit_protect') "
            "AND (position > ? OR (position = ? AND created_at > ?))",
            (
                checkpoint.namespace,
                checkpoint.scope_id,
                checkpoint.branch_id,
                checkpoint.position,
                checkpoint.position,
                checkpoint.created_at,
            ),
        ).fetchall()
        conn.execute(
            "DELETE FROM state_checkpoints "
            "WHERE namespace = ? AND scope_id = ? AND branch_id = ? "
            "AND source NOT IN ('rollback_snapshot', 'retry_snapshot', 'edit_protect') "
            "AND (position > ? OR (position = ? AND created_at > ?))",
            (
                checkpoint.namespace,
                checkpoint.scope_id,
                checkpoint.branch_id,
                checkpoint.position,
                checkpoint.position,
                checkpoint.created_at,
            ),
        )
        for row in rows:
            snapshot_id = row["snapshot_id"]
            if snapshot_id:
                conn.execute(
                    "DELETE FROM state_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                )

    def _ensure_scope_empty(self, conn: sqlite3.Connection, scope: StateScope) -> None:
        """检查目标作用域是否已有数据, 防止 fork 覆盖既有内容"""
        for domain in self.registry.all():
            if domain.builder(conn, scope):
                raise ValueError(f"目标作用域已存在数据: {scope.scope_id}")

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> StateCheckpoint:
        """将 SQLite 行转换为检查点对象"""
        return StateCheckpoint(
            checkpoint_id=str(row["checkpoint_id"]),
            namespace=str(row["namespace"]),
            scope_id=str(row["scope_id"]),
            branch_id=str(row["branch_id"]),
            name=str(row["name"] or ""),
            description=str(row["description"] or ""),
            snapshot_id=str(row["snapshot_id"]),
            batch_id=str(row["batch_id"] or ""),
            state_revision=int(row["state_revision"]),
            position=int(row["position"]),
            checkpoint_kind=str(row["checkpoint_kind"]),
            parent_checkpoint_id=(
                str(row["parent_checkpoint_id"]) if row["parent_checkpoint_id"] else None
            ),
            source=str(row["source"] or ""),
            reason=str(row["reason"] or ""),
            created_at=float(row["created_at"]),
        )
