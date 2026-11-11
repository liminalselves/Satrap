"""
流程会话持久化: 阈值位置与状态槽

flow_threshold_positions 每个 (scope, flow, tile, wf) 一份已提交 seq;
flow_states 每个 (scope, flow, state) 一份 CAS 版本化内容, 与阈值位置同库同事务写入
"""
from __future__ import annotations

from satrap.core.config.platform_messages import MessageScope

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import sqlite3
import time


@dataclass(frozen=True)
class StateCommit:
    """一次状态槽提交"""

    state_id: str
    content: str
    base_version: int
    """WF 运行开始时读到的版本, CAS 校验用"""
    covers_to: int
    run_id: str


class FlowConflictError(RuntimeError):
    """状态槽 CAS 失败或位置推进冲突"""

    def __init__(self, code: str, message: str) -> None:
        """
        初始化冲突错误

        参数:
        - code: flow_state_conflict / flow_position_conflict
        - message: 可读失败原因
        """
        super().__init__(message)
        self.code = code


class FlowStore:
    """阈值位置与状态槽的持久化存取, 与消息档案同库"""

    def __init__(self, database: str | Path, adapter_id: str, flow_name: str) -> None:
        """
        初始化存储

        参数:
        - database: platform.db 路径
        - adapter_id: 所属平台实例
        - flow_name: 命名流程配置名
        """
        self.database = Path(database)
        self.adapter_id = adapter_id
        self.flow_name = flow_name
        self.scope_key = ""
        """绑定的对话键; ensure_positions_for_tile 用, 由会话在绑档案时写入"""

    def _connect(self) -> sqlite3.Connection:
        """打开平台数据库连接, 首次写路径顺带执行表结构迁移"""
        connection = sqlite3.connect(self.database.as_uri() + "?mode=rwc", uri=True, timeout=5)
        from satrap.core.config.platform_schema import ensure_platform_tables
        ensure_platform_tables(connection)
        connection.commit()
        return connection

    # ---------- 阈值位置 ----------

    def threshold_positions(self, scope: MessageScope) -> dict[tuple[str, str], int]:
        """
        读取一个 scope 下全部阈值位置

        参数:
        - scope: 对话身份

        返回:
        - {(tile_id, wf_id): committed_seq}
        """
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT tile_id, wf_id, committed_seq FROM flow_threshold_positions "
                "WHERE scope_key=? AND flow_name=?",
                (scope.key, self.flow_name),
            ).fetchall()
            return {(str(r[0]), str(r[1])): int(r[2]) for r in rows}

    def ensure_position(self, connection: sqlite3.Connection, scope_key: str, tile_id: str, wf_id: str) -> int:
        """
        取或初始化一个 WF 在磁贴上的已提交位置 (新驱动的 WF 从当前桶末尾开始)

        参数:
        - connection: 已开事务的连接
        - scope_key: 对话键
        - tile_id: 阈值磁贴节点 id
        - wf_id: WF 节点 id

        返回:
        - committed_seq
        """
        row = connection.execute(
            "SELECT committed_seq FROM flow_threshold_positions "
            "WHERE scope_key=? AND flow_name=? AND tile_id=? AND wf_id=?",
            (scope_key, self.flow_name, tile_id, wf_id),
        ).fetchone()
        if row is not None:
            return int(row[0])
        # 新驱动的 WF 从当前桶末尾开始, 不回放历史
        latest = int(connection.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM platform_messages WHERE scope_key=?",
            (scope_key,),
        ).fetchone()[0])
        connection.execute(
            "INSERT INTO flow_threshold_positions VALUES(?, ?, ?, ?, ?, ?)",
            (scope_key, self.flow_name, tile_id, wf_id, latest, time.time()),
        )
        return latest

    def commit_position(self, connection: sqlite3.Connection, scope_key: str, tile_id: str, wf_id: str, seq: int) -> None:
        """
        推进一个 WF 的已提交位置 (与状态槽提交同事务)

        参数:
        - connection: 已开事务的连接
        - scope_key: 对话键
        - tile_id: 阈值磁贴节点 id
        - wf_id: WF 节点 id
        - seq: 批次末尾的消息序号
        """
        connection.execute(
            "INSERT INTO flow_threshold_positions VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_key, flow_name, tile_id, wf_id) DO UPDATE SET committed_seq=excluded.committed_seq, updated_at=excluded.updated_at",
            (scope_key, self.flow_name, tile_id, wf_id, seq, time.time()),
        )

    def delete_position(self, connection: sqlite3.Connection, scope_key: str, tile_id: str, wf_id: str) -> None:
        """去掉不再由该磁贴驱动的 WF 的位置记录"""
        connection.execute(
            "DELETE FROM flow_threshold_positions WHERE scope_key=? AND flow_name=? AND tile_id=? AND wf_id=?",
            (scope_key, self.flow_name, tile_id, wf_id),
        )

    def positions_for_tile(self, scope: MessageScope, tile_id: str) -> dict[str, int]:
        """
        一个磁贴驱动的全部 WF 的已提交位置

        参数:
        - scope: 对话身份
        - tile_id: 阈值磁贴节点 id

        返回:
        - {wf_id: committed_seq}
        """
        return self.positions_for_tile_by_scope_key(scope.key, tile_id)

    def positions_for_tile_by_scope_key(self, scope_key: str, tile_id: str) -> dict[str, int]:
        """
        按已冻结的对话键读一个磁贴驱动的全部 WF 位置

        参数:
        - scope_key: 对话键
        - tile_id: 阈值磁贴节点 id

        返回:
        - {wf_id: committed_seq}
        """
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT wf_id, committed_seq FROM flow_threshold_positions "
                "WHERE scope_key=? AND flow_name=? AND tile_id=?",
                (scope_key, self.flow_name, tile_id),
            ).fetchall()
            return {str(r[0]): int(r[1]) for r in rows}

    def ensure_positions_for_tile(self, tile_id: str, wf_ids: list[str]) -> dict[str, int]:
        """
        为磁贴实际驱动的每个 WF 取或初始化已提交位置 (新驱动从桶末尾开始)

        参数:
        - tile_id: 阈值磁贴节点 id
        - wf_ids: 该磁贴 event 边下游的全部 WF 节点 id

        返回:
        - {wf_id: committed_seq}; 首次驱动的 WF 取当前最新 seq
        """
        out: dict[str, int] = {}
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for wf_id in wf_ids:
                out[wf_id] = self.ensure_position(connection, self._current_scope_key, tile_id, wf_id)
            # 不再由该磁贴驱动的 WF 清掉它的位置记录 (运行时契约 3.6)
            driven = set(wf_ids)
            rows = connection.execute(
                "SELECT wf_id FROM flow_threshold_positions WHERE scope_key=? AND flow_name=? AND tile_id=?",
                (self._current_scope_key, self.flow_name, tile_id),
            ).fetchall()
            for (wf_id,) in rows:
                if str(wf_id) not in driven:
                    self.delete_position(connection, self._current_scope_key, tile_id, str(wf_id))
        return out

    @property
    def _current_scope_key(self) -> str:
        """会话绑定的对话键 (会话在绑档案时写入 store.scope_key)"""
        return self.scope_key

    # ---------- 状态槽 ----------

    def state(self, scope: MessageScope, state_id: str) -> dict[str, Any]:
        """
        读取一个状态槽当前版本

        参数:
        - scope: 对话身份
        - state_id: 状态槽节点 id

        返回:
        - {version, content, covers_to, committed_at, run_id}; 不存在返回版本 0
        """
        return self.state_by_scope_key(scope.key, state_id)

    def state_by_scope_key(self, scope_key: str, state_id: str) -> dict[str, Any]:
        """
        按已冻结的对话键读取状态槽, 供工具在拿不到 MessageScope 时使用

        参数:
        - scope_key: 对话键
        - state_id: 状态槽节点 id

        返回:
        - {version, content, covers_to, committed_at, run_id}; 不存在返回版本 0
        """
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT version, content, covers_to, committed_at, run_id FROM flow_states "
                "WHERE scope_key=? AND flow_name=? AND state_id=?",
                (scope_key, self.flow_name, state_id),
            ).fetchone()
            if row is None:
                return {"version": 0, "content": "", "covers_to": 0, "committed_at": 0.0, "run_id": ""}
            return {"version": int(row[0]), "content": str(row[1]), "covers_to": int(row[2]),
                    "committed_at": float(row[3]), "run_id": str(row[4])}

    def states_for_scope_key(self, scope_key: str) -> dict[str, dict[str, Any]]:
        """
        按已冻结的对话键读取全部状态槽, 供运行状态接口

        参数:
        - scope_key: 对话键

        返回:
        - {state_id: {version, covers_to, committed_at, run_id}}
        """
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT state_id, version, covers_to, committed_at, run_id FROM flow_states "
                "WHERE scope_key=? AND flow_name=?",
                (scope_key, self.flow_name),
            ).fetchall()
            return {
                str(r[0]): {"version": int(r[1]), "covers_to": int(r[2]),
                            "committed_at": float(r[3]), "run_id": str(r[4])}
                for r in rows
            }

    def positions_for_scope_key(self, scope_key: str) -> dict[tuple[str, str], int]:
        """
        按已冻结的对话键读取全部阈值位置, 供运行状态接口

        参数:
        - scope_key: 对话键

        返回:
        - {(tile_id, wf_id): committed_seq}
        """
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT tile_id, wf_id, committed_seq FROM flow_threshold_positions "
                "WHERE scope_key=? AND flow_name=?",
                (scope_key, self.flow_name),
            ).fetchall()
            return {(str(r[0]), str(r[1])): int(r[2]) for r in rows}

    def commit_states(
        self, scope: MessageScope, tile_id: str, wf_id: str, seq: int,
        commits: list[StateCommit],
    ) -> None:
        """
        状态槽 CAS 提交与阈值位置推进在同一事务

        参数:
        - scope: 对话身份
        - tile_id: 驱动本次运行的阈值磁贴节点 id, 空串表示非阈值触发不推进位置
        - wf_id: WF 节点 id
        - seq: 批次末尾的消息序号 (tile_id 为空时忽略)
        - commits: 要写入的状态槽列表 (同一 WF 可能写多个槽, 全部 CAS 通过才一起提交)

        返回:
        - 无; 任一槽 base_version 不匹配时整个事务回滚并抛 FlowConflictError
        """
        self.commit_states_by_scope_key(scope.key, tile_id, wf_id, seq, commits)

    def commit_states_by_scope_key(
        self, scope_key: str, tile_id: str, wf_id: str, seq: int,
        commits: list[StateCommit],
    ) -> None:
        """
        按已冻结的对话键提交状态槽与位置, 供工具在拿不到 MessageScope 时使用

        参数:
        - scope_key: 对话键
        - tile_id: 阈值磁贴节点 id, 空串不推进位置
        - wf_id: WF 节点 id
        - seq: 批次末尾序号
        - commits: 要写入的状态槽列表
        """
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for commit in commits:
                    row = connection.execute(
                        "SELECT version FROM flow_states WHERE scope_key=? AND flow_name=? AND state_id=?",
                        (scope_key, self.flow_name, commit.state_id),
                    ).fetchone()
                    current = int(row[0]) if row else 0
                    if current != commit.base_version:
                        raise FlowConflictError(
                            "flow_state_conflict",
                            f"状态槽 {commit.state_id} 版本冲突: 期望 {commit.base_version}, 当前 {current}",
                        )
                for commit in commits:
                    connection.execute(
                        "INSERT INTO flow_states VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(scope_key, flow_name, state_id) DO UPDATE SET "
                        "version=excluded.version, content=excluded.content, covers_to=excluded.covers_to, "
                        "committed_at=excluded.committed_at, run_id=excluded.run_id",
                        (scope_key, self.flow_name, commit.state_id, commit.base_version + 1,
                         commit.content, commit.covers_to, time.time(), commit.run_id),
                    )
                if tile_id:
                    self.commit_position(connection, scope_key, tile_id, wf_id, seq)
            except Exception:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def clear_state(self, scope: MessageScope, state_id: str) -> None:
        """
        /new on_new=clear: 版本回到 0, 内容清空

        参数:
        - scope: 对话身份
        - state_id: 状态槽节点 id
        """
        self.clear_state_by_scope_key(scope.key, state_id)

    def clear_state_by_scope_key(self, scope_key: str, state_id: str) -> None:
        """
        按已冻结的对话键清状态槽

        参数:
        - scope_key: 对话键
        - state_id: 状态槽节点 id
        """
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE flow_states SET version=0, content='', covers_to=0, committed_at=0, run_id='' "
                "WHERE scope_key=? AND flow_name=? AND state_id=?",
                (scope_key, self.flow_name, state_id),
            )

    def drop_position(self, scope: MessageScope, tile_id: str, wf_id: str, seq: int) -> None:
        """
        /new on_new=drop: 已提交位置直接跳到最新序号

        参数:
        - scope: 对话身份
        - tile_id: 阈值磁贴节点 id
        - wf_id: WF 节点 id
        - seq: 目标序号 (取当前最新)
        """
        self.drop_position_by_scope_key(scope.key, tile_id, wf_id, seq)

    def drop_position_by_scope_key(self, scope_key: str, tile_id: str, wf_id: str, seq: int) -> None:
        """
        按已冻结的对话键把已提交位置跳到指定序号

        参数:
        - scope_key: 对话键
        - tile_id: 阈值磁贴节点 id
        - wf_id: WF 节点 id
        - seq: 目标序号
        """
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self.commit_position(connection, scope_key, tile_id, wf_id, seq)
