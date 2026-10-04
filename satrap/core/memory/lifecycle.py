"""在调用方事务中硬删除记忆正文, 不将内容复制到删除审计"""
from __future__ import annotations

from collections.abc import Sequence
import json
import sqlite3
import uuid


def erase_memory(connection: sqlite3.Connection, scope_key: str, identity: str, *, actor: str, now: float) -> None:
    """
    删除一条已授权范围内的记忆及含旧内容的提案

    参数:
    - connection: 调用方已取得写权限的事务
    - scope_key: 完整对话身份键
    - identity: 同一范围内的记忆 ID
    - actor: 可信操作者
    - now: 操作时间
    """
    connection.execute("DELETE FROM memory_refs WHERE scope_key=? AND memory_id=?", (scope_key, identity))
    connection.execute("DELETE FROM memories WHERE scope=? AND id=?", (scope_key, identity))
    connection.execute("UPDATE memory_proposals SET payload_json='{}', state=CASE WHEN state='pending' THEN 'conflicted' ELSE state END "
                       "WHERE scope_key=? AND memory_id=?", (scope_key, identity))
    connection.execute("INSERT INTO memory_audit VALUES (?, ?, ?, ?, 'delete', ?)",
                       (uuid.uuid4().hex, scope_key, identity, actor, now))


def cleanup_memories(connection: sqlite3.Connection, scope_key: str, *, sources: Sequence[str] | None, actor: str, now: float) -> dict[str, int]:
    """
    按明确选择删除当前对话全部记忆或与所选来源有关的记忆和提案

    参数:
    - connection: 档案管理持有的同一写事务
    - scope_key: 完整对话范围, 不能只按消息 ID 匹配
    - sources: 选定来源 ID, None 表示全部记忆
    - actor: 已认证管理入口身份
    - now: 操作时间

    返回:
    - 实际硬删除的记忆数量及清理的提案数量
    """
    if sources is None:
        identities = [row[0] for row in connection.execute("SELECT id FROM memories WHERE scope=?", (scope_key,))]
    elif sources:
        placeholders = ",".join("?" for _ in sources)
        identities = [row[0] for row in connection.execute(
            "SELECT DISTINCT m.id FROM memories m JOIN memory_refs r ON r.memory_id=m.id AND r.scope_key=m.scope "
            f"WHERE m.scope=? AND r.message_id IN ({placeholders})", (scope_key, *sources))]
    else:
        identities = []
    proposals = connection.execute("SELECT proposal_id, payload_json, memory_id FROM memory_proposals WHERE scope_key=?", (scope_key,)).fetchall()
    affected = []
    selected = set(sources or [])
    for proposal in proposals:
        payload = json.loads(proposal[1])
        if sources is None or proposal[2] in identities or selected.intersection(payload.get("source_message_ids", [])):
            affected.append(proposal[0])
    for identity in identities:
        erase_memory(connection, scope_key, identity, actor=actor, now=now)
    connection.executemany("UPDATE memory_proposals SET payload_json='{}', state=CASE WHEN state='pending' THEN 'conflicted' ELSE state END "
                           "WHERE scope_key=? AND proposal_id=?", [(scope_key, identity) for identity in affected])
    return {"deleted_memory_count": len(identities), "cleared_memory_proposal_count": len(affected)}
