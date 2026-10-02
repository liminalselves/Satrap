"""
对话数据查看与编辑服务

分别管理模型上下文与 Chat 展示历史, 使用版本检查和事务备份防止并发覆盖,
由运行服务在实例操作锁内调用, 控制服务仅在目标服务停止时直接访问数据库
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any
import sqlite3
import hashlib
import copy
import json
import time
import uuid

from satrap.core.utils.context.utils import _load_message_content, _message_content_json
from satrap.core.storage.file_lock import database_session_lock
from satrap.core.utils.vision import content_text_projection


class ConversationDataConflict(ValueError):
    """数据版本变化或活动实例正在处理消息, 本次编辑不写入"""


def data_revision(value: object) -> str:
    """
    计算内容版本, 同时覆盖工具调用和展示回复版本

    参数:
    - value: 当前完整数据

    返回:
    - 稳定的内容哈希
    """
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


class ConversationDataService:
    """只操作指定平台数据库的对话数据, 不创建或激活 Agent 实例"""

    def __init__(self, database: str | Path) -> None:
        """
        绑定平台数据库

        参数:
        - database: 由服务端存储布局解析的数据库路径
        """
        self.database = Path(database)

    def _connect(self, *, readonly: bool = True) -> sqlite3.Connection:
        """
        打开已有数据库, 查询不会创建文件或迁移表

        参数:
        - readonly: 是否使用只读连接

        返回:
        - 使用命名行的 SQLite 连接
        """
        if not self.database.is_file():
            raise KeyError("平台尚无已保存的对话数据")
        connection = sqlite3.connect(self.database.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> set[str]:
        """
        读取已有表名

        参数:
        - connection: 数据库连接

        返回:
        - 已存在的数据表
        """
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def list_conversations(self, query: str = "", offset: int = 0, limit: int = 40) -> dict[str, Any]:
        """
        按 ID 或 Chat 标题查找保存过的对话, 包含工作流独立上下文

        参数:
        - query: 搜索关键词
        - offset: 分页偏移
        - limit: 每页数量, 最大 100

        返回:
        - 对话列表与总数, 空平台返回空列表
        """
        if not self.database.is_file():
            return {"items": [], "total": 0}
        with closing(self._connect()) as connection:
            tables = self._tables(connection)
            candidates: set[str] = set()
            roots: set[str] = set()
            for table, key in (("conversation_meta", "conversation_id"), ("session_configs", "session_id"), ("display_turns", "conversation_id")):
                if table in tables:
                    candidates.update(row[0] for row in connection.execute(f"SELECT DISTINCT {key} FROM {table}"))
            roots.update(candidates)
            counts = dict(connection.execute("SELECT conversation_id, COUNT(*) FROM chat_history GROUP BY conversation_id")) if "chat_history" in tables else {}
            if "conversation_data_backups" in tables:
                for row in connection.execute("SELECT DISTINCT conversation_id FROM conversation_data_backups"):
                    counts.setdefault(row[0], 0)
            owners: dict[str, list[str]] = {root: [] for root in roots}
            for context in counts:
                owner = next((root for root in sorted(roots, key=len, reverse=True) if context == root or context.startswith(root + "_")), context)
                candidates.add(owner)
                owners.setdefault(owner, []).append(context)
            items = []
            for conversation in candidates:
                contexts = sorted(owners.get(conversation, []), key=lambda value: (value != conversation + "_main", value))
                count = sum(counts.get(context, 0) for context in contexts)
                turns = connection.execute("SELECT COUNT(*) FROM display_turns WHERE conversation_id=?", (conversation,)).fetchone()[0] if "display_turns" in tables else 0
                title_row = connection.execute("SELECT user_input FROM display_turns WHERE conversation_id=? ORDER BY turn_index LIMIT 1", (conversation,)).fetchone() if turns else None
                title = str(title_row[0])[:100] if title_row else conversation
                if query.casefold() not in f"{conversation} {title} {' '.join(contexts)}".casefold():
                    continue
                items.append({"conversation_id": conversation, "title": title, "message_count": count, "history_count": turns, "context_ids": contexts or [conversation]})
            items.sort(key=lambda item: item["conversation_id"])
            return {"items": items[max(0, offset):max(0, offset) + min(100, max(1, limit))], "total": len(items)}

    def _context(self, connection: sqlite3.Connection, conversation: str) -> list[dict[str, Any]]:
        """
        无损读取消息内容、工具调用及思考字段

        参数:
        - connection: 数据库连接
        - conversation: 对话 ID

        返回:
        - 按原始顺序排列的模型消息
        """
        if "chat_history" not in self._tables(connection):
            return []
        messages = []
        for row in connection.execute("SELECT * FROM chat_history WHERE conversation_id=? ORDER BY id", (conversation,)):
            message = {"role": row["role"], "content": _load_message_content(row["content"], row["content_json"] if "content_json" in row.keys() else None)}
            for key in ("tool_calls", "tool_call_id", "reasoning_content"):
                if key in row.keys() and row[key] is not None:
                    message[key] = json.loads(row[key]) if key == "tool_calls" else row[key]
            messages.append(message)
        return messages

    @staticmethod
    def _require_conversation(connection: sqlite3.Connection, conversation: str, live_messages: list[dict[str, Any]] | None) -> None:
        """
        拒绝不存在的数据标识, 不因查看或编辑而创建空对话

        参数:
        - connection: 当前数据库连接
        - conversation: 对话或上下文 ID
        - live_messages: 已确认归属的活动上下文
        """
        if live_messages is not None:
            return
        tables = ConversationDataService._tables(connection)
        for table, column in (("chat_history", "conversation_id"), ("conversation_meta", "conversation_id"), ("session_configs", "session_id"), ("display_turns", "conversation_id"), ("conversation_data_backups", "conversation_id")):
            if table in tables and connection.execute(f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (conversation,)).fetchone():
                return
        raise KeyError("对话或上下文不存在")

    def _history(self, connection: sqlite3.Connection, conversation: str) -> dict[str, list[dict[str, Any]]]:
        """
        获取独立展示历史及其回复版本和工具明细

        参数:
        - connection: 数据库连接
        - conversation: 对话 ID

        返回:
        - 按表组织的完整展示数据, 不包含模型上下文
        """
        tables = self._tables(connection)
        result: dict[str, list[dict[str, Any]]] = {"display_turns": [], "display_turn_variants": [], "display_tool_calls": []}
        if "display_turns" in tables:
            result["display_turns"] = [dict(row) for row in connection.execute("SELECT * FROM display_turns WHERE conversation_id=? ORDER BY turn_index", (conversation,))]
            for table in ("display_turn_variants", "display_tool_calls"):
                if table in tables:
                    result[table] = [dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE turn_id IN (SELECT id FROM display_turns WHERE conversation_id=?) ORDER BY id", (conversation,))]
        return result

    def read(self, conversation: str, layer: str, offset: int = 0, limit: int = 50, *, live_messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """
        查看上下文或独立展示历史, 返回编辑所需的完整内容版本

        参数:
        - conversation: 对话 ID
        - layer: context 或 history
        - offset: 分页偏移
        - limit: 每页数量
        - live_messages: 活动实例的最新内存上下文, None 表示读取磁盘

        返回:
        - 分页数据、版本和修改备份目录
        """
        if layer not in {"context", "history"}:
            raise ValueError("未知数据层")
        with closing(self._connect()) as connection:
            self._require_conversation(connection, conversation, live_messages)
            value: Any = copy.deepcopy(live_messages) if layer == "context" and live_messages is not None else self._context(connection, conversation) if layer == "context" else self._history(connection, conversation)
            items = value if layer == "context" else [{**turn, "variants": [row for row in value["display_turn_variants"] if row["turn_id"] == turn["id"]], "tool_call_records": [row for row in value["display_tool_calls"] if row["turn_id"] == turn["id"]]} for turn in value["display_turns"]]
            backups = [dict(row) for row in connection.execute("SELECT id, layer, reason, created_at FROM conversation_data_backups WHERE conversation_id=? AND layer=? ORDER BY created_at DESC LIMIT 50", (conversation, layer))] if "conversation_data_backups" in self._tables(connection) else []
            offset = max(0, offset)
            return {"ok": True, "conversation_id": conversation, "layer": layer, "revision": data_revision(value), "total": len(items), "items": [{**item, "index": index} for index, item in enumerate(items) if offset <= index < offset + min(100, max(1, limit))], "backups": backups, "source": "memory" if live_messages is not None and layer == "context" else "storage"}

    @staticmethod
    def _edit_context(messages: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
        """
        编辑消息或删除完整工具调用组, 保留消息的结构字段

        参数:
        - messages: 当前完整消息快照
        - payload: 操作及编辑参数

        返回:
        - 新快照, 非法索引或内容抛出 ValueError
        """
        result = copy.deepcopy(messages)
        action = payload.get("action")
        if action == "clear":
            if not isinstance(payload.get("keep_system", True), bool):
                raise ValueError("保留系统提示词选项必须是布尔值")
            return [item for item in result if item.get("role") in {"system", "developer"}] if payload.get("keep_system", True) else []
        index = payload.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(result):
            raise ValueError("消息索引无效, 请重新读取")
        if action == "edit":
            content = payload.get("content")
            if not isinstance(content, (str, list)) and content is not None:
                raise ValueError("消息内容必须是文本、多模态数组或 null")
            if isinstance(content, list) and any(not isinstance(part, dict) or not isinstance(part.get("type"), str) for part in content):
                raise ValueError("多模态内容必须是包含 type 字段的对象数组")
            reasoning = payload.get("reasoning_content", result[index].get("reasoning_content"))
            if reasoning is not None and not isinstance(reasoning, str):
                raise ValueError("思考内容必须是文本或 null")
            result[index]["content"] = content
            if reasoning is not None:
                result[index]["reasoning_content"] = reasoning
            else:
                result[index].pop("reasoning_content", None)
            return result
        if action != "delete":
            raise ValueError("未知编辑操作")
        removed = {index}
        calls = {call["id"] for call in result[index].get("tool_calls", [])}
        tool_id = result[index].get("tool_call_id")
        if tool_id:
            for position, item in enumerate(result):
                if any(call.get("id") == tool_id for call in item.get("tool_calls", [])):
                    removed.add(position)
                    calls.update(call["id"] for call in item.get("tool_calls", []))
        removed.update(position for position, item in enumerate(result) if item.get("tool_call_id") in calls)
        return [item for position, item in enumerate(result) if position not in removed]

    @staticmethod
    def _write_context(connection: sqlite3.Connection, conversation: str, messages: list[dict[str, Any]]) -> None:
        """
        事务内替换上下文并清除失效的总结与指针快照

        参数:
        - connection: 当前写事务
        - conversation: 对话 ID
        - messages: 新的完整消息快照
        """
        connection.execute("DELETE FROM chat_history WHERE conversation_id=?", (conversation,))
        for item in messages:
            connection.execute("INSERT INTO chat_history (conversation_id, role, content, content_json, tool_call_id, tool_calls, reasoning_content) VALUES (?, ?, ?, ?, ?, ?, ?)", (conversation, item["role"], None if item.get("content") is None else content_text_projection(item.get("content")), _message_content_json(item.get("content")), item.get("tool_call_id"), json.dumps(item["tool_calls"], ensure_ascii=False) if item.get("tool_calls") else None, item.get("reasoning_content")))
        tables = ConversationDataService._tables(connection)
        if "context_runtime_state" in tables:
            connection.execute("DELETE FROM context_runtime_state WHERE conversation_id=?", (conversation,))
        if "state_checkpoints" in tables:
            connection.execute("DELETE FROM state_checkpoints WHERE namespace='conversation' AND scope_id=? AND snapshot_id=''", (conversation,))
        # 已物化快照保留, 依赖旧消息顺序的指针检查点失效

    @staticmethod
    def _write_history(connection: sqlite3.Connection, conversation: str, history: dict[str, list[dict[str, Any]]]) -> None:
        """
        事务内替换展示数据, 保持各回复版本和工具明细关联

        参数:
        - connection: 当前写事务
        - conversation: 对话 ID
        - history: 新的展示表数据, 不含模型上下文
        """
        tables = ConversationDataService._tables(connection)
        for table in ("display_tool_calls", "display_turn_variants"):
            if table in tables:
                connection.execute(f"DELETE FROM {table} WHERE turn_id IN (SELECT id FROM display_turns WHERE conversation_id=?)", (conversation,))
        connection.execute("DELETE FROM display_turns WHERE conversation_id=?", (conversation,))
        for table in ("display_turns", "display_turn_variants", "display_tool_calls"):
            for row in history[table]:
                keys = list(row)
                connection.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", [row[key] for key in keys])

    def mutate(self, conversation: str, layer: str, payload: dict[str, Any], *, live_messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """
        检查版本后原子保存单个数据层, 写入前创建可恢复备份

        参数:
        - conversation: 对话 ID
        - layer: context 或 history
        - payload: 操作、版本与编辑字段
        - live_messages: 操作锁内读取的活动上下文, None 表示磁盘状态

        返回:
        - 保存标识、备份 ID 和完整新上下文, 冲突或写入失败不改变数据
        """
        if layer not in {"context", "history"}:
            raise ValueError("未知数据层")
        with database_session_lock(self.database, conversation), closing(self._connect(readonly=False)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_conversation(connection, conversation, live_messages)
            previous: Any = copy.deepcopy(live_messages) if layer == "context" and live_messages is not None else self._context(connection, conversation) if layer == "context" else self._history(connection, conversation)
            if payload.get("expected_revision") != data_revision(previous):
                raise ConversationDataConflict("对话数据已变化, 请重新读取后合并修改")
            updated: Any
            action = payload.get("action")
            if action == "checkpoint" and layer == "context":
                if "state_checkpoints" not in self._tables(connection):
                    raise KeyError("此上下文尚无检查点")
                checkpoint = connection.execute("SELECT * FROM state_checkpoints WHERE checkpoint_id=? AND namespace='conversation' AND scope_id=?", (payload.get("checkpoint_id"), conversation)).fetchone()
                if checkpoint is None:
                    raise KeyError("检查点不存在或不属于此上下文")
                if not checkpoint["snapshot_id"]:
                    if checkpoint["position"] > len(previous):
                        raise ValueError("指针检查点已失效")
                    updated = copy.deepcopy(previous[:checkpoint["position"]])
                else:
                    snapshot = connection.execute("SELECT snapshot_json FROM state_snapshots WHERE snapshot_id=?", (checkpoint["snapshot_id"],)).fetchone()
                    if snapshot is None:
                        raise KeyError("检查点快照不存在")
                    rows = json.loads(snapshot[0])["domains"]["messages"]
                    updated = []
                    for row in rows:
                        message = {"role": row["role"], "content": _load_message_content(row.get("content"), row.get("content_json"))}
                        for key in ("tool_calls", "tool_call_id", "reasoning_content"):
                            if row.get(key) is not None:
                                message[key] = json.loads(row[key]) if key == "tool_calls" else row[key]
                        updated.append(message)
            elif action == "restore":
                if "conversation_data_backups" not in self._tables(connection):
                    raise KeyError("修改备份不存在")
                backup = connection.execute("SELECT payload FROM conversation_data_backups WHERE id=? AND conversation_id=? AND layer=?", (payload.get("backup_id"), conversation, layer)).fetchone()
                if backup is None:
                    raise KeyError("修改备份不存在")
                updated = json.loads(backup[0])
            elif layer == "context":
                updated = self._edit_context(previous, payload)
            else:
                updated = copy.deepcopy(previous)
                if action == "clear":
                    updated = {table: [] for table in previous}
                else:
                    index = payload.get("index")
                    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(updated["display_turns"]):
                        raise ValueError("历史轮次索引无效")
                    turn = updated["display_turns"][index]
                    if action == "delete":
                        updated["display_turns"].pop(index)
                        for table in ("display_turn_variants", "display_tool_calls"):
                            updated[table] = [row for row in updated[table] if row["turn_id"] != turn["id"]]
                    elif action == "edit":
                        for key in ("user_input", "answer", "thinking"):
                            if key in payload:
                                if not isinstance(payload[key], str):
                                    raise ValueError(f"{key} 必须是文本")
                                turn[key] = payload[key]
                        if "answer" in payload or "thinking" in payload:
                            turn["segments"] = None
                        for variant in updated["display_turn_variants"]:
                            if variant["turn_id"] == turn["id"] and variant["variant_index"] == turn.get("active_variant", 0):
                                for key in ("answer", "thinking", "segments"):
                                    variant[key] = turn.get(key)
                    else:
                        raise ValueError("未知编辑操作")
            connection.execute("CREATE TABLE IF NOT EXISTS conversation_data_backups (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, layer TEXT NOT NULL, payload TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL)")
            backup_id = uuid.uuid4().hex
            connection.execute("INSERT INTO conversation_data_backups VALUES (?, ?, ?, ?, ?, ?)", (backup_id, conversation, layer, json.dumps(previous, ensure_ascii=False, allow_nan=False), str(action), time.time()))
            if layer == "context":
                self._write_context(connection, conversation, updated)
            else:
                self._write_history(connection, conversation, updated)
        return {"ok": True, "saved": True, "backup_id": backup_id, "messages": updated if layer == "context" else None}
