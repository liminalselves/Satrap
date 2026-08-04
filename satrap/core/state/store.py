"""状态检查点存储

表结构:
- state_scopes: 作用域与当前状态版本
- state_checkpoints: 检查点元数据 (含变更来源审计字段)
- state_snapshots: 独立状态快照 (JSON)

所有写操作在显式事务中执行; 领域数据由调用方注册的回调读写,
与检查点同库时享受完整事务原子性。
"""
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional
import hashlib
import json
import sqlite3
import threading
import time
import uuid

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


def _json_loads(text: str) -> dict:
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
        self.db_path = Path(db_path) if db_path else Path.cwd() / ".satrap" / "state.db"
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
    ) -> StateCheckpoint:
        """为作用域创建检查点: 构建完整快照并写入双表

        参数:
        - scope: 状态作用域
        - name: 检查点显示名称
        - description: 检查点说明
        - checkpoint_kind: 检查点类型, manual 或 stable
        - checkpoint_id: 检查点 ID, 默认自动生成

        返回:
        - StateCheckpoint: 创建的检查点
        """
        cid = checkpoint_id or f"{checkpoint_kind}-{uuid.uuid4().hex[:16]}"
        with self._lock:
            with self._transaction() as conn:
                return self._create_checkpoint(
                    conn, scope, cid, name, description, checkpoint_kind
                )

    def ensure_stable_checkpoint(self, scope: StateScope) -> Optional[StateCheckpoint]:
        """为作用域创建或刷新稳定检查点 (同水位去重)

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
                snapshot = self._load_snapshot(conn, checkpoint)
                scope = StateScope(
                    checkpoint.namespace, checkpoint.scope_id, checkpoint.branch_id
                )
                restore_snapshot(
                    conn, scope, snapshot, self.registry, RestoreOptions(preserve_ids=True)
                )
                self._set_revision(conn, scope, checkpoint.state_revision)
                self._delete_future_checkpoints(conn, checkpoint)
        logger.info(f"[StateStore] 已回滚到检查点: {checkpoint_id}")

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
                snapshot = self._load_snapshot(conn, checkpoint)
                new_scope = StateScope(checkpoint.namespace, new_scope_id, checkpoint.branch_id)
                self._ensure_scope_empty(conn, new_scope)
                id_map = build_id_map(self.registry, snapshot, new_scope)
                restore_snapshot(
                    conn,
                    new_scope,
                    snapshot,
                    self.registry,
                    RestoreOptions(preserve_ids=False, id_map=id_map),
                )
                self._set_revision(conn, new_scope, checkpoint.state_revision)
                # 在新分支起点自动创建检查点, 记录父检查点
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
    def _transaction(self) -> Iterator[sqlite3.Connection]:
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
                conn.commit()

    def _create_checkpoint(
        self,
        conn: sqlite3.Connection,
        scope: StateScope,
        checkpoint_id: str,
        name: str,
        description: str,
        checkpoint_kind: str,
        parent_checkpoint_id: str | None = None,
    ) -> StateCheckpoint:
        """事务内创建检查点: 构建快照 + 写入双表 + 推进版本"""
        mutation = current_mutation_context()
        snapshot = build_snapshot(conn, scope, self.registry)
        snapshot_id = f"state-snapshot-{uuid.uuid4().hex}"
        position = self._collect_position(conn, scope)
        revision = self._next_revision(conn, scope)
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
        conn.execute(
            "INSERT INTO state_checkpoints "
            "(checkpoint_id, namespace, scope_id, branch_id, name, description, "
            " snapshot_id, state_revision, position, checkpoint_kind, "
            " parent_checkpoint_id, source, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                checkpoint_id,
                scope.namespace,
                scope.scope_id,
                scope.branch_id,
                name,
                description,
                snapshot_id,
                revision,
                position,
                checkpoint_kind,
                parent_checkpoint_id,
                mutation.source if mutation else "manual",
                mutation.reason if mutation else "",
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
            state_revision=revision,
            position=position,
            checkpoint_kind=checkpoint_kind,
            parent_checkpoint_id=parent_checkpoint_id,
            created_at=now,
        )

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
        """删除作用域内水位高于检查点的未来检查点及其快照"""
        rows = conn.execute(
            "SELECT snapshot_id FROM state_checkpoints "
            "WHERE namespace = ? AND scope_id = ? AND branch_id = ? AND position > ?",
            (checkpoint.namespace, checkpoint.scope_id, checkpoint.branch_id, checkpoint.position),
        ).fetchall()
        conn.execute(
            "DELETE FROM state_checkpoints "
            "WHERE namespace = ? AND scope_id = ? AND branch_id = ? AND position > ?",
            (checkpoint.namespace, checkpoint.scope_id, checkpoint.branch_id, checkpoint.position),
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
            state_revision=int(row["state_revision"]),
            position=int(row["position"]),
            checkpoint_kind=str(row["checkpoint_kind"]),
            parent_checkpoint_id=(
                str(row["parent_checkpoint_id"]) if row["parent_checkpoint_id"] else None
            ),
            created_at=float(row["created_at"]),
        )
