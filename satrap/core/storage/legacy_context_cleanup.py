"""
旧子代理上下文的可恢复清理

只处理精确匹配默认子代理提示词且无正式引用的单条系统消息,
写入前锁定目标并重验快照, 备份包含校验和及相关状态, 不使用前缀批量删除
"""
from __future__ import annotations

from contextlib import closing, ExitStack
from pathlib import Path
from typing import Any
import traceback
import argparse
import hashlib
import sqlite3
import uuid
import json
import time
import os

from satrap.core.storage.file_lock import database_session_lock
from satrap.core.storage.layout import StorageLayout

from satrap.core.log import logger


_DOMAINS = {
    "chat_history": "conversation_id", "context_runtime_state": "conversation_id",
    "context_catalog": "context_id", "state_scopes": "scope_id",
    "state_checkpoints": "scope_id", "state_snapshots": "scope_id",
}


def _checksum(value: object) -> str:
    """
    计算稳定 JSON 校验和

    参数:
    - value: 待验证的快照或清单

    返回:
    - SHA256 校验和
    """
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()


class LegacyContextCleanup:
    """为一个平台数据库预览, 备份, 清理及恢复旧子代理记录"""

    def __init__(self, database: str | Path) -> None:
        """
        绑定现有数据库, 不创建或迁移数据库

        参数:
        - database: 平台数据库路径
        """
        self.database = Path(database).resolve()

    def _connect(self, readonly: bool = True) -> sqlite3.Connection:
        """
        打开现有数据库

        参数:
        - readonly: 是否仅用于预览, 默认 True

        返回:
        - 命名行连接, 缺少数据库时抛出错误交由 CLI 边界记录
        """
        connection = sqlite3.connect(self.database.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _snapshot(connection: sqlite3.Connection, tables: set[str], identity: str) -> dict[str, list[dict[str, Any]]]:
        """
        按精确 ID 导出消息及其状态

        参数:
        - connection: 当前连接
        - tables: 已存在的表
        - identity: 候选上下文 ID

        返回:
        - 可恢复的原始记录, 不包括其它会话
        """
        result = {}
        for table, column in _DOMAINS.items():
            if table in tables:
                suffix = " AND namespace='conversation'" if table.startswith("state_") else ""
                result[table] = [dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE {column}=?{suffix} ORDER BY rowid", (identity,))]
        return result

    @staticmethod
    def _references(connection: sqlite3.Connection, tables: set[str]) -> set[str]:
        """
        收集会话和展示层正式引用, 格式无效时拒绝维护操作

        参数:
        - connection: 当前连接
        - tables: 已存在的表

        返回:
        - 被引用的会话或上下文身份
        """
        references = set()
        for table, column in (("session_configs", "session_id"), ("context_sessions", "session_id"), ("conversation_meta", "conversation_id"), ("display_turns", "conversation_id"), ("conversation_data_backups", "conversation_id"), ("agent_runs", "scope")):
            if table in tables:
                references.update(str(row[0]) for row in connection.execute(f"SELECT DISTINCT {column} FROM {table}"))
        if "context_catalog" in tables:
            references.update(row[0] for row in connection.execute("SELECT context_id FROM context_catalog WHERE session_id IS NOT NULL OR context_kind<>'standalone'"))
        if "user_info" in tables:
            for row in connection.execute("SELECT user_session FROM user_info"):
                sessions = json.loads(row[0] or "[]")
                if not isinstance(sessions, list):
                    raise ValueError("用户会话引用格式无效, 无法安全清理")
                references.update(str(value) for value in sessions)
        return references

    def _preview(self, connection: sqlite3.Connection) -> dict[str, Any]:
        """
        校验消息形态, 提示词和正式引用

        参数:
        - connection: 当前快照连接

        返回:
        - 候选精确 ID, 记录快照和需要人工判断的拒绝项
        """
        from satrap.expend.tools.agent.utils import SUB_AGENT_SYSTEM_PROMPT

        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "chat_history" not in tables:
            return {"candidates": {}, "rejected": []}
        references = self._references(connection, tables)
        identities = [row[0] for row in connection.execute("SELECT DISTINCT conversation_id FROM chat_history ORDER BY conversation_id") if str(row[0]).startswith("sub_agent_")]
        candidates = {}
        rejected = []
        for identity in identities:
            rows = [dict(row) for row in connection.execute("SELECT * FROM chat_history WHERE conversation_id=? ORDER BY id", (identity,))]
            reason = ""
            if len(rows) != 1 or rows[0]["role"] != "system" or rows[0].get("content") != SUB_AGENT_SYSTEM_PROMPT:
                reason = "消息或提示词不匹配"
            elif any(rows[0].get(key) for key in ("tool_calls", "tool_call_id", "reasoning_content")):
                reason = "包含工具或思考记录"
            elif any(identity == reference or identity.startswith(reference + "_") for reference in references):
                reason = "存在正式引用"
            elif rows[0].get("content_json") and json.loads(rows[0]["content_json"]) != SUB_AGENT_SYSTEM_PROMPT:
                reason = "结构化消息内容不匹配"
            snapshot = self._snapshot(connection, tables, identity)
            if not reason and any(row.get("summary") or row.get("api_input_tokens") is not None for row in snapshot.get("context_runtime_state", [])):
                reason = "包含已运行任务的摘要或用量"
            if reason:
                rejected.append({"id": identity, "reason": reason})
            else:
                candidates[identity] = snapshot
        return {"candidates": candidates, "rejected": rejected}

    def preview(self) -> dict[str, Any]:
        """
        只读生成清理清单, 不创建锁或备份

        返回:
        - 带快照版本的候选清单
        """
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            result = self._preview(connection)
            return {**result, "revision": _checksum(result["candidates"])}

    def apply(self, preview: dict[str, Any], backup: str | Path) -> dict[str, Any]:
        """
        锁定清单中的精确 ID, 重验后先备份再原子删除

        参数:
        - preview: 调用方已查看的候选快照和版本
        - backup: 新备份文件路径, 已存在时拒绝覆盖

        返回:
        - 删除数量和备份路径, 校验冲突或备份失败时不删除数据
        """
        identities = sorted(preview["candidates"])
        if not identities:
            return {"deleted": 0, "backup": None}
        with ExitStack() as locks:
            for identity in identities:
                locks.enter_context(database_session_lock(self.database, identity))
            with closing(self._connect(False)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._preview(connection)
                selected = {identity: current["candidates"][identity] for identity in identities if identity in current["candidates"]}
                if _checksum(selected) != preview["revision"] or _checksum(preview["candidates"]) != preview["revision"]:
                    raise ValueError("候选数据已变化, 请重新预览")
                body = {"format": 1, "database": str(self.database), "created_at": time.time(), "records": selected}
                archive = {"body": body, "checksum": _checksum(body)}
                path = Path(backup).resolve()
                if path == self.database:
                    raise ValueError("备份路径不能指向数据库")
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("x", encoding="utf-8") as stream:
                    json.dump(archive, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                verified = json.loads(path.read_text(encoding="utf-8"))
                if _checksum(verified["body"]) != verified["checksum"]:
                    raise ValueError("备份校验失败")
                for identity, records in selected.items():
                    for table in records:
                        suffix = " AND namespace='conversation'" if table.startswith("state_") else ""
                        connection.execute(f"DELETE FROM {table} WHERE {_DOMAINS[table]}=?{suffix}", (identity,))
                connection.commit()
                logger.info(f"[旧上下文清理] 已备份并清理: {len(identities)} 个, backup={path}")
                return {"deleted": len(identities), "backup": str(path)}

    def restore(self, backup: str | Path) -> dict[str, Any]:
        """
        校验备份后原子恢复精确记录, 已占用的身份拒绝覆盖

        参数:
        - backup: apply 产生的完整备份

        返回:
        - 恢复数量, 任何冲突或非法字段均不写入
        """
        archive = json.loads(Path(backup).read_text(encoding="utf-8"))
        body = archive["body"]
        if _checksum(body) != archive["checksum"] or body["format"] != 1 or body["database"] != str(self.database):
            raise ValueError("备份校验失败或数据库身份不匹配")
        records = body["records"]
        with ExitStack() as locks:
            for identity in sorted(records):
                locks.enter_context(database_session_lock(self.database, identity))
            with closing(self._connect(False)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                references = self._references(connection, tables)
                for identity in records:
                    if not identity.startswith("sub_agent_") or any(identity == reference or identity.startswith(reference + "_") for reference in references):
                        raise ValueError(f"上下文身份存在正式引用: {identity}")
                for identity, domains in records.items():
                    for table, rows in domains.items():
                        if table not in _DOMAINS:
                            raise ValueError("备份包含未知数据域")
                        column = _DOMAINS[table]
                        suffix = " AND namespace='conversation'" if table.startswith("state_") else ""
                        if connection.execute(f"SELECT 1 FROM {table} WHERE {column}=?{suffix} LIMIT 1", (identity,)).fetchone():
                            raise ValueError(f"上下文身份已占用: {identity}")
                        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                        for row in rows:
                            if not row or set(row) - columns or row.get(column) != identity or (table.startswith("state_") and row.get("namespace") != "conversation"):
                                raise ValueError("备份字段或上下文身份无效")
                            keys = list(row)
                            connection.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", [row[key] for key in keys])
                connection.commit()
                logger.info(f"[旧上下文清理] 已恢复: {len(records)} 个, backup={backup}")
                return {"restored": len(records)}


def main() -> int:
    """
    命令行维护入口, 捕获并记录失败且返回非零状态

    返回:
    - 成功返回 0, 操作失败返回 1
    """
    parser = argparse.ArgumentParser(description="预览和可恢复清理旧子代理上下文")
    parser.add_argument("action", choices=["preview", "apply", "restore"])
    parser.add_argument("--data-root")
    parser.add_argument("--platform-id", default="local")
    parser.add_argument("--backup")
    parser.add_argument("--preview-file")
    arguments = parser.parse_args()
    try:
        layout = StorageLayout(arguments.data_root)
        service = LegacyContextCleanup(layout.platform_db(arguments.platform_id))
        if arguments.action == "restore":
            if not arguments.backup:
                raise ValueError("恢复需要指定 --backup")
            result = service.restore(arguments.backup)
        else:
            preview = json.loads(Path(arguments.preview_file).read_text(encoding="utf-8")) if arguments.action == "apply" and arguments.preview_file else service.preview()
            result = {"candidates": len(preview["candidates"]), "ids": list(preview["candidates"]), "rejected": preview["rejected"], "revision": preview["revision"]}
            if arguments.action == "preview" and arguments.preview_file:
                with Path(arguments.preview_file).open("x", encoding="utf-8") as stream:
                    json.dump(preview, stream, ensure_ascii=False, indent=2)
            if arguments.action == "apply":
                backup = arguments.backup or str(layout.trash_root(arguments.platform_id) / "legacy-contexts" / (uuid.uuid4().hex + ".json"))
                result = service.apply(preview, backup)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        logger.error(f"[旧上下文清理] 操作失败: {arguments.action}, {error}\n{traceback.format_exc()}")
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
