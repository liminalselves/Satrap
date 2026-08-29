"""
展示层旁路记录: 独立 db 存储用户侧对话显示数据 (供前端聊天页消费)

定位: 面向前端的展示数据层, 与 satrap 后端核心 (core 会话/工作流, edictum 简易会话)
解耦 -- 本体只负责把对话显示数据落库, 经 api 层暴露给前端

设计动机:
- ContextManager 的 chat_history 是"模型视角"上下文: 不存 thinking (写入前清除),
且总结压缩会删除原始轮次 -- 不适合作为前端聊天显示的数据源
- DisplayRecorder 在 SimpleSession 外部旁路: 复用 content_callback / thinking_callback
  与 ToolsManager 的 tool_call_start/tool_call_end 可选钩子, 把每轮对话的
  用户输入 / thinking / 最终输出 / 工具调用状态 写入独立 db, 完全不改动会话内部

数据粒度 (对话级 + 工具明细):
- display_turns: 一轮一条 (user_input / thinking / answer), start_turn 即插入
  (answer 先空, end_turn 回填), 使工具调用可实时关联 turn_id
- display_tool_calls: 一轮内每次工具调用一条, success 三态
  (NULL=进行中 / 1=完成 / 0=失败), 执行前插入 (进行中), 执行后按 call_id 更新
- display_turn_variants: Retry 产生的持久化回复版本, 保存展示内容和本轮模型上下文增量
-- 长 shell 命令执行中前端可实时显示"进行中", 避免用户以为卡死

线程安全: 回调可能经 to_thread 在工作线程执行 (异步会话), 写入用独立连接 + 锁保护
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, cast

from satrap.core.log import logger
from satrap.core.utils.paths import get_db_path

_ARG_VALUE_LIMIT = 20
# 工具参数单值截断长度 (只记录模型填入参数的前 N 字符)


def _truncate_arguments(arguments: Any, limit: int = _ARG_VALUE_LIMIT) -> str:
    """
    把工具参数序列化为 JSON, 每个值截断到前 limit 字符 (只记录模型填入参数, 不记录结果)

    参数:
    - arguments: 调用参数
    - limit: 数量上限

    返回:
    - str: 把工具参数序列化为 JSON, 每个值截断到前 limit 字符 (只记录模型填入参数, 不记录结果)
    """
    if not isinstance(arguments, dict):
        return "{}"
    truncated: dict[str, str] = {}
    for k, v in cast(dict[Any, Any], arguments).items():
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        s = str(s)
        truncated[str(k)] = s[:limit] + "…" if len(s) > limit else s
    try:
        return json.dumps(truncated, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """
    表是否存在 (项目功能: 表可能尚未创建, 如只登记过项目的库)

    参数:
    - conn: 数据库连接
    - name: 名称

    返回:
    - bool: 表是否存在 (项目功能: 表可能尚未创建, 如只登记过项目的库)
    """
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        is not None
    )


def _ensure_project_schema_locked(conn: sqlite3.Connection) -> None:
    """
    确保 projects 表存在

    参数:
    - conn: 数据库连接
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            root_path TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )


def _ensure_column_locked(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    """
    确保表中存在指定列

    参数:
    - conn: 数据库连接
    - table: 表名
    - column: 列名
    - declaration: SQLite 列声明
    """
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


class DisplayRecorder:
    """
    展示层旁路记录器: 把一轮对话的输入/思考/输出/工具调用写入独立 db

    用法 (外层组合, 零侵入会话):
    ```python
    recorder = DisplayRecorder(conversation_id=session_id)  # db 默认 local 平台的 platform.db
    session = SimpleSession(
        session_id, llm,
        content_callback=recorder.on_content,
        thinking_callback=recorder.on_thinking,
        return_thinking=True, stream=True,
    )
    session.tools_manager.tool_call_start = recorder.on_tool_start
    session.tools_manager.tool_call_end = recorder.on_tool_end

    recorder.start_turn(user_input)
    answer = session.run(user_input, thinking="medium")
    recorder.end_turn(answer)
    ```
    """

    def __init__(self, db_path: str | None = None, conversation_id: str = "") -> None:
        """
        初始化记录器

        参数:
        - db_path: 展示层 db 路径, 默认 local 平台的 platform.db
          (与 CM 的 chat_history.db 分离)
        - conversation_id: 会话 ID (与 CM conversation_id 对齐, 便于关联)
        """
        self.db_path = db_path or get_db_path()
        self.conversation_id = conversation_id
        self._lock = threading.RLock()
        """可重入锁: 允许持锁方法内调 _get_conn (其内部也拿锁), 避免死锁"""
        self._conn: sqlite3.Connection | None = None
        self._init_db()
        # turn_index 起始: 查库取 max+1, 之后内存自增 (跨重启连续, 单会话内无需每次查库)
        self._next_turn_index = self._load_max_turn_index() + 1
        # 本轮暂存: start_turn 时初始化, end_turn 时消费
        self._turn_id: int | None = None
        self._thinking_parts: list[str] = []
        self._answer_parts: list[str] = []
        self._tool_seq = 0
        self._variant_index = 0
        # segments: 按时间顺序记录段 (thinking/tool/content)
        self._segments: list[dict[str, Any]] = []

    # ---------- db 基础 ----------

    def _get_conn(self) -> sqlite3.Connection:
        """
        获取复用连接 (check_same_thread=False, 由 _lock 保证串行)

        返回:
        - sqlite3.Connection: 复用连接 (check_same_thread=False, 由 _lock 保证串行)
        """
        with self._lock:
            if self._conn is None:
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
                self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            return self._conn

    def close(self) -> None:
        """关闭连接 (进程退出或不再使用时调用)"""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _init_db(self) -> None:
        """初始化表结构"""
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS display_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    user_input TEXT NOT NULL,
                    thinking TEXT,
                    answer TEXT NOT NULL DEFAULT '',
                    attachments TEXT,
                    segments TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conv_turn ON display_turns (conversation_id, turn_index)"
            )
            _ensure_column_locked(
                conn,
                "display_turns",
                "active_variant",
                "INTEGER NOT NULL DEFAULT 0",
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS display_tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    arguments TEXT,
                    success INTEGER,
                    call_id TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (turn_id) REFERENCES display_turns(id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turn ON display_tool_calls (turn_id)"
            )
            _ensure_column_locked(
                conn,
                "display_tool_calls",
                "variant_index",
                "INTEGER NOT NULL DEFAULT 0",
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS display_turn_variants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id INTEGER NOT NULL,
                    variant_index INTEGER NOT NULL,
                    thinking TEXT,
                    answer TEXT NOT NULL DEFAULT '',
                    segments TEXT,
                    context_messages TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(turn_id, variant_index),
                    FOREIGN KEY (turn_id) REFERENCES display_turns(id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turn_variant"
                " ON display_turn_variants (turn_id, variant_index)"
            )
            # 既有展示轮次按第 0 个回复版本登记
            conn.execute(
                """
                INSERT OR IGNORE INTO display_turn_variants
                    (turn_id, variant_index, thinking, answer, segments, context_messages, created_at)
                SELECT id, 0, thinking, answer, segments, NULL, created_at FROM display_turns
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_meta (
                    conversation_id TEXT PRIMARY KEY,
                    model TEXT NOT NULL DEFAULT 'default',
                    think TEXT NOT NULL DEFAULT 'off',
                    project_id TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            _ensure_project_schema_locked(conn)
            conn.commit()

    def _load_max_turn_index(self) -> int:
        """
        查当前会话最大 turn_index (无记录返回 -1, 使起始为 0)

        返回:
        - int:  -1, 使起始为 0)
        """
        conn = self._get_conn()
        with self._lock:
            row = conn.execute(
                "SELECT MAX(turn_index) FROM display_turns WHERE conversation_id = ?",
                (self.conversation_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else -1

    # ---------- 会话元数据 ----------

    def save_meta(self, model: str, think: str = "off", project_id: str | None = None) -> None:
        """
        保存会话元数据 (model / 默认思考强度 think / 所属项目 project_id)

        参数:
        - model: 模型名称
        - think: 思考模式
        - project_id: 项目 ID

        重复保存时只更新 model/think, project_id 保持既有绑定不变 (改绑走 set_conversation_project)
        """
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO conversation_meta (conversation_id, model, think, project_id, created_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(conversation_id) DO UPDATE SET model=excluded.model, think=excluded.think",
                (self.conversation_id, model, think, project_id, time.time()),
            )
            conn.commit()

    # ---------- 生命周期 ----------

    def start_turn(
        self,
        user_input: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        """
        run 前调用: 插入 turn 行 (answer 先空), 使工具调用可实时关联 turn_id

        参数:
        - user_input: 用户输入
        - attachments: 附件列表

        返回:
        - dict[str, int]: 新轮次的数据库 ID, turn_index 和 variant_index
        """
        with self._lock:
            turn_index = self._next_turn_index
            self._next_turn_index += 1
            conn = self._get_conn()
            att_json = json.dumps(attachments, ensure_ascii=False) if attachments else None
            cur = conn.execute(
                "INSERT INTO display_turns (conversation_id, turn_index, user_input, thinking, answer, attachments, segments, created_at)"
                " VALUES (?, ?, ?, NULL, '', ?, NULL, ?)",
                (self.conversation_id, turn_index, user_input, att_json, time.time()),
            )
            conn.commit()
            self._turn_id = int(cur.lastrowid or 0)
            self._thinking_parts = []
            self._answer_parts = []
            self._tool_seq = 0
            self._variant_index = 0
            self._segments = []
            return {
                "turn_id": self._turn_id,
                "turn_index": turn_index,
                "variant_index": self._variant_index,
            }

    def end_turn(
        self,
        fallback_answer: str = "",
        *,
        context_messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, int] | None:
        """
        run 后调用: 回填 thinking / answer / segments (answer 优先回调流拼接, 空则用 run 返回值兜底)

        参数:
        - fallback_answer: 回退回答
        - context_messages: 本回复对应的模型上下文增量

        返回:
        - dict[str, int] | None: 已完成轮次和回复版本标识
        """
        if self._turn_id is None:
            return None
        thinking = "".join(self._thinking_parts) or None
        answer = "".join(self._answer_parts) or fallback_answer
        segments_json = json.dumps(self._segments, ensure_ascii=False) if self._segments else None
        context_json = (
            json.dumps(context_messages, ensure_ascii=False) if context_messages is not None else None
        )
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "UPDATE display_turns SET thinking = ?, answer = ?, segments = ?, active_variant = ?"
                " WHERE id = ?",
                (thinking, answer, segments_json, self._variant_index, self._turn_id),
            )
            conn.execute(
                """
                INSERT INTO display_turn_variants
                    (turn_id, variant_index, thinking, answer, segments, context_messages, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id, variant_index) DO UPDATE SET
                    thinking=excluded.thinking,
                    answer=excluded.answer,
                    segments=excluded.segments,
                    context_messages=excluded.context_messages,
                    created_at=excluded.created_at
                """,
                (
                    self._turn_id,
                    self._variant_index,
                    thinking,
                    answer,
                    segments_json,
                    context_json,
                    time.time(),
                ),
            )
            conn.commit()
            turn_id = self._turn_id
            turn_row = conn.execute(
                "SELECT turn_index FROM display_turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
            count_row = conn.execute(
                "SELECT COUNT(*) FROM display_turn_variants WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            result = {
                "turn_id": turn_id,
                "turn_index": int(turn_row[0]) if turn_row else -1,
                "variant_index": self._variant_index,
                "variant_count": int(count_row[0]) if count_row else 0,
            }
            self._turn_id = None
            self._segments = []
            return result

    def start_retry_variant(self) -> dict[str, Any] | None:
        """
        为最后一轮开始新的回复版本, 保留现有回复

        返回:
        - dict[str, Any] | None: 待重试轮次信息, 无轮次时返回 None
        """
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT id, turn_index, user_input, attachments, active_variant FROM display_turns"
                " WHERE conversation_id = ? ORDER BY turn_index DESC LIMIT 1",
                (self.conversation_id,),
            ).fetchone()
            if row is None:
                return None
            turn_id = int(row[0])
            current_variant = int(row[4] or 0)
            context_row = conn.execute(
                "SELECT context_messages FROM display_turn_variants"
                " WHERE turn_id = ? AND variant_index = ?",
                (turn_id, current_variant),
            ).fetchone()
            max_row = conn.execute(
                "SELECT MAX(variant_index) FROM display_turn_variants WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            next_variant = int(max_row[0]) + 1 if max_row and max_row[0] is not None else 1
            self._turn_id = turn_id
            self._variant_index = next_variant
            self._thinking_parts = []
            self._answer_parts = []
            self._tool_seq = 0
            self._segments = []
            return {
                "turn_id": turn_id,
                "turn_index": int(row[1]),
                "user_input": str(row[2]),
                "attachments": json.loads(row[3]) if row[3] else None,
                "variant_index": next_variant,
                "previous_variant": current_variant,
                "previous_context_messages": (
                    json.loads(context_row[0]) if context_row and context_row[0] else None
                ),
            }

    def abort_active_variant(self) -> None:
        """取消尚未完成的回复版本并清理其工具调用"""
        with self._lock:
            if self._turn_id is not None:
                conn = self._get_conn()
                conn.execute(
                    "DELETE FROM display_tool_calls WHERE turn_id = ? AND variant_index = ?",
                    (self._turn_id, self._variant_index),
                )
                conn.commit()
            self._turn_id = None
            self._thinking_parts = []
            self._answer_parts = []
            self._tool_seq = 0
            self._segments = []

    # ---------- 注入 SimpleSession 的回调 ----------

    def on_thinking(self, delta: str) -> None:
        """
        thinking_callback: 追加思考增量到缓冲, 并记录 segments 时间顺序

        参数:
        - delta: 增量内容
        """
        if delta:
            self._thinking_parts.append(delta)
            # 追加到当前 thinking 段或新建段
            if self._segments and self._segments[-1].get("type") == "thinking":
                self._segments[-1]["content"] += delta
            else:
                self._segments.append({"type": "thinking", "content": delta})

    def on_content(self, delta: str) -> None:
        """
        content_callback: 追加正文增量到缓冲, 并记录 segments 时间顺序

        参数:
        - delta: 增量内容
        """
        if delta:
            self._answer_parts.append(delta)
            # 追加到当前 content 段或新建段
            if self._segments and self._segments[-1].get("type") == "content":
                self._segments[-1]["content"] += delta
            else:
                self._segments.append({"type": "content", "content": delta})

    # ---------- 注入 ToolsManager 的观察钩子 ----------

    def on_tool_start(self, event: dict[str, Any]) -> None:
        """
        tool_call_start: 插入工具调用行 (success=NULL 进行中), 前端实时显示"正在执行";
        同时记录 segments 时间顺序

        参数:
        - event: 事件
        """
        if self._turn_id is None:
            return
        tool_data = {
            "seq": self._tool_seq,
            "name": str(event.get("name", "")),
            "arguments": _truncate_arguments(event.get("arguments")),
            "success": None,
            "call_id": str(event.get("call_id", "")),
            "created_at": time.time(),
        }
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO display_tool_calls"
                " (turn_id, variant_index, seq, name, arguments, success, call_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    self._turn_id,
                    self._variant_index,
                    tool_data["seq"],
                    tool_data["name"],
                    tool_data["arguments"],
                    tool_data["call_id"],
                    tool_data["created_at"],
                ),
            )
            conn.commit()
            self._tool_seq += 1
        # 记录 tool segment
        self._segments.append({"type": "tool", "tool": tool_data})

    def on_tool_end(self, event: dict[str, Any]) -> None:
        """
        tool_call_end: 按 call_id 更新 success (1=完成 / 0=失败), 并同步 segments 中的 tool 状态

        参数:
        - event: 事件
        """
        if self._turn_id is None:
            return
        success = 1 if event.get("success") else 0
        call_id = str(event.get("call_id", ""))
        with self._lock:
            conn = self._get_conn()
            if call_id:
                conn.execute(
                    "UPDATE display_tool_calls SET success = ?"
                    " WHERE turn_id = ? AND variant_index = ? AND call_id = ?",
                    (success, self._turn_id, self._variant_index, call_id),
                )
            else:
                conn.execute(
                    "UPDATE display_tool_calls SET success = ?"
                    " WHERE turn_id = ? AND variant_index = ? AND success IS NULL"
                    " AND id = (SELECT MAX(id) FROM display_tool_calls"
                    " WHERE turn_id = ? AND variant_index = ? AND success IS NULL)",
                    (
                        success,
                        self._turn_id,
                        self._variant_index,
                        self._turn_id,
                        self._variant_index,
                    ),
                )
                # 无 call_id 兜底: 更新本轮最近一条进行中的记录
            conn.commit()
        # 同步更新 segments 中的 tool 状态
        for seg in self._segments:
            if seg.get("type") == "tool" and seg.get("tool", {}).get("call_id") == call_id:
                seg["tool"]["success"] = bool(success)
                break

    # ---------- 查询 (供前端) ----------

    def list_turns(self, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        """
        列出当前会话的对话轮次 (按 turn_index 升序), 每轮含工具调用明细和 segments 时间顺序

        参数:
        - limit: 数量上限
        - offset: 偏移量

        返回:
        - list[dict[str, Any]]: 列出当前会话的对话轮次 (按 turn_index 升序), 每轮含工具调用明细和 segments 时间顺序
        """
        conn = self._get_conn()
        with self._lock:
            sql = (
                "SELECT id, turn_index, user_input, thinking, answer, attachments, segments,"
                " created_at, active_variant,"
                " (SELECT COUNT(*) FROM display_turn_variants v WHERE v.turn_id = display_turns.id)"
                " FROM display_turns WHERE conversation_id = ? ORDER BY turn_index ASC"
            )
            params: list[Any] = [self.conversation_id]
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                params += [limit, offset]
            rows = conn.execute(sql, params).fetchall()
            turns: list[dict[str, Any]] = [
                {
                    "id": r[0],
                    "turn_index": r[1],
                    "user_input": r[2],
                    "thinking": r[3],
                    "answer": r[4],
                    "attachments": json.loads(r[5]) if r[5] else None,
                    "segments": json.loads(r[6]) if r[6] else None,
                    "created_at": r[7],
                    "active_variant": int(r[8] or 0),
                    "variant_count": int(r[9] or 0),
                    "tool_calls": self._list_tool_calls_locked(conn, r[0], int(r[8] or 0)),
                    "variants": self._list_variants_locked(conn, r[0]),
                }
                for r in rows
            ]
        return turns

    @staticmethod
    def _list_tool_calls_locked(
        conn: sqlite3.Connection,
        turn_id: int,
        variant_index: int = 0,
    ) -> list[dict[str, Any]]:
        """
        列出某轮的工具调用 (按 seq 升序); 调用方需已持有 _lock

        参数:
        - conn: 数据库连接
        - turn_id: 轮次ID
        - variant_index: 回复版本索引

        返回:
        - list[dict[str, Any]]: 列出某轮的工具调用 (按 seq 升序); 调用方需已持有 _lock
        """
        rows = conn.execute(
            "SELECT seq, name, arguments, success, call_id, created_at"
            " FROM display_tool_calls WHERE turn_id = ? AND variant_index = ? ORDER BY seq ASC",
            (turn_id, variant_index),
        ).fetchall()
        return [
            {
                "seq": r[0],
                "name": r[1],
                "arguments": r[2],
                # success: None=进行中 / True=完成 / False=失败
                "success": None if r[3] is None else bool(r[3]),
                "call_id": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]

    @classmethod
    def _list_variants_locked(
        cls,
        conn: sqlite3.Connection,
        turn_id: int,
    ) -> list[dict[str, Any]]:
        """
        列出轮次的全部回复版本

        参数:
        - conn: 数据库连接
        - turn_id: 轮次数据库 ID

        返回:
        - list[dict[str, Any]]: 按版本索引排序的回复版本
        """
        rows = conn.execute(
            "SELECT variant_index, thinking, answer, segments, created_at"
            " FROM display_turn_variants WHERE turn_id = ? ORDER BY variant_index ASC",
            (turn_id,),
        ).fetchall()
        return [
            {
                "variant_index": int(row[0]),
                "thinking": row[1],
                "answer": row[2],
                "segments": json.loads(row[3]) if row[3] else None,
                "created_at": row[4],
                "tool_calls": cls._list_tool_calls_locked(conn, turn_id, int(row[0])),
            }
            for row in rows
        ]

    # ---------- 删除 / 复制 (供 retry / fork) ----------

    def variant_context(
        self,
        turn_index: int,
        variant_index: int,
    ) -> list[dict[str, Any]] | None:
        """
        读取回复版本对应的模型上下文增量

        参数:
        - turn_index: 轮次索引
        - variant_index: 回复版本索引

        返回:
        - list[dict[str, Any]] | None: 上下文增量, 旧数据未知时返回 None
        """
        with self._lock:
            row = self._get_conn().execute(
                "SELECT v.context_messages FROM display_turn_variants v"
                " JOIN display_turns t ON t.id = v.turn_id"
                " WHERE t.conversation_id = ? AND t.turn_index = ? AND v.variant_index = ?",
                (self.conversation_id, turn_index, variant_index),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return cast(list[dict[str, Any]], json.loads(row[0]))

    def save_variant_context(
        self,
        turn_index: int,
        variant_index: int,
        messages: list[dict[str, Any]],
    ) -> bool:
        """
        补写回复版本的模型上下文增量

        参数:
        - turn_index: 轮次索引
        - variant_index: 回复版本索引
        - messages: 模型上下文增量

        返回:
        - bool: 是否找到并更新目标版本
        """
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "UPDATE display_turn_variants SET context_messages = ?"
                " WHERE turn_id = (SELECT id FROM display_turns"
                " WHERE conversation_id = ? AND turn_index = ?) AND variant_index = ?",
                (
                    json.dumps(messages, ensure_ascii=False),
                    self.conversation_id,
                    turn_index,
                    variant_index,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def activate_variant(self, turn_index: int, variant_index: int) -> dict[str, Any] | None:
        """
        激活指定回复版本并同步展示层当前值

        参数:
        - turn_index: 轮次索引
        - variant_index: 回复版本索引

        返回:
        - dict[str, Any] | None: 激活后的完整轮次
        """
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT t.id, v.thinking, v.answer, v.segments"
                " FROM display_turns t JOIN display_turn_variants v ON v.turn_id = t.id"
                " WHERE t.conversation_id = ? AND t.turn_index = ? AND v.variant_index = ?",
                (self.conversation_id, turn_index, variant_index),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE display_turns SET thinking = ?, answer = ?, segments = ?, active_variant = ?"
                " WHERE id = ?",
                (row[1], row[2], row[3], variant_index, row[0]),
            )
            conn.commit()
        return self.get_turn(turn_index)

    def get_turn(self, turn_index: int) -> dict[str, Any] | None:
        """
        按索引读取单个展示轮次

        参数:
        - turn_index: 轮次索引

        返回:
        - dict[str, Any] | None: 匹配轮次
        """
        return next(
            (turn for turn in self.list_turns() if int(turn["turn_index"]) == turn_index),
            None,
        )

    def last_turn(self) -> dict[str, Any] | None:
        """
        读取最后一个展示轮次

        返回:
        - dict[str, Any] | None: 最后轮次
        """
        turns = self.list_turns()
        return turns[-1] if turns else None

    def active_context_before(self, up_to_index: int) -> list[dict[str, Any]]:
        """
        拼接指定轮次之前所有已选回复的模型上下文增量

        参数:
        - up_to_index: 不包含的结束轮次索引

        返回:
        - list[dict[str, Any]]: 可写入新会话的模型上下文消息
        """
        result: list[dict[str, Any]] = []
        with self._lock:
            rows = self._get_conn().execute(
                "SELECT t.turn_index, v.context_messages FROM display_turns t"
                " JOIN display_turn_variants v"
                " ON v.turn_id = t.id AND v.variant_index = t.active_variant"
                " WHERE t.conversation_id = ? AND t.turn_index < ? ORDER BY t.turn_index ASC",
                (self.conversation_id, up_to_index),
            ).fetchall()
        for _, raw_messages in rows:
            if raw_messages:
                result.extend(cast(list[dict[str, Any]], json.loads(raw_messages)))
        return result

    def delete_last_turn(self) -> dict[str, Any] | None:
        """
        删除最大 turn_index 的记录及其工具调用, 返回被删记录 (无记录返回 None)

        返回:
        - dict[str, Any] | None: 被删记录 (无记录返回 None)
        """
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT id, turn_index, user_input FROM display_turns"
                " WHERE conversation_id = ? ORDER BY turn_index DESC LIMIT 1",
                (self.conversation_id,),
            ).fetchone()
            if row is None:
                return None
            turn_id, turn_index, user_input = row[0], row[1], row[2]
            conn.execute("DELETE FROM display_tool_calls WHERE turn_id = ?", (turn_id,))
            conn.execute("DELETE FROM display_turn_variants WHERE turn_id = ?", (turn_id,))
            conn.execute("DELETE FROM display_turns WHERE id = ?", (turn_id,))
            conn.commit()
            # 回退计数器
            self._next_turn_index = turn_index
            return {"id": turn_id, "turn_index": turn_index, "user_input": user_input}

    def delete_conversation(self) -> None:
        """删除当前会话的全部数据 (turns + tool_calls + meta)"""
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "DELETE FROM display_tool_calls WHERE turn_id IN (SELECT id FROM display_turns WHERE conversation_id = ?)",
                (self.conversation_id,),
            )
            conn.execute(
                "DELETE FROM display_turn_variants WHERE turn_id IN"
                " (SELECT id FROM display_turns WHERE conversation_id = ?)",
                (self.conversation_id,),
            )
            conn.execute("DELETE FROM display_turns WHERE conversation_id = ?", (self.conversation_id,))
            conn.execute("DELETE FROM conversation_meta WHERE conversation_id = ?", (self.conversation_id,))
            conn.commit()

    def copy_turns_to(self, target: DisplayRecorder, up_to_index: int) -> int:
        """
        把 turn_index < up_to_index 的轮次复制到 target recorder, 返回复制条数

        参数:
        - target: 目标
        - up_to_index: upto索引

        返回:
        - int: 复制条数
        """
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT t.id, t.turn_index, t.user_input, t.thinking, t.answer, t.attachments,"
                " t.segments, t.created_at, t.active_variant, v.context_messages"
                " FROM display_turns t LEFT JOIN display_turn_variants v"
                " ON v.turn_id = t.id AND v.variant_index = t.active_variant"
                " WHERE t.conversation_id = ? AND t.turn_index < ? ORDER BY t.turn_index ASC",
                (self.conversation_id, up_to_index),
            ).fetchall()
        count = 0
        for r in rows:
            with target._lock:
                tconn = target._get_conn()
                cur = tconn.execute(
                    "INSERT INTO display_turns"
                    " (conversation_id, turn_index, user_input, thinking, answer, attachments,"
                    " segments, created_at, active_variant) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (target.conversation_id, r[1], r[2], r[3], r[4], r[5], r[6], r[7]),
                )
                new_turn_id = int(cur.lastrowid or 0)
                tconn.execute(
                    "INSERT INTO display_turn_variants"
                    " (turn_id, variant_index, thinking, answer, segments, context_messages, created_at)"
                    " VALUES (?, 0, ?, ?, ?, ?, ?)",
                    (new_turn_id, r[3], r[4], r[6], r[9], r[7]),
                )
                # 复制工具调用
                tools = conn.execute(
                    "SELECT seq, name, arguments, success, call_id, created_at"
                    " FROM display_tool_calls WHERE turn_id = ? AND variant_index = ?"
                    " ORDER BY seq ASC",
                    (r[0], r[8]),
                ).fetchall()
                for tool in tools:
                    tconn.execute(
                        "INSERT INTO display_tool_calls"
                        " (turn_id, variant_index, seq, name, arguments, success, call_id, created_at)"
                        " VALUES (?, 0, ?, ?, ?, ?, ?, ?)",
                        (new_turn_id, tool[0], tool[1], tool[2], tool[3], tool[4], tool[5]),
                    )
                tconn.commit()
            count += 1
        # 同步 target 的 turn_index 计数器
        if count > 0:
            target._next_turn_index = rows[-1][1] + 1
        return count



def query_conversations(
    db_path: str | None = None,
    *,
    search: str = "",
    project_id: str | None = None,
    model: str = "",
    turn_count: str = "all",
    older_than_days: float | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """
    查询 Chat 历史会话并返回分页结果

    只读取已有数据库, 不因冷查询创建文件或数据表

    参数:
    - db_path: 展示层 db 路径, 默认 local 平台的 platform.db
    - search: 标题或会话 ID 搜索文本
    - project_id: 项目 ID, `__none__` 表示无项目
    - model: 模型配置名称
    - turn_count: 轮数过滤, 支持 all、empty、single
    - older_than_days: 仅返回超过指定未使用天数的会话
    - page: 页码, 从 1 开始
    - page_size: 每页数量

    返回:
    - dict[str, Any]: 会话列表、总数和分页信息
    """
    path = Path(db_path or get_db_path())
    normalized_page = max(int(page), 1)
    normalized_page_size = max(1, min(int(page_size), 200))
    if not path.exists():
        return {
            "items": [],
            "total": 0,
            "page": normalized_page,
            "page_size": normalized_page_size,
        }
    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        tables = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        has_turns = "display_turns" in tables
        has_meta = "conversation_meta" in tables
        if not has_turns and not has_meta:
            return {
                "items": [],
                "total": 0,
                "page": normalized_page,
                "page_size": normalized_page_size,
            }
        if has_turns and has_meta:
            rows = conn.execute(
                """
                WITH turn_stats AS (
                    SELECT t.conversation_id,
                           COUNT(*) AS turn_count,
                           MAX(t.created_at) AS last_at,
                           (SELECT user_input FROM display_turns first_turn
                            WHERE first_turn.conversation_id = t.conversation_id
                            ORDER BY first_turn.turn_index ASC LIMIT 1) AS first_input
                    FROM display_turns t
                    GROUP BY t.conversation_id
                )
                SELECT m.conversation_id, COALESCE(s.turn_count, 0),
                       COALESCE(s.last_at, m.created_at), COALESCE(s.first_input, '新对话'),
                       m.project_id, m.model, m.think, m.created_at
                FROM conversation_meta m
                LEFT JOIN turn_stats s ON s.conversation_id = m.conversation_id
                UNION ALL
                SELECT s.conversation_id, s.turn_count, s.last_at, s.first_input,
                       NULL, 'default', 'off', s.last_at
                FROM turn_stats s
                WHERE s.conversation_id NOT IN (SELECT conversation_id FROM conversation_meta)
                """
            ).fetchall()
        elif has_meta:
            rows = conn.execute(
                "SELECT conversation_id, 0, created_at, '新对话', project_id, model, think, created_at "
                "FROM conversation_meta"
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT conversation_id, COUNT(*) AS turn_count, MAX(created_at) AS last_at,
                       (SELECT user_input FROM display_turns t2
                         WHERE t2.conversation_id = t.conversation_id
                         ORDER BY turn_index ASC LIMIT 1) AS first_input,
                       NULL AS project_id, 'default', 'off', MIN(created_at)
                FROM display_turns
                GROUP BY conversation_id
                """
            ).fetchall()
        items = [
            {
                "conversation_id": str(r[0]),
                "turn_count": int(r[1] or 0),
                "last_at": float(r[2] or r[7] or 0),
                "title": str(r[3] or "新对话"),
                "project_id": r[4],
                "model": str(r[5] or "default"),
                "think": str(r[6] or "off"),
                "created_at": float(r[7] or 0),
            }
            for r in rows
        ]

        keyword = search.strip().casefold()
        if keyword:
            items = [
                item
                for item in items
                if keyword in str(item["title"]).casefold()
                or keyword in str(item["conversation_id"]).casefold()
            ]
        if project_id == "__none__":
            items = [item for item in items if not item.get("project_id")]
        elif project_id:
            items = [item for item in items if item.get("project_id") == project_id]
        if model.strip():
            items = [item for item in items if item.get("model") == model.strip()]
        if turn_count == "empty":
            items = [item for item in items if item["turn_count"] == 0]
        elif turn_count == "single":
            items = [item for item in items if item["turn_count"] == 1]
        if older_than_days is not None:
            cutoff = time.time() - max(float(older_than_days), 0) * 86400
            items = [item for item in items if float(item["last_at"]) <= cutoff]
        items.sort(key=lambda item: float(item["last_at"]), reverse=True)
        total = len(items)
        offset = (normalized_page - 1) * normalized_page_size
        return {
            "items": items[offset:offset + normalized_page_size],
            "total": total,
            "page": normalized_page,
            "page_size": normalized_page_size,
        }
    finally:
        conn.close()


def list_conversations(db_path: str | None = None) -> list[dict[str, Any]]:
    """
    列出全部 Chat 历史会话

    参数:
    - db_path: 展示层数据库路径

    返回:
    - list[dict[str, Any]]: 按最近使用时间倒序的全部会话
    """
    page = 1
    items: list[dict[str, Any]] = []
    while True:
        result = query_conversations(db_path, page=page, page_size=200)
        items.extend(cast(list[dict[str, Any]], result["items"]))
        if len(items) >= int(result["total"]):
            return items
        page += 1


# ---------- 项目 (工作区文件夹绑定) ----------


def create_project(name: str, root_path: str, db_path: str | None = None) -> dict[str, Any]:
    """
    创建项目 (绑定工作区文件夹), 返回项目记录

    参数:
    - name: 名称
    - root_path: 根目录路径
    - db_path: 数据库路径

    返回:
    - dict[str, Any]: 项目记录
    """
    path = db_path or get_db_path()
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        _ensure_project_schema_locked(conn)
        project_id = uuid.uuid4().hex
        record = {
            "project_id": project_id,
            "name": name.strip(),
            "root_path": root_path,
            "created_at": time.time(),
        }
        conn.execute(
            "INSERT INTO projects (project_id, name, root_path, created_at) VALUES (?, ?, ?, ?)",
            (project_id, record["name"], root_path, record["created_at"]),
        )
        conn.commit()
        return record
    finally:
        conn.close()


def list_projects(db_path: str | None = None) -> list[dict[str, Any]]:
    """
    列出全部项目 (按创建时间正序)

    参数:
    - db_path: 数据库路径

    返回:
    - list[dict[str, Any]]: 列出全部项目 (按创建时间正序)
    """
    path = db_path or get_db_path()
    if not Path(path).exists():
        return []
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        _ensure_project_schema_locked(conn)
        conn.commit()
        rows = conn.execute(
            "SELECT project_id, name, root_path, created_at FROM projects ORDER BY created_at ASC"
        ).fetchall()
        return [
            {"project_id": r[0], "name": r[1], "root_path": r[2], "created_at": r[3]}
            for r in rows
        ]
    finally:
        conn.close()


def get_project(project_id: str, db_path: str | None = None) -> dict[str, Any] | None:
    """
    按 ID 查项目, 不存在返回 None

    参数:
    - project_id: 项目 ID
    - db_path: 数据库路径

    返回:
    - dict[str, Any] | None:  None
    """
    path = db_path or get_db_path()
    if not Path(path).exists():
        return None
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        _ensure_project_schema_locked(conn)
        conn.commit()
        row = conn.execute(
            "SELECT project_id, name, root_path, created_at FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return {"project_id": row[0], "name": row[1], "root_path": row[2], "created_at": row[3]}
    finally:
        conn.close()


def delete_project(project_id: str, db_path: str | None = None) -> bool:
    """
    删除项目: 仅解绑其下会话 (project_id 置 NULL), 不动会话数据与磁盘文件

    参数:
    - project_id: 项目 ID
    - db_path: 数据库路径

    返回:
    - bool: 删除项目: 仅解绑其下会话 (project_id 置 NULL), 不动会话数据与磁盘文件
    """
    path = db_path or get_db_path()
    if not Path(path).exists():
        return False
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        _ensure_project_schema_locked(conn)
        if _has_table(conn, "conversation_meta"):
            conn.execute(
                "UPDATE conversation_meta SET project_id = NULL WHERE project_id = ?",
                (project_id,),
            )
        cursor = conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def set_conversation_project(conversation_id: str, project_id: str | None, db_path: str | None = None) -> bool:
    """
    改绑会话所属项目 (None 表示移出项目), 会话不存在返回 False

    参数:
    - conversation_id: 会话 ID
    - project_id: 项目 ID
    - db_path: 数据库路径

    返回:
    - bool:  False
    """
    path = db_path or get_db_path()
    if not Path(path).exists():
        return False
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        _ensure_project_schema_locked(conn)
        if not _has_table(conn, "conversation_meta"):
            return False
        cursor = conn.execute(
            "UPDATE conversation_meta SET project_id = ? WHERE conversation_id = ?",
            (project_id, conversation_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_conversation_meta(conversation_id: str, db_path: str | None = None) -> dict[str, Any] | None:
    """
    查询会话元数据 (model / think / project_id)

    参数:
    - conversation_id: 会话 ID
    - db_path: 数据库路径

    返回 None 表示会话不存在

    返回:
    - dict[str, Any] | None: 查询会话元数据 (model / think / project_id)
    """
    path = db_path or get_db_path()
    if not Path(path).exists():
        return None
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        _ensure_project_schema_locked(conn)
        conn.commit()
        if not _has_table(conn, "conversation_meta"):
            return None
        row = conn.execute(
            "SELECT model, think, created_at, project_id FROM conversation_meta WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return {"model": row[0], "think": row[1] or "off", "created_at": row[2], "project_id": row[3]}
    finally:
        conn.close()
