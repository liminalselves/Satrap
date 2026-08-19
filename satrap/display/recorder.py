"""展示层旁路记录: 独立 db 存储用户侧对话显示数据 (供前端聊天页消费)

定位: 面向前端的展示数据层, 与 satrap 后端核心 (core 会话/工作流, edictum 简易会话)
解耦 —— 本体只负责把对话显示数据落库, 经 api 层暴露给前端

设计动机:
- ContextManager 的 chat_history 是"模型视角"上下文: 不存 thinking (写入前清除),
  且总结压缩会删除原始轮次 —— 不适合作为前端聊天显示的数据源
- DisplayRecorder 在 SimpleSession 外部旁路: 复用 content_callback / thinking_callback
  与 ToolsManager 的 tool_call_start/tool_call_end 可选钩子, 把每轮对话的
  用户输入 / thinking / 最终输出 / 工具调用状态 写入独立 db, 完全不改动会话内部

数据粒度 (对话级 + 工具明细):
- display_turns: 一轮一条 (user_input / thinking / answer), start_turn 即插入
  (answer 先空, end_turn 回填), 使工具调用可实时关联 turn_id
- display_tool_calls: 一轮内每次工具调用一条, success 三态
  (NULL=进行中 / 1=完成 / 0=失败), 执行前插入 (进行中), 执行后按 call_id 更新
  —— 长 shell 命令执行中前端可实时显示"进行中", 避免用户以为卡死

线程安全: 回调可能经 to_thread 在工作线程执行 (异步会话), 写入用独立连接 + 锁保护
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, cast

from satrap.core.log import logger
from satrap.core.utils.paths import get_db_path

# 工具参数单值截断长度 (只记录模型填入参数的前 N 字符)
_ARG_VALUE_LIMIT = 20


def _truncate_arguments(arguments: Any, limit: int = _ARG_VALUE_LIMIT) -> str:
    """把工具参数序列化为 JSON, 每个值截断到前 limit 字符 (只记录模型填入参数, 不记录结果)"""
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


class DisplayRecorder:
    """展示层旁路记录器: 把一轮对话的输入/思考/输出/工具调用写入独立 db

    用法 (外层组合, 零侵入会话):
    ```python
    recorder = DisplayRecorder(conversation_id=session_id)  # db 默认 .satrap/satrapdata/display.db
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
        """初始化记录器

        参数:
        - db_path: 展示层独立 db 路径, 默认 .satrap/satrapdata/display.db
          (与 CM 的 chat_history.db 分离)
        - conversation_id: 会话 ID (与 CM conversation_id 对齐, 便于关联)
        """
        self.db_path = db_path or get_db_path("display.db")
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
        # segments: 按时间顺序记录段 (thinking/tool/content)
        self._segments: list[dict[str, Any]] = []

    # ---------------- db 基础 ----------------

    def _get_conn(self) -> sqlite3.Connection:
        """获取复用连接 (check_same_thread=False, 由 _lock 保证串行)"""
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
            # 兼容旧表: 无 attachments 列时 ALTER TABLE 添加
            try:
                conn.execute("SELECT attachments FROM display_turns LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE display_turns ADD COLUMN attachments TEXT")
            # 兼容旧表: 无 segments 列时 ALTER TABLE 添加
            try:
                conn.execute("SELECT segments FROM display_turns LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE display_turns ADD COLUMN segments TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conv_turn ON display_turns (conversation_id, turn_index)"
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_meta (
                    conversation_id TEXT PRIMARY KEY,
                    model TEXT NOT NULL DEFAULT 'default',
                    think TEXT NOT NULL DEFAULT 'off',
                    created_at REAL NOT NULL
                )
                """
            )
            # 兼容旧表: 无 think 列时 ALTER TABLE 添加
            try:
                conn.execute("SELECT think FROM conversation_meta LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE conversation_meta ADD COLUMN think TEXT NOT NULL DEFAULT 'off'")
            conn.commit()

    def _load_max_turn_index(self) -> int:
        """查当前会话最大 turn_index (无记录返回 -1, 使起始为 0)"""
        conn = self._get_conn()
        with self._lock:
            row = conn.execute(
                "SELECT MAX(turn_index) FROM display_turns WHERE conversation_id = ?",
                (self.conversation_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else -1

    # ---------------- 会话元数据 ----------------

    def save_meta(self, model: str, think: str = "off") -> None:
        """保存会话元数据 (model / 默认思考强度 think)"""
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO conversation_meta (conversation_id, model, think, created_at) VALUES (?, ?, ?, ?)",
                (self.conversation_id, model, think, time.time()),
            )
            conn.commit()

    # ---------------- 生命周期 ----------------

    def start_turn(self, user_input: str, attachments: list[dict[str, Any]] | None = None) -> None:
        """run 前调用: 插入 turn 行 (answer 先空), 使工具调用可实时关联 turn_id"""
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
            self._segments = []

    def end_turn(self, fallback_answer: str = "") -> None:
        """run 后调用: 回填 thinking / answer / segments (answer 优先回调流拼接, 空则用 run 返回值兜底)"""
        if self._turn_id is None:
            return
        thinking = "".join(self._thinking_parts) or None
        answer = "".join(self._answer_parts) or fallback_answer
        segments_json = json.dumps(self._segments, ensure_ascii=False) if self._segments else None
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "UPDATE display_turns SET thinking = ?, answer = ?, segments = ? WHERE id = ?",
                (thinking, answer, segments_json, self._turn_id),
            )
            conn.commit()
            self._turn_id = None
            self._segments = []

    # ---------------- 注入 SimpleSession 的回调 ----------------

    def on_thinking(self, delta: str) -> None:
        """thinking_callback: 追加思考增量到缓冲, 并记录 segments 时间顺序"""
        if delta:
            self._thinking_parts.append(delta)
            # 追加到当前 thinking 段或新建段
            if self._segments and self._segments[-1].get("type") == "thinking":
                self._segments[-1]["content"] += delta
            else:
                self._segments.append({"type": "thinking", "content": delta})

    def on_content(self, delta: str) -> None:
        """content_callback: 追加正文增量到缓冲, 并记录 segments 时间顺序"""
        if delta:
            self._answer_parts.append(delta)
            # 追加到当前 content 段或新建段
            if self._segments and self._segments[-1].get("type") == "content":
                self._segments[-1]["content"] += delta
            else:
                self._segments.append({"type": "content", "content": delta})

    # ---------------- 注入 ToolsManager 的观察钩子 ----------------

    def on_tool_start(self, event: dict[str, Any]) -> None:
        """tool_call_start: 插入工具调用行 (success=NULL 进行中), 前端实时显示"正在执行";
        同时记录 segments 时间顺序"""
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
                "INSERT INTO display_tool_calls (turn_id, seq, name, arguments, success, call_id, created_at)"
                " VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (
                    self._turn_id,
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
        """tool_call_end: 按 call_id 更新 success (1=完成 / 0=失败), 并同步 segments 中的 tool 状态"""
        if self._turn_id is None:
            return
        success = 1 if event.get("success") else 0
        call_id = str(event.get("call_id", ""))
        with self._lock:
            conn = self._get_conn()
            if call_id:
                conn.execute(
                    "UPDATE display_tool_calls SET success = ? WHERE turn_id = ? AND call_id = ?",
                    (success, self._turn_id, call_id),
                )
            else:
                # 无 call_id 兜底: 更新本轮最近一条进行中的记录
                conn.execute(
                    "UPDATE display_tool_calls SET success = ? WHERE turn_id = ? AND success IS NULL"
                    " AND id = (SELECT MAX(id) FROM display_tool_calls WHERE turn_id = ? AND success IS NULL)",
                    (success, self._turn_id, self._turn_id),
                )
            conn.commit()
        # 同步更新 segments 中的 tool 状态
        for seg in self._segments:
            if seg.get("type") == "tool" and seg.get("tool", {}).get("call_id") == call_id:
                seg["tool"]["success"] = bool(success)
                break

    # ---------------- 查询 (供前端) ----------------

    def list_turns(self, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        """列出当前会话的对话轮次 (按 turn_index 升序), 每轮含工具调用明细和 segments 时间顺序"""
        conn = self._get_conn()
        with self._lock:
            sql = (
                "SELECT id, turn_index, user_input, thinking, answer, attachments, segments, created_at"
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
                    "tool_calls": self._list_tool_calls_locked(conn, r[0]),
                }
                for r in rows
            ]
        return turns

    @staticmethod
    def _list_tool_calls_locked(conn: sqlite3.Connection, turn_id: int) -> list[dict[str, Any]]:
        """列出某轮的工具调用 (按 seq 升序); 调用方需已持有 _lock"""
        rows = conn.execute(
            "SELECT seq, name, arguments, success, call_id, created_at"
            " FROM display_tool_calls WHERE turn_id = ? ORDER BY seq ASC",
            (turn_id,),
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

    # ---------------- 删除 / 复制 (供 retry / fork) ----------------

    def delete_last_turn(self) -> dict[str, Any] | None:
        """删除最大 turn_index 的记录及其工具调用, 返回被删记录 (无记录返回 None)"""
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
            conn.execute("DELETE FROM display_turns WHERE conversation_id = ?", (self.conversation_id,))
            conn.execute("DELETE FROM conversation_meta WHERE conversation_id = ?", (self.conversation_id,))
            conn.commit()

    def copy_turns_to(self, target: DisplayRecorder, up_to_index: int) -> int:
        """把 turn_index < up_to_index 的轮次复制到 target recorder, 返回复制条数"""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT turn_index, user_input, thinking, answer, attachments, created_at"
                " FROM display_turns WHERE conversation_id = ? AND turn_index < ?"
                " ORDER BY turn_index ASC",
                (self.conversation_id, up_to_index),
            ).fetchall()
        count = 0
        for r in rows:
            with target._lock:
                tconn = target._get_conn()
                cur = tconn.execute(
                    "INSERT INTO display_turns (conversation_id, turn_index, user_input, thinking, answer, attachments, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (target.conversation_id, r[0], r[1], r[2], r[3], r[4], r[5]),
                )
                # 复制工具调用
                src_turn_id = conn.execute(
                    "SELECT id FROM display_turns WHERE conversation_id = ? AND turn_index = ?",
                    (self.conversation_id, r[0]),
                ).fetchone()
                if src_turn_id:
                    tools = conn.execute(
                        "SELECT seq, name, arguments, success, call_id, created_at"
                        " FROM display_tool_calls WHERE turn_id = ? ORDER BY seq ASC",
                        (src_turn_id[0],),
                    ).fetchall()
                    new_turn_id = int(cur.lastrowid or 0)
                    for t in tools:
                        tconn.execute(
                            "INSERT INTO display_tool_calls (turn_id, seq, name, arguments, success, call_id, created_at)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (new_turn_id, t[0], t[1], t[2], t[3], t[4], t[5]),
                        )
                tconn.commit()
            count += 1
        # 同步 target 的 turn_index 计数器
        if count > 0:
            target._next_turn_index = rows[-1][0] + 1
        return count



def list_conversations(db_path: str | None = None) -> list[dict[str, Any]]:
    """列出全部会话 (跨 conversation), 供前端会话列表

    模块级函数: 不绑定某个 conversation 实例, 直接开独立连接查询

    参数:
    - db_path: 展示层 db 路径, 默认 .satrap/satrapdata/display.db

    返回按最近活跃倒序: 每会话取首条用户输入作标题 + 轮次计数 + 最近时间
    """
    path = db_path or get_db_path("display.db")
    if not Path(path).exists():
        return []
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        rows = conn.execute(
            """
            SELECT conversation_id,
                   COUNT(*) AS turn_count,
                   MAX(created_at) AS last_at,
                   (SELECT user_input FROM display_turns t2
                     WHERE t2.conversation_id = t.conversation_id
                     ORDER BY turn_index ASC LIMIT 1) AS first_input
            FROM display_turns t
            GROUP BY conversation_id
            ORDER BY last_at DESC
            """
        ).fetchall()
        return [
            {
                "conversation_id": r[0],
                "turn_count": r[1],
                "last_at": r[2],
                "title": (r[3] or "新对话"),
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_conversation_meta(conversation_id: str, db_path: str | None = None) -> dict[str, Any] | None:
    """查询会话元数据 (model / think)

    返回 None 表示会话不存在
    """
    path = db_path or get_db_path("display.db")
    if not Path(path).exists():
        return None
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        row = conn.execute(
            "SELECT model, think, created_at FROM conversation_meta WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return {"model": row[0], "think": row[1] or "off", "created_at": row[2]}
    finally:
        conn.close()
