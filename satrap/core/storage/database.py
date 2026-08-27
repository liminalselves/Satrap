"""平台数据库中的会话级数据清理操作"""
from __future__ import annotations

import sqlite3
from pathlib import Path


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
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "chat_history" in tables:
            connection.execute(
                "DELETE FROM chat_history WHERE conversation_id = ?",
                (session_id,),
            )
        for table in ("state_checkpoints", "state_snapshots", "state_scopes"):
            if table in tables:
                connection.execute(
                    f"DELETE FROM {table} WHERE namespace = ? AND scope_id = ?",
                    ("conversation", session_id),
                )
        if "memories" in tables:
            connection.execute(
                "DELETE FROM memories WHERE scope = ?",
                (f"session:{session_id}",),
            )
        connection.commit()
