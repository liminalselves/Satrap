"""平台数据库中的会话级数据清理操作"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any, cast


_RESTORABLE_TABLES = frozenset({
    "session_configs",
    "conversation_meta",
    "display_turns",
    "display_turn_variants",
    "display_tool_calls",
    "chat_history",
    "state_scopes",
    "state_checkpoints",
    "state_snapshots",
    "memories",
    "context_sessions",
    "user_info",
})
"""允许从会话归档恢复的数据库表"""


def _related_scope_pattern(session_id: str) -> str:
    """
    返回匹配会话工作流子上下文的安全 LIKE 模式

    参数:
    - session_id: 主会话 ID

    返回:
    - str: 仅匹配 `会话ID_子上下文` 的转义模式
    """
    escaped = session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "\\_%"


def _table_names(connection: sqlite3.Connection) -> set[str]:
    """返回数据库中的全部普通表名"""
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """
    返回允许恢复表的实际列名

    参数:
    - connection: SQLite 连接
    - table: 已通过服务端白名单校验的表名

    返回:
    - set[str]: 数据库架构中存在的列名
    """
    if table not in _RESTORABLE_TABLES:
        raise ValueError(f"归档包含不允许恢复的表: {table}")
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _validated_restore_columns(
    table: str,
    row: Mapping[str, object],
    available_columns: set[str],
) -> list[str]:
    """
    校验归档行的列名并保持原始字段顺序

    参数:
    - table: 目标表名
    - row: 归档中的单行记录
    - available_columns: 目标表的实际列名

    返回:
    - list[str]: 可以安全用于 INSERT 的列名
    """
    columns = list(row)
    if not columns:
        raise ValueError(f"归档表 {table} 包含空记录")
    unknown_columns = set(columns) - available_columns
    if unknown_columns:
        names = ", ".join(sorted(unknown_columns))
        raise ValueError(f"归档表 {table} 包含未知列: {names}")
    return columns


def snapshot_session_domain(database: str | Path, session_id: str) -> dict[str, list[dict[str, Any]]]:
    """
    导出恢复一个会话所需的数据库记录

    参数:
    - database: 平台数据库路径
    - session_id: 会话 ID

    返回:
    - dict[str, list[dict[str, Any]]]: 按表组织的 JSON 安全记录
    """
    database_path = Path(database)
    if not database_path.exists():
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    with sqlite3.connect(str(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        tables = _table_names(connection)
        related_pattern = _related_scope_pattern(session_id)
        selectors: dict[str, tuple[str, tuple[object, ...]]] = {
            "session_configs": ("session_id = ?", (session_id,)),
            "conversation_meta": ("conversation_id = ?", (session_id,)),
            "display_turns": ("conversation_id = ?", (session_id,)),
            "display_turn_variants": (
                "turn_id IN (SELECT id FROM display_turns WHERE conversation_id = ?)",
                (session_id,),
            ),
            "display_tool_calls": (
                "turn_id IN (SELECT id FROM display_turns WHERE conversation_id = ?)",
                (session_id,),
            ),
            "chat_history": (
                "conversation_id = ? OR conversation_id LIKE ? ESCAPE '\\'",
                (session_id, related_pattern),
            ),
            "state_scopes": (
                "namespace = ? AND (scope_id = ? OR scope_id LIKE ? ESCAPE '\\')",
                ("conversation", session_id, related_pattern),
            ),
            "state_checkpoints": (
                "namespace = ? AND (scope_id = ? OR scope_id LIKE ? ESCAPE '\\')",
                ("conversation", session_id, related_pattern),
            ),
            "state_snapshots": (
                "namespace = ? AND (scope_id = ? OR scope_id LIKE ? ESCAPE '\\')",
                ("conversation", session_id, related_pattern),
            ),
            "memories": (
                "scope = ? OR scope LIKE ? ESCAPE '\\'",
                (f"session:{session_id}", f"session:{related_pattern}"),
            ),
            "context_sessions": ("session_id = ?", (session_id,)),
        }
        for table, (where, params) in selectors.items():
            if table not in tables:
                continue
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE {where}",
                params,
            ).fetchall()
            if rows:
                result[table] = [dict(row) for row in rows]
        if "user_info" in tables:
            user_rows: list[dict[str, Any]] = []
            for row in connection.execute("SELECT * FROM user_info").fetchall():
                try:
                    sessions: object = json.loads(row["user_session"] or "[]")
                except Exception:
                    sessions = []
                if isinstance(sessions, list) and session_id in {
                    str(item) for item in cast(list[object], sessions)
                }:
                    user_rows.append(dict(row))
            if user_rows:
                result["user_info"] = user_rows
    return result


def restore_session_domain(
    database: str | Path,
    session_id: str,
    records: dict[str, list[dict[str, Any]]],
    *,
    restore_id: str | None = None,
    restore_metadata: str = "",
) -> None:
    """
    在目标会话 ID 未被占用时恢复数据库记录

    参数:
    - database: 平台数据库路径
    - session_id: 会话 ID
    - records: snapshot_session_domain 产生的记录
    """
    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(database_path))) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")   # 身份检查和插入共用写事务, 防止并发恢复交错
        if restore_id is not None:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS archive_restores "
                "(archive_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, metadata TEXT NOT NULL, "
                "completed INTEGER NOT NULL DEFAULT 0)"
            )
            receipt = connection.execute(
                "SELECT session_id, metadata FROM archive_restores WHERE archive_id = ?", (restore_id,),
            ).fetchone()
            if receipt is not None:
                if receipt["session_id"] != session_id or receipt["metadata"] != restore_metadata:
                    raise ValueError("恢复凭据与归档不匹配")
                return   # 先前提交已成功, 不重复恢复数据库记录
        tables = _table_names(connection)
        schemas: dict[str, set[str]] = {}
        for table in records:
            if table not in _RESTORABLE_TABLES:
                raise ValueError(f"归档包含不允许恢复的表: {table}")
            if table not in tables:
                raise ValueError(f"目标数据库缺少归档表: {table}")
            schemas[table] = _table_columns(connection, table)
        turn_ids = {row.get("id") for row in records.get("display_turns", [])}
        for table, rows in records.items():
            for row in rows:
                _validated_restore_columns(table, row, schemas[table])
                identity_column = {
                    "session_configs": "session_id", "conversation_meta": "conversation_id",
                    "display_turns": "conversation_id", "context_sessions": "session_id",
                }.get(table)
                if identity_column and row.get(identity_column) != session_id:
                    raise ValueError(f"归档表 {table} 的会话身份不匹配")
                if table in ("display_turn_variants", "display_tool_calls") and row.get("turn_id") not in turn_ids:
                    raise ValueError(f"归档表 {table} 引用了归档外的轮次")
                scope_column = {
                    "chat_history": "conversation_id", "state_scopes": "scope_id",
                    "state_checkpoints": "scope_id", "state_snapshots": "scope_id", "memories": "scope",
                }.get(table)
                if scope_column:
                    expected = f"session:{session_id}" if table == "memories" else session_id
                    scope = str(row.get(scope_column, ""))
                    if scope != expected and not scope.startswith(expected + "_"):
                        raise ValueError(f"归档表 {table} 的会话范围不匹配")
                if table.startswith("state_") and row.get("namespace") != "conversation":
                    raise ValueError(f"归档表 {table} 的命名空间不匹配")
                if table == "user_info":
                    try:
                        sessions = json.loads(row.get("user_session") or "[]")
                    except (TypeError, ValueError) as error:
                        raise ValueError("归档用户会话列表无效") from error
                    if not isinstance(sessions, list) or session_id not in sessions:
                        raise ValueError("归档用户会话身份不匹配")
        identity_checks = {
            "session_configs": ("session_id", session_id),
            "conversation_meta": ("conversation_id", session_id),
        }
        for table, (column, value) in identity_checks.items():
            if table not in tables:
                continue
            existed = connection.execute(
                f"SELECT 1 FROM {table} WHERE {column} = ?",
                (value,),
            ).fetchone()
            if existed is not None:
                raise ValueError(f"会话 ID 已存在: {session_id}")
        try:
            for table, rows in records.items():
                if table == "user_info":
                    continue
                for row in rows:
                    columns = _validated_restore_columns(table, row, schemas[table])
                    placeholders = ",".join("?" for _ in columns)
                    connection.execute(
                        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                        tuple(row[column] for column in columns),
                    )
            if "user_info" in tables:
                for row in records.get("user_info", []):
                    columns = _validated_restore_columns(
                        "user_info",
                        row,
                        schemas["user_info"],
                    )
                    user_id = str(row.get("user_id", ""))
                    current = connection.execute(
                        "SELECT user_session FROM user_info WHERE user_id = ?",
                        (user_id,),
                    ).fetchone()
                    if current is None:
                        placeholders = ",".join("?" for _ in columns)
                        connection.execute(
                            f"INSERT INTO user_info ({','.join(columns)}) VALUES ({placeholders})",
                            tuple(row[column] for column in columns),
                        )
                        continue
                    try:
                        sessions: object = json.loads(current["user_session"] or "[]")
                    except Exception:
                        sessions = []
                    normalized = (
                        [str(item) for item in cast(list[object], sessions)]
                        if isinstance(sessions, list)
                        else []
                    )
                    if session_id not in normalized:
                        normalized.append(session_id)
                    connection.execute(
                        "UPDATE user_info SET user_session = ? WHERE user_id = ?",
                        (json.dumps(normalized, ensure_ascii=False), user_id),
                    )
            if restore_id is not None:
                connection.execute(
                    "INSERT INTO archive_restores (archive_id, session_id, metadata) VALUES (?, ?, ?)",
                    (restore_id, session_id, restore_metadata),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def delete_session_domain_rows(database: str | Path, session_id: str) -> None:
    """
    删除指定会话的上下文, 检查点和长期记忆记录

    参数:
    - database: 平台数据库路径
    - session_id: 会话 ID
    """
    database_path = Path(database)
    if not database_path.exists():
        return
    with sqlite3.connect(str(database_path)) as connection:
        tables = _table_names(connection)
        related_pattern = _related_scope_pattern(session_id)
        if "display_tool_calls" in tables and "display_turns" in tables:
            connection.execute(
                "DELETE FROM display_tool_calls WHERE turn_id IN "
                "(SELECT id FROM display_turns WHERE conversation_id = ?)",
                (session_id,),
            )
        if "display_turn_variants" in tables and "display_turns" in tables:
            connection.execute(
                "DELETE FROM display_turn_variants WHERE turn_id IN "
                "(SELECT id FROM display_turns WHERE conversation_id = ?)",
                (session_id,),
            )
        if "display_turns" in tables:
            connection.execute(
                "DELETE FROM display_turns WHERE conversation_id = ?",
                (session_id,),
            )
        if "conversation_meta" in tables:
            connection.execute(
                "DELETE FROM conversation_meta WHERE conversation_id = ?",
                (session_id,),
            )
        if "chat_history" in tables:
            connection.execute(
                "DELETE FROM chat_history WHERE conversation_id = ? "
                "OR conversation_id LIKE ? ESCAPE '\\'",
                (session_id, related_pattern),
            )
        if "context_runtime_state" in tables:
            connection.execute(
                "DELETE FROM context_runtime_state WHERE conversation_id = ? "
                "OR conversation_id LIKE ? ESCAPE '\\'",
                (session_id, related_pattern),
            )
        if "session_configs" in tables:
            connection.execute(
                "DELETE FROM session_configs WHERE session_id = ?",
                (session_id,),
            )
        for table in ("state_checkpoints", "state_snapshots", "state_scopes"):
            if table in tables:
                connection.execute(
                    f"DELETE FROM {table} WHERE namespace = ? "
                    "AND (scope_id = ? OR scope_id LIKE ? ESCAPE '\\')",
                    ("conversation", session_id, related_pattern),
                )
        if "memories" in tables:
            connection.execute(
                "DELETE FROM memories WHERE scope = ? OR scope LIKE ? ESCAPE '\\'",
                (f"session:{session_id}", f"session:{related_pattern}"),
            )
        if "context_sessions" in tables:
            connection.execute(
                "DELETE FROM context_sessions WHERE session_id = ?",
                (session_id,),
            )
        if "user_info" in tables:
            rows = connection.execute(
                "SELECT user_id, user_session FROM user_info"
            ).fetchall()
            for user_id, raw_sessions in rows:
                try:
                    sessions: object = json.loads(raw_sessions or "[]")
                except Exception:
                    sessions = []
                if not isinstance(sessions, list):
                    continue
                normalized = [str(item) for item in cast(list[object], sessions)]
                remaining = [item for item in normalized if item != session_id]
                if remaining != normalized:
                    connection.execute(
                        "UPDATE user_info SET user_session = ? WHERE user_id = ?",
                        (json.dumps(remaining, ensure_ascii=False), user_id),
                    )
        connection.commit()
