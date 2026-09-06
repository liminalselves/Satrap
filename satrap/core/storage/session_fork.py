"""分支会话的参数覆盖和私有知识库复制"""
from __future__ import annotations

from contextlib import ExitStack, closing
from pathlib import Path
import sqlite3
import shutil
import json
import time
import uuid

from satrap.core.storage.file_lock import session_storage_lock
from satrap.core.storage.layout import StorageLayout


def fork_session_settings(layout: StorageLayout, platform_id: str, source_id: str, target_id: str) -> dict[str, str]:
    """复制当前参数与知识库快照, 新库使用独立 ID, 全局库引用保持原值"""
    if not source_id or not target_id or source_id == target_id:
        raise ValueError("分支需要两个不同的会话 ID")
    database = layout.platform_db(platform_id)
    if not database.exists():
        return {}
    created: list[Path] = []
    mapping: dict[str, str] = {}
    try:
        with ExitStack() as locks, closing(sqlite3.connect(database, timeout=30)) as connection, connection:
            for session_id in sorted((source_id, target_id)):
                locks.enter_context(session_storage_lock(layout, platform_id, session_id))
            connection.row_factory = sqlite3.Row
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            kb_rows = connection.execute("SELECT * FROM rag_knowledge_bases WHERE scope='session' AND session_id=? ORDER BY id", (source_id,)).fetchall() if "rag_knowledge_bases" in tables else []
            for row in kb_rows:
                if not row["id"].isalnum() or (row["generation"] and not row["generation"].isalnum()):
                    raise ValueError("源知识库路径标识无效")
            connection.execute("BEGIN IMMEDIATE")
            if "rag_knowledge_bases" in tables:
                current = connection.execute("SELECT * FROM rag_knowledge_bases WHERE scope='session' AND session_id=? ORDER BY id", (source_id,)).fetchall()
                if [dict(row) for row in current] != [dict(row) for row in kb_rows]:
                    raise ValueError("源知识库在分支期间发生变化, 请重试")
                if connection.execute("SELECT 1 FROM rag_knowledge_bases WHERE session_id=?", (target_id,)).fetchone():
                    raise ValueError("目标会话已有知识库")
            for row in kb_rows:
                new_id = uuid.uuid4().hex
                mapping[row["id"]] = new_id
                if row["generation"]:
                    source = layout.session_indexes(platform_id, source_id) / "rag" / row["id"] / row["generation"] / "metadata.sqlite"
                    if not source.is_file():
                        raise ValueError("源知识库索引文件缺失")
                    target = layout.session_indexes(platform_id, target_id) / "rag" / new_id
                    target.mkdir(parents=True, exist_ok=False)
                    created.append(target)
                    generation = target / row["generation"]
                    generation.mkdir()
                    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(generation / "metadata.sqlite")) as copied:
                        original.backup(copied)
                values = dict(row)
                values.update(id=new_id, session_id=target_id, revision=1, created_at=time.time(), updated_at=time.time())
                columns = ",".join(values)
                connection.execute(f"INSERT INTO rag_knowledge_bases ({columns}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
            if "session_config_overrides" in tables:
                if connection.execute("SELECT 1 FROM session_config_overrides WHERE session_id=?", (target_id,)).fetchone():
                    raise ValueError("目标会话已有参数覆盖")
                rows = connection.execute("SELECT * FROM session_config_overrides WHERE session_id=?", (source_id,)).fetchall()
                for row in rows:
                    values = json.loads(row["config_json"])
                    if row["namespace"] == "plugins.rag":
                        if "session_db_ids" in values:
                            values["session_db_ids"] = [mapping.get(item, item) for item in values["session_db_ids"]]
                        if "write_db_id" in values:
                            values["write_db_id"] = mapping.get(values["write_db_id"], values["write_db_id"])
                    connection.execute(
                        "INSERT INTO session_config_overrides (session_id,namespace,config_json,schema_version,revision,updated_at) VALUES (?,?,?,?,1,?)",
                        (target_id, row["namespace"], json.dumps(values, ensure_ascii=False), row["schema_version"], time.time()),
                    )
        return mapping
    except BaseException:
        for target in created:
            shutil.rmtree(target)
        raise
