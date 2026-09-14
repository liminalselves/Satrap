"""
工作流任务和步骤持久化

与会话共用数据库, 负责执行互斥, 步骤恢复与整轮消息的原子提交,
存储接口由统一执行器直接调用, 不注册执行期钩子
"""
from __future__ import annotations

from contextlib import contextmanager
import threading
import hashlib
from pathlib import Path
import sqlite3
import base64
from typing import Any, Iterator
import math
import json
import time
from uuid import uuid4

from satrap.core.storage.file_lock import FileLock
from . import codec


class RunConflictError(RuntimeError):
    """任务作用域, 状态或上下文与恢复条件不符"""


class RunNeedsAttention(RuntimeError):
    """副作用结果未知, 需要人工决定是否重试"""


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def encode(value: Any) -> str:
    """
    严格序列化执行状态, 不用字符串降级隐藏不可恢复的数据

    参数:
    - value: 可序列化的 JSON 数据, 不接受任意 Python 对象

    返回:
    - 规范化 JSON 字符串, 无法序列化时抛出异常
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    """
    计算上下文与配置指纹

    参数:
    - value: 可序列化的 JSON 数据, 不接受任意 Python 对象

    返回:
    - 数据的 SHA-256 指纹
    """
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


class RunStore:
    """与会话使用同一 SQLite 文件, 每个实例绑定单一工作流作用域"""

    def __init__(self, database: str | Path, scope: str):
        """
        初始化指定会话的执行记录存储

        参数:
        - database: SQLite 文件路径, 不允许使用内存数据库
        - scope: 会话标识, 用于隔离执行记录和获取执行锁
        """
        if str(database) == ":memory:":
            raise ValueError("可恢复任务必须使用文件数据库")
        self.database = Path(database).resolve()
        self.scope = scope
        self._connection: sqlite3.Connection | None = None
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, context_fingerprint TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL, result TEXT,
                    error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS agent_runs_scope ON agent_runs(scope, created_at);
                CREATE TABLE IF NOT EXISTS agent_steps (
                    run_id TEXT NOT NULL, step_key TEXT NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL, input TEXT NOT NULL, result TEXT,
                    recovery_policy TEXT NOT NULL DEFAULT 'manual',
                    PRIMARY KEY(run_id, step_key)
                );
                CREATE TABLE IF NOT EXISTS agent_step_inputs (
                    run_id TEXT NOT NULL, step_key TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(run_id, step_key)
                );
                CREATE INDEX IF NOT EXISTS agent_runs_page ON agent_runs(scope, created_at DESC, id DESC);
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """
        为短事务建立连接, 不跨模型或工具调用持有数据库事务

        返回:
        - 事务连接上下文, 退出时提交或回滚并释放自建连接
        """
        db = self._connection or sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            if db is not self._connection:
                db.close()

    @contextmanager
    def claim(self) -> Iterator[None]:
        """
        使用进程内非重入锁和操作系统锁, 防止协程及进程重复执行

        返回:
        - 执行权上下文, 作用域已被占用时抛出 RunConflictError
        """
        key = fingerprint([str(self.database).casefold(), self.scope])
        with _LOCKS_GUARD:
            local = _LOCKS.setdefault(key, threading.Lock())
        if not local.acquire(blocking=False):
            raise RunConflictError("此会话已有任务正在执行")
        try:
            with FileLock(self.database.parent / "locks" / f"run-{key}.lock", timeout=0):
                self._connection = sqlite3.connect(self.database, timeout=10)
                self._connection.row_factory = sqlite3.Row
                try:
                    yield
                finally:
                    self._connection.close()
                    self._connection = None
        except TimeoutError as exc:
            raise RunConflictError("此会话由其他进程执行中") from exc
        finally:
            local.release()

    def create(self, payload: dict[str, Any], context: str, config: str) -> str:
        """
        保存任务输入和执行参数, 调用方须持有作用域执行权

        参数:
        - payload: 用户输入及本轮执行参数
        - context: 开始执行时的持久化历史指纹
        - config: 模型和工具配置指纹

        返回:
        - 新任务 ID
        """
        run_id = uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO agent_runs VALUES (?,?,?,?,?,?,NULL,NULL,?,?)",
                (run_id, self.scope, "running", encode(payload), context, config, now, now),
            )
        return run_id

    def get(self, run_id: str) -> dict[str, Any]:
        """
        任务 ID 必须属于当前作用域

        参数:
        - run_id: 当前会话内的任务 ID

        返回:
        - 当前会话的任务记录, 不存在或不属于当前会话时抛出异常
        """
        with self.connect() as db:
            row = db.execute("SELECT * FROM agent_runs WHERE id=? AND scope=?", (run_id, self.scope)).fetchone()
        if row is None:
            raise RunConflictError("任务不存在或不属于当前会话")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def list(self) -> list[dict[str, Any]]:
        """
        返回作用域内的执行记录

        返回:
        - 当前作用域内的任务列表
        """
        with self.connect() as db:
            rows = db.execute("SELECT id FROM agent_runs WHERE scope=? ORDER BY created_at DESC", (self.scope,)).fetchall()
        return [self.get(row["id"]) for row in rows]

    def update(self, run_id: str, *, status: str, error: str | None = None, result: str | None = None) -> None:
        """
        更新任务状态, 不跨作用域修改

        参数:
        - run_id: 当前会话内的任务 ID
        - status: 待保存的任务状态
        - error: 错误信息, 默认 None
        - result: 最终回答, 默认 None
        """
        if status not in {"running", "completed", "failed", "cancelled", "interrupted", "needs_attention"}:
            raise ValueError("未知任务状态")
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE agent_runs SET status=?, error=?, result=?, updated_at=? WHERE id=? AND scope=?",
                (status, error, result, time.time(), run_id, self.scope),
            )
            if cursor.rowcount != 1:
                raise RunConflictError("任务不存在或不属于当前会话")

    def step(self, run_id: str, key: str) -> dict[str, Any] | None:
        """
        读取已保存步骤, 完成结果可直接复用

        参数:
        - run_id: 当前会话内的任务 ID
        - key: 任务内稳定的步骤标识

        返回:
        - 步骤记录, 不存在时返回 None
        """
        with self.connect() as db:
            row = db.execute("SELECT s.* FROM agent_runs r LEFT JOIN agent_steps s ON s.run_id=r.id AND s.step_key=? WHERE r.id=? AND r.scope=?", (key, run_id, self.scope)).fetchone()
        if row is None:
            raise RunConflictError("任务不存在或不属于当前会话")
        if row["step_key"] is None:
            return None
        result = dict(row)
        result["input"] = json.loads(result["input"])
        if isinstance(result["input"], dict) and "_format" in result["input"] and result["input"]["_format"] != 2:
            raise RunConflictError("不支持的请求格式版本")
        result["result"] = json.loads(result["result"]) if result["result"] is not None else None
        return result

    def summaries(self, limit: int | None = None, cursor: str | None = None, unfinished: bool = False) -> dict[str, Any]:
        """
        两次查询返回一页任务和步骤摘要, 不读取请求正文

        参数:
        - limit: 每页记录上限, 默认 None 使用内部默认值
        - cursor: 上一页返回的游标, 默认 None 从第一页开始
        - unfinished: 是否仅查询未完成任务, 默认 False

        返回:
        - 当前页任务摘要及下一页游标
        """
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
            raise ValueError("分页数量须为 1 到 100")
        where, params = "scope=?", [self.scope]
        if unfinished:
            where += " AND status NOT IN ('completed','cancelled')"
        if cursor:
            try:
                position = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
                created, identifier = position["created"], position["id"]
                if position["scope"] != self.scope or position["unfinished"] != unfinished or not isinstance(identifier, str) or type(created) not in (int, float) or not math.isfinite(created):
                    raise ValueError()
            except (ValueError, KeyError, TypeError, UnicodeError) as error:
                raise ValueError("分页游标无效") from error
            where += " AND (created_at,id) < (?,?)"
            params.extend([created, identifier])
        with self.connect() as db:
            rows = db.execute(f"SELECT id,status,created_at,updated_at,error FROM agent_runs WHERE {where} ORDER BY created_at DESC,id DESC" + (" LIMIT ?" if limit is not None else ""), (*params, limit + 1) if limit is not None else params).fetchall()
            more = limit is not None and len(rows) > limit
            items = [dict(row) for row in (rows[:limit] if limit is not None else rows)]
            by_id = {item["id"]: item for item in items}
            for item in items:
                item["steps"] = []
            if items:
                columns = "SELECT s.run_id,s.step_key,s.kind,s.status,s.recovery_policy FROM agent_steps s"
                if limit is None:
                    steps = db.execute(f"{columns} JOIN agent_runs r ON r.id=s.run_id WHERE {where} ORDER BY s.rowid", params).fetchall()
                else:
                    placeholders = ",".join("?" for _ in by_id)
                    steps = db.execute(f"{columns} WHERE s.run_id IN ({placeholders}) ORDER BY s.rowid", list(by_id)).fetchall()
                for row in steps:
                    step = dict(row)
                    by_id[step.pop("run_id")]["steps"].append(step)
        next_cursor = None
        if more:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(encode({"scope": self.scope, "unfinished": unfinished, "created": last["created_at"], "id": last["id"]}).encode()).decode("ascii")
        return {"runs": items, "next_cursor": next_cursor}

    def model_request(self, run_id: str, key: str) -> tuple[dict[str, Any], int]:
        """
        只在重发未完成模型请求或编码下个请求时读取正文

        参数:
        - run_id: 当前会话内的任务 ID
        - key: 任务内稳定的步骤标识

        返回:
        - 重建的完整请求与引用深度, 数据损坏时抛出异常
        """
        with self.connect() as db:
            def read(current: str, seen: set[str]) -> tuple[dict[str, Any], int]:
                """
                递归恢复已保存的模型请求并检测循环引用

                参数:
                - current: 正在解码的模型步骤标识
                - seen: 已访问标识集合, 用于检测循环引用

                返回:
                - 重建的完整请求与引用深度, 循环引用或记录缺失时抛出异常
                """
                if current in seen or len(seen) > codec.MAX_DEPTH:
                    raise RunConflictError("请求引用循环或过深")
                seen.add(current)
                row = db.execute("SELECT s.input,b.body FROM agent_steps s JOIN agent_runs r ON r.id=s.run_id LEFT JOIN agent_step_inputs b ON b.run_id=s.run_id AND b.step_key=s.step_key WHERE s.run_id=? AND s.step_key=? AND s.kind='model' AND r.scope=?", (run_id, current, self.scope)).fetchone()
                if row is None:
                    raise RunConflictError("模型请求不存在或不属于当前会话")
                metadata = json.loads(row["input"])
                if "_format" not in metadata:
                    return metadata, 0
                if metadata["_format"] != 2 or row["body"] is None:
                    raise RunConflictError("请求格式不支持或正文缺失")
                body = json.loads(row["body"])
                previous = None
                if body.get("base") is not None:
                    try:
                        earlier = codec.model_position(body["base"]) < codec.model_position(current)
                    except ValueError as error:
                        raise RunConflictError(str(error)) from error
                    if not earlier:
                        raise RunConflictError("请求只能引用同一任务内更早的模型步骤")
                    previous = read(body["base"], seen)
                try:
                    request = codec.unpack(body, previous)
                except (ValueError, KeyError, TypeError) as error:
                    raise RunConflictError(str(error)) from error
                return {**request, "_prepared": metadata.get("_prepared", {})}, body["depth"]
            return read(key, set())

    def start_model_step(self, run_id: str, key: str, request: dict[str, Any]) -> None:
        """
        请求正文与执行意图原子保存, 完成前不发起模型调用

        参数:
        - run_id: 当前会话内的任务 ID
        - key: 任务内稳定的步骤标识
        - request: 完整模型请求, 包含消息及调用参数
        """
        position = codec.model_position(key)
        with self.connect() as db:
            prior = db.execute("SELECT s.step_key FROM agent_steps s JOIN agent_runs r ON r.id=s.run_id WHERE s.run_id=? AND s.kind='model' AND r.scope=? AND CAST(substr(s.step_key,7) AS INTEGER) < ? ORDER BY CAST(substr(s.step_key,7) AS INTEGER) DESC LIMIT 1", (run_id, self.scope, position)).fetchone()
        previous = None
        if prior is not None:
            actual, depth = self.model_request(run_id, prior[0])
            previous = (prior[0], actual, depth)
        body = encode(codec.pack(request, previous))
        metadata = {"_format": 2, "_prepared": request.get("_prepared", {})}
        with self.connect() as db:
            cursor = db.execute("INSERT INTO agent_steps(run_id,step_key,kind,status,input,recovery_policy) SELECT id,?,'model','running',?,'retry' FROM agent_runs WHERE id=? AND scope=? AND status='running'", (key, encode(metadata), run_id, self.scope))
            if cursor.rowcount != 1:
                raise RunConflictError("任务不存在或不在执行中")
            db.execute("INSERT INTO agent_step_inputs VALUES (?,?,?)", (run_id, key, body))

    def start_step(self, run_id: str, key: str, kind: str, value: Any, policy: str = "manual") -> None:
        """
        先保存执行意图, 未完成步骤保留原输入

        参数:
        - run_id: 当前会话内的任务 ID
        - key: 任务内稳定的步骤标识
        - kind: 步骤种类, 用于区分模型与工具
        - value: 可序列化的 JSON 数据, 不接受任意 Python 对象
        - policy: 中断后的重试策略, 默认 manual 要求人工确认
        """
        if policy not in {"retry", "manual"}:
            raise ValueError("未知工具恢复策略")
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO agent_steps(run_id,step_key,kind,status,input,recovery_policy) SELECT id,?,?,'running',?,? FROM agent_runs WHERE id=? AND scope=? AND status='running'",
                (key, kind, encode(value), policy, run_id, self.scope),
            )
            if cursor.rowcount != 1:
                raise RunConflictError("任务不存在或不在执行中")

    def finish_step(self, run_id: str, key: str, result: Any) -> None:
        """
        完整结果一次落盘, 不按流式 token 写入

        参数:
        - run_id: 当前会话内的任务 ID
        - key: 任务内稳定的步骤标识
        - result: 步骤结果或最终回答, 保存时须可序列化
        """
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE agent_steps SET status='completed', result=? WHERE run_id=? AND step_key=? AND status='running' AND EXISTS (SELECT 1 FROM agent_runs WHERE id=? AND scope=? AND status='running')",
                (encode(result), run_id, key, run_id, self.scope),
            )
            if cursor.rowcount != 1:
                raise RunConflictError("步骤不存在或已经结束")

    def authorize_retry(self, run_id: str, key: str) -> None:
        """
        显式授权未知结果步骤的重试, 不清除执行记录

        参数:
        - run_id: 当前会话内的任务 ID
        - key: 任务内稳定的步骤标识
        """
        with self.claim():
            run = self.get(run_id)
            if run["status"] != "needs_attention":
                raise RunConflictError("仅等待人工处理的任务允许授权重试")
            with self.connect() as db:
                cursor = db.execute(
                    "UPDATE agent_steps SET recovery_policy='retry' WHERE run_id=? AND step_key=? AND status='running' AND kind='tool'",
                    (run_id, key),
                )
                if cursor.rowcount != 1:
                    raise RunConflictError("没有等待确认的工具步骤")

    def abort(self, run_id: str) -> None:
        """
        终止未完成任务, 不删除已发生的执行结果

        参数:
        - run_id: 当前会话内的任务 ID
        """
        with self.claim():
            if self.get(run_id)["status"] in {"completed", "cancelled"}:
                raise RunConflictError("任务已经结束")
            self.update(run_id, status="cancelled")

    def history_signature(self, db: sqlite3.Connection | None = None) -> str:
        """
        使用持久化消息及行 ID 检测追加, 编辑, 回滚等历史变更

        参数:
        - db: 可复用的数据库连接, 默认 None 建立短连接

        返回:
        - 当前持久化会话历史的指纹
        """
        if db is None:
            with self.connect() as connection:
                return self.history_signature(connection)
        rows = db.execute("SELECT * FROM chat_history WHERE conversation_id=? ORDER BY id", (self.scope,)).fetchall()
        return fingerprint([dict(row) for row in rows])

    def commit_messages(self, run_id: str, messages: list[dict[str, Any]], result: str) -> None:
        """
        消息追加与完成标记在同一事务提交, 重复恢复不重复写入

        参数:
        - run_id: 当前会话内的任务 ID
        - messages: 本轮完整消息, 仅在执行成功后提交
        - result: 步骤结果或最终回答, 保存时须可序列化
        """
        from satrap.core.utils.context.utils import _message_content_json
        from satrap.core.utils.vision import content_text_projection

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM agent_runs WHERE id=? AND scope=?", (run_id, self.scope)).fetchone()
            if run is None:
                raise RunConflictError("任务不存在或不属于当前会话")
            if run["status"] == "completed":
                return
            if run["status"] != "running":
                raise RunConflictError("仅执行中的任务可以提交消息")
            if self.history_signature(db) != run["context_fingerprint"]:
                raise RunConflictError("会话历史已改变, 不能提交旧任务结果")
            for message in messages:
                db.execute(
                    "INSERT INTO chat_history(conversation_id,role,content,content_json,tool_call_id,tool_calls,reasoning_content) VALUES (?,?,?,?,?,?,?)",
                    (self.scope, message["role"], content_text_projection(message.get("content")),
                     _message_content_json(message.get("content")), message.get("tool_call_id"),
                     encode(message["tool_calls"]) if message.get("tool_calls") else None,
                     message.get("reasoning_content")),
                )
            payload = json.loads(run["payload"])
            payload["committed_context_fingerprint"] = self.history_signature(db)
            db.execute("UPDATE agent_runs SET status='completed',result=?,payload=?,error=NULL,updated_at=? WHERE id=?",
                       (result, encode(payload), time.time(), run_id))
