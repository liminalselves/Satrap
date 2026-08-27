"""
长期记忆存储: SQLite 结构化记忆, 模型自驱增删改

设计 (对齐 proj_astro 模式, 无 embedding 依赖):
- 记忆 = title + content + tags + importance 的结构化文本, 全部注入 system prompt
- 模式三档: disabled (不注入/不可用) / base (只读) / full (可增删改)
- 注入: 全量注入 + importance 降序 + max_entries 截断, 控制 token 成本
- 作用域: 每个实例必须绑定一个具体会话, 不支持跨会话可见集合

数据默认写入当前平台实例的 platform.db
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from satrap.core.utils.paths import get_db_path

DEFAULT_MEMORY_DB = Path(get_db_path())
"""local 平台的默认记忆数据库路径"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    importance INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
"""


class MemoryStore:
    """SQLite 长期记忆存储 (线程安全, 每次操作独立连接)"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        mode: str = "full",
        max_entries: int = 30,
        *,
        scope: str,
    ) -> None:
        """
        参数:
        - db_path: 数据库路径, 默认 local 平台的 platform.db
        - mode: 记忆模式, disabled / base / full
        - max_entries: 注入上限条数 (importance 降序), 默认 30
        - scope: 会话作用域, 必须是非空值
        """
        if not scope.strip():
            raise ValueError("记忆 scope 必须绑定到具体会话")
        self.db_path = Path(db_path or DEFAULT_MEMORY_DB)
        self.mode = mode
        self.max_entries = max_entries
        self.scope = scope.strip()
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        """
        打开新连接 (行级访问, 避免跨线程共享)

        返回:
        - sqlite3.Connection: 打开新连接 (行级访问, 避免跨线程共享)
        """
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _scope_filter(self) -> tuple[str, tuple[str]]:
        """生成当前会话的 scope 过滤条件"""
        return " AND scope=?", (self.scope,)

    # ---------- 模式管理 ----------

    def set_mode(self, mode: str) -> None:
        """
        切换记忆模式: disabled / base / full

        参数:
        - mode: 模式
        """
        if mode not in ("disabled", "base", "full"):
            raise ValueError(f"未知记忆模式: {mode}, 可选 disabled / base / full")
        self.mode = mode

    def can_write(self) -> bool:
        """
        当前是否允许增删改 (full 模式)

        返回:
        - bool: 当前是否允许增删改 (full 模式)
        """
        return self.mode == "full"

    def write_denied_reason(self, action: str) -> str:
        """
        写拒绝原因文案 (按实际模式生成)

        参数:
        - action: 操作类型

        返回:
        - str: 写拒绝原因文案 (按实际模式生成)
        """
        if self.mode == "disabled":
            return f"记忆功能已禁用 (disabled), 无法{action}"
        return f"记忆处于只读模式 (base), 无法{action}"

    def _write_denied_error(self, action: str) -> dict[str, Any]:
        """
        写拒绝错误 (按实际模式生成文案)

        参数:
        - action: 操作类型

        返回:
        - dict[str, Any]: 写拒绝错误 (按实际模式生成文案)
        """
        return {"error": self.write_denied_reason(action), "ok": False}

    # ---------- 增删改查 ----------

    def add(
        self,
        title: str,
        content: str,
        tags: list[str] | None = None,
        importance: int = 1,
    ) -> dict[str, Any]:
        """
        添加一条记忆, 返回记忆记录

        参数:
        - title: 标题
        - content: 内容
        - tags: 标签集合
        - importance: 重要度

        返回:
        - dict[str, Any]: 记忆记录
        """
        if not self.can_write():
            return self._write_denied_error("添加")
        if not title.strip() or not content.strip():
            return {"error": "title 与 content 不能为空", "ok": False}
        memory_id = uuid.uuid4().hex
        now = datetime.now().isoformat(timespec="seconds")
        record: dict[str, Any] = {
            "id": memory_id,
            "scope": self.scope,
            "title": title.strip(),
            "content": content.strip(),
            "tags": [str(t) for t in (tags or [])],
            "importance": int(importance),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO memories (id, scope, title, content, tags, importance, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["id"], record["scope"], record["title"], record["content"],
                    json.dumps(record["tags"], ensure_ascii=False),
                    record["importance"], record["created_at"], record["updated_at"],
                ),
            )
        return {"memory_id": memory_id, "title": record["title"], "content": record["content"], "status": "added", "ok": True}

    def update(self, memory_id: str, **fields: Any) -> dict[str, Any]:
        """
        按 ID 更新记忆 (title/content/tags/importance), 返回更新后记录

        参数:
        - memory_id: 记忆 ID
        - fields: 字段集合

        返回:
        - dict[str, Any]: 更新后记录
        """
        if not self.can_write():
            return self._write_denied_error("更新")
        updates: dict[str, Any] = {}
        for key in ("title", "content", "importance"):
            if key in fields and fields[key] is not None:
                if isinstance(fields[key], str):
                    updates[key] = fields[key].strip()
                elif key == "importance":
                    try:
                        updates[key] = int(fields[key])
                    except (TypeError, ValueError):
                        return {"error": "importance 必须是整数", "ok": False}
                else:
                    updates[key] = fields[key]
        if "tags" in fields and fields["tags"] is not None:
            updates["tags"] = json.dumps([str(t) for t in cast(list[Any], fields["tags"])], ensure_ascii=False)
        if not updates:
            return {"error": "没有可更新的字段", "ok": False}
        updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            resolved = self._resolve_memory_id(conn, memory_id)
            if resolved is None:
                return {"error": f"记忆不存在: {memory_id}", "ok": False}
            existing = conn.execute(
                "SELECT * FROM memories WHERE id=? AND scope=?",
                (resolved, self.scope),
            ).fetchone()
            conn.execute(
                "UPDATE memories SET title=?, content=?, tags=?, importance=?, updated_at=? WHERE id=? AND scope=?",
                (
                    updates.get("title", existing["title"]),
                    updates.get("content", existing["content"]),
                    updates.get("tags", existing["tags"]),
                    updates.get("importance", existing["importance"]),
                    updates["updated_at"],
                    resolved, self.scope,
                ),
            )
        return self.get(resolved)

    def delete(self, memory_id: str) -> dict[str, Any]:
        """
        按 ID (或唯一前缀) 删除记忆

        参数:
        - memory_id: 记忆 ID

        返回:
        - dict[str, Any]: 按 ID (或唯一前缀) 删除记忆
        """
        if not self.can_write():
            return self._write_denied_error("删除")
        clause, params = self._scope_filter()
        with self._lock, self._connect() as conn:
            resolved = self._resolve_memory_id(conn, memory_id)
            if resolved is None:
                return {"error": f"记忆不存在: {memory_id}", "ok": False}
            cursor = conn.execute(
                f"DELETE FROM memories WHERE id=?{clause}",
                (resolved, *params),
            )
        if cursor.rowcount == 0:
            return {"error": f"记忆不存在: {memory_id}", "ok": False}
        return {"memory_id": resolved, "status": "deleted", "ok": True}

    def get(self, memory_id: str) -> dict[str, Any]:
        """
        按 ID (或唯一前缀) 获取单条记忆

        参数:
        - memory_id: 记忆 ID

        返回:
        - dict[str, Any]: 按 ID (或唯一前缀) 获取单条记忆
        """
        clause, params = self._scope_filter()
        with self._lock, self._connect() as conn:
            resolved = self._resolve_memory_id(conn, memory_id)
            row = None
            if resolved is not None:
                row = conn.execute(
                    f"SELECT * FROM memories WHERE id=?{clause}",
                    (resolved, *params),
                ).fetchone()
        return self._row_to_dict(row) if row is not None else {"error": f"记忆不存在: {memory_id}", "ok": False}

    def _resolve_memory_id(self, conn: sqlite3.Connection, memory_id: str) -> str | None:
        """
        在当前会话中解析记忆 ID, 精确匹配优先, 否则使用唯一前缀匹配

        参数:
        - conn: 数据库连接
        - memory_id: 记忆 ID

        返回:
        - str | None: 解析到的完整 ID, 不唯一或不存在时返回 None
        """
        clause, params = self._scope_filter()
        row = conn.execute(
            f"SELECT id FROM memories WHERE id=?{clause}",
            (memory_id, *params),
        ).fetchone()
        if row is not None:
            return str(row["id"])
        rows = conn.execute(
            f"SELECT id FROM memories WHERE id LIKE ?{clause}",
            (memory_id + "%", *params),
        ).fetchall()
        if len(rows) == 1:
            return str(rows[0]["id"])
        return None

    def list_all(self) -> list[dict[str, Any]]:
        """
        列出当前会话的全部记忆 (importance 降序)

        返回:
        - list[dict[str, Any]]: 当前会话的记忆列表
        """
        clause, params = self._scope_filter()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM memories WHERE 1=1{clause} ORDER BY importance DESC, updated_at DESC",
                params,
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def clear(self) -> int:
        """
        清空当前会话的记忆, 返回删除条数

        返回:
        - int: 删除条数
        """
        if not self.can_write():
            return 0
        clause, params = self._scope_filter()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM memories WHERE 1=1{clause}",
                params,
            )
        return cursor.rowcount

    # ---------- 注入格式化 ----------

    def to_context_block(self) -> str:
        """
        生成可注入 system prompt 的记忆块 (disabled 模式返回空串)

        返回:
        - str: 当前会话的记忆上下文块
        """
        if self.mode == "disabled":
            return ""
        return self._render_block(self.list_all()[: self.max_entries])

    def _render_block(self, memories: list[dict[str, Any]]) -> str:
        """
        单块渲染 (无分层标签)

        参数:
        - memories: 记忆列表

        返回:
        - str: 单块渲染 (无分层标签)
        """
        if not memories:
            return ""
        lines = ["<long-term-memory>"]
        lines.extend(self._render_lines(memories))
        lines.append("</long-term-memory>")
        return "\n".join(lines)

    @staticmethod
    def _render_lines(memories: list[dict[str, Any]]) -> list[str]:
        """
        记忆条目行渲染

        参数:
        - memories: 记忆列表

        返回:
        - list[str]: 记忆条目行渲染
        """
        lines: list[str] = []
        for m in memories:
            tags = f", 标签: {', '.join(m['tags'])}" if m["tags"] else ""
            lines.append(f"- [{m['title']}] {m['content']}{tags}")
        return lines

    def count(self) -> int:
        """
        统计当前会话的记忆条数

        返回:
        - int: 统计可见集合内记忆条数
        """
        clause, params = self._scope_filter()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM memories WHERE 1=1{clause}",
                params,
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """
        sqlite 行转字典 (tags 解析为列表)

        参数:
        - row: 数据行

        返回:
        - dict[str, Any]: sqlite 行转字典 (tags 解析为列表)
        """
        try:
            tags = cast(list[Any], json.loads(row["tags"])) if row["tags"] else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        return {
            "id": row["id"],
            "scope": row["scope"],
            "title": row["title"],
            "content": row["content"],
            "tags": [t for t in tags if isinstance(t, str)],
            "importance": int(row["importance"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "ok": True,
        }
