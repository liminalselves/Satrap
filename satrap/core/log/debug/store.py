"""
独立高级诊断库的追加与分页读取

短事务写入稳定对象, 请求和事件, 正文通过不可变池共享;
读取摘要不解码正文, 清理只回收不再被事件引用的资料
"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator, Sequence
from pathlib import Path
import json
import base64
import math
import sqlite3
import threading
import time
from typing import cast

from .codec import DebugBodyCodec, encoded_json
from .contracts import DebugData, DebugEvent, DebugObject, EventPage, EventSummary
from .runtime import runtime_alive
from .diagnostics import LEVEL_RANK, RANK_LEVEL, event_diagnostic


def _cursor(values: list[DebugData]) -> str:
    """
    编码稳定复合游标

    参数:
    - values: 排序时间及唯一键

    返回:
    - 有界 URL 安全游标
    """
    return base64.urlsafe_b64encode(encoded_json(values)).decode("ascii")


def _parse_cursor(value: str, keys: int) -> tuple[float, list[str]]:
    """
    校验分页游标, 不以时间戳单独排序造成同刻记录遗漏

    参数:
    - value: 上次响应的游标
    - keys: 预期唯一键字段数量

    返回:
    - 时间和顺序键, 无效时抛出 ValueError
    """
    if len(value) > 4096:
        raise ValueError("高级诊断游标过长")
    try:
        data = json.loads(base64.urlsafe_b64decode(value))
    except (ValueError, UnicodeError) as error:
        raise ValueError("高级诊断游标无效") from error
    if (not isinstance(data, list) or len(data) != keys + 1 or isinstance(data[0], bool)
            or not isinstance(data[0], (int, float)) or not math.isfinite(data[0])
            or any(not isinstance(item, str) or len(item) > 512 for item in data[1:])):
        raise ValueError("高级诊断游标结构无效")
    return float(data[0]), cast(list[str], data[1:])


class DebugStore:
    """每服务独立持有的诊断存储, 不修改业务数据库"""

    def __init__(self, path: Path, service: str) -> None:
        """
        绑定受宿主控制的数据库路径

        参数:
        - path: 固定服务诊断库位置
        - service: 当前服务标识
        """
        self.path, self.service = path.resolve(), service
        self._ready = False
        self._schema_lock = threading.Lock()

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """
        获取短期连接, 写操作由后台调用

        参数:
        - write: 是否允许初始化和事务写入

        返回:
        - 开启外键和行名读取的连接, 退出后关闭
        """
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=2)
        else:
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                with self._schema_lock:
                    if not self._ready:
                        self._initialize(db)
                        self._ready = True
                with db:
                    yield db
            else:
                db.execute("PRAGMA query_only=ON")
                yield db
        finally:
            db.close()

    @staticmethod
    def _initialize(db: sqlite3.Connection) -> None:
        """
        初始化版本化的独立诊断结构

        参数:
        - db: 当前写连接
        """
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1, 2}:
            raise ValueError("高级诊断库版本不支持")
        if version == 0:
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS debug_objects (
                object_key TEXT PRIMARY KEY, identity_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS debug_contexts (
                object_key TEXT NOT NULL REFERENCES debug_objects(object_key),
                context_id TEXT NOT NULL, title TEXT NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(object_key, context_id)
            );
            CREATE TABLE IF NOT EXISTS debug_requests (
                request_id TEXT PRIMARY KEY, object_key TEXT NOT NULL REFERENCES debug_objects(object_key),
                context_id TEXT NOT NULL, title TEXT NOT NULL, parent_request_id TEXT NOT NULL,
                started_at REAL NOT NULL, updated_at REAL NOT NULL, status TEXT NOT NULL,
                incomplete INTEGER NOT NULL DEFAULT 0, settings_revision TEXT NOT NULL,
                diagnostic_level INTEGER
            );
            CREATE INDEX IF NOT EXISTS debug_request_context ON debug_requests(object_key,context_id,updated_at DESC,request_id);
            CREATE INDEX IF NOT EXISTS debug_request_time ON debug_requests(updated_at DESC,request_id);
            CREATE INDEX IF NOT EXISTS debug_request_status ON debug_requests(status,updated_at DESC,request_id);
            CREATE INDEX IF NOT EXISTS debug_context_time ON debug_contexts(updated_at DESC,object_key,context_id);
            CREATE TABLE IF NOT EXISTS debug_request_contexts (
                request_id TEXT NOT NULL REFERENCES debug_requests(request_id) ON DELETE CASCADE,
                context_id TEXT NOT NULL, PRIMARY KEY(request_id,context_id)
            );
            CREATE INDEX IF NOT EXISTS debug_context_requests ON debug_request_contexts(context_id,request_id);
            CREATE TABLE IF NOT EXISTS debug_links (
                kind TEXT NOT NULL, link_key TEXT NOT NULL,
                request_id TEXT NOT NULL REFERENCES debug_requests(request_id) ON DELETE CASCADE,
                PRIMARY KEY(kind,link_key,request_id)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS debug_events (
                event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES debug_requests(request_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL, category TEXT NOT NULL, phase TEXT NOT NULL,
                span_id TEXT NOT NULL, parent_span_id TEXT NOT NULL, recorded_at REAL NOT NULL,
                status TEXT NOT NULL, metadata_json TEXT NOT NULL, UNIQUE(request_id,sequence)
            );
            CREATE INDEX IF NOT EXISTS debug_event_request ON debug_events(request_id,sequence);
        """)
        DebugBodyCodec.initialize(db)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS debug_event_bodies (
                event_id TEXT NOT NULL REFERENCES debug_events(event_id) ON DELETE CASCADE,
                slot TEXT NOT NULL, body_key TEXT NOT NULL REFERENCES debug_bodies(key),
                PRIMARY KEY(event_id,slot)
            );
            CREATE INDEX IF NOT EXISTS debug_body_roots ON debug_event_bodies(body_key);
        """)
        with db:
            db.execute("BEGIN IMMEDIATE")
            if "diagnostic_level" not in {row[1] for row in db.execute("PRAGMA table_info(debug_requests)")}:
                db.execute("ALTER TABLE debug_requests ADD COLUMN diagnostic_level INTEGER")
            db.execute("PRAGMA user_version=2")
        # 只添加可空汇总列, 旧请求保持未分类, 不还原历史正文或逐条回填

    def append(self, events: Sequence[DebugEvent]) -> None:
        """
        在一个短事务中持久化冻结事件和共享正文

        参数:
        - events: 后台取得的有限批次, 不跨事务执行模型或工具
        """
        if not events:
            return
        with self.connect(write=True) as db:
            codec = DebugBodyCodec(db)
            for event in events:
                metadata = {**event.metadata, **event_diagnostic(event.category, event.status, event.metadata)}
                level = metadata["diagnostic_level"]
                rank = LEVEL_RANK.get(str(level), -1)
                if event.incomplete:
                    rank = max(rank, 1)
                obj = event.object
                db.execute("INSERT OR IGNORE INTO debug_objects VALUES(?,?)", (obj.key, json.dumps(obj.payload(), ensure_ascii=False)))
                db.execute("""INSERT INTO debug_contexts VALUES(?,?,?,?) ON CONFLICT(object_key,context_id)
                              DO UPDATE SET title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE title END,
                              updated_at=MAX(updated_at,excluded.updated_at)""",
                           (obj.key, event.context_id, event.title, event.recorded_at))
                db.execute("""INSERT INTO debug_requests
                              (request_id,object_key,context_id,title,parent_request_id,started_at,updated_at,status,incomplete,settings_revision,diagnostic_level)
                              VALUES(?,?,?,?,?,?,?,'running',?,?,0) ON CONFLICT(request_id)
                              DO UPDATE SET context_id=CASE WHEN excluded.context_id<>'' THEN excluded.context_id ELSE context_id END,
                              title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE title END,
                              parent_request_id=CASE WHEN excluded.parent_request_id<>'' THEN excluded.parent_request_id ELSE parent_request_id END,
                              started_at=MIN(started_at,excluded.started_at),
                              updated_at=MAX(updated_at,excluded.updated_at), incomplete=MAX(incomplete,excluded.incomplete)""",
                           (event.request_id, obj.key, event.context_id, event.title, event.parent_request_id,
                            event.recorded_at, event.recorded_at, int(event.incomplete), event.settings_revision))
                owner = db.execute("SELECT object_key FROM debug_requests WHERE request_id=?", (event.request_id,)).fetchone()
                if owner[0] != obj.key:
                    raise ValueError("诊断请求不能跨对象写入")
                db.execute("INSERT OR IGNORE INTO debug_request_contexts VALUES(?,?)", (event.request_id, event.context_id))
                if event.context_id:
                    db.execute("DELETE FROM debug_request_contexts WHERE request_id=? AND context_id=''", (event.request_id,))
                child_context = event.metadata.get("context_id") if event.category in {"execution_run", "context_preparation"} else None
                if isinstance(child_context, str) and child_context:
                    db.execute("INSERT OR IGNORE INTO debug_request_contexts VALUES(?,?)", (event.request_id, child_context))
                    db.execute("INSERT INTO debug_contexts VALUES(?,?,?,?) ON CONFLICT(object_key,context_id) DO UPDATE SET updated_at=MAX(updated_at,excluded.updated_at)",
                               (obj.key, child_context, child_context, event.recorded_at))
                inserted = db.execute("INSERT OR IGNORE INTO debug_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                                      (event.event_id, event.request_id, event.sequence, event.category, event.phase,
                                       event.span_id, event.parent_span_id, event.recorded_at, event.status,
                                       encoded_json(metadata).decode("utf-8")))
                if inserted.rowcount:
                    db.execute("""UPDATE debug_requests SET diagnostic_level=CASE
                                  WHEN diagnostic_level IS NULL AND ?<=0 THEN NULL
                                  ELSE MAX(COALESCE(diagnostic_level,-1),?) END WHERE request_id=?""", (rank, rank, event.request_id))
                    for slot, body in event.bodies.items():
                        key = codec.put(body)
                        db.execute("INSERT INTO debug_event_bodies VALUES(?,?,?)", (event.event_id, slot, key))
                if event.category == "execution_run" and isinstance(event.metadata.get("run_id"), str):
                    db.execute("INSERT OR IGNORE INTO debug_links VALUES('run',?,?)", (event.metadata["run_id"], event.request_id))
                if event.category == "chat" and event.phase == "start":
                    version = event.metadata.get("reply_version")
                    if isinstance(version, dict) and type(version.get("turn_id")) is int and type(version.get("variant_index")) is int:
                        link = json.dumps([obj.key, event.context_id, version["turn_id"], version["variant_index"]])
                        db.execute("INSERT OR IGNORE INTO debug_links VALUES('turn',?,?)", (link, event.request_id))
                if event.category == "context_fork" and isinstance(event.metadata.get("target_context"), str):
                    db.execute("INSERT OR IGNORE INTO debug_links VALUES('fork',?,?)", (event.metadata["target_context"], event.request_id))
                if event.category == "platform_approval" and isinstance(event.metadata.get("action_link"), str):
                    db.execute("INSERT OR IGNORE INTO debug_links VALUES('action',?,?)", (event.metadata["action_link"], event.request_id))
                if event.category == "request" and event.phase == "end":
                    db.execute("UPDATE debug_requests SET status=?,incomplete=MAX(incomplete,?) WHERE request_id=?",
                               (event.status, int(event.incomplete), event.request_id))

    @staticmethod
    def _limit(limit: int) -> int:
        """
        校验分页上限, 不隐式允许无界查询

        参数:
        - limit: 请求分页条数

        返回:
        - 已校验条数
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("高级诊断分页条数必须为 1 至 100")
        return limit

    def find_link(self, kind: str, key: str) -> str:
        """
        读取已记录业务身份对应的最近诊断请求, 不复制来源正文

        参数:
        - kind: 宿主固定的 run, turn 或 fork
        - key: 已校验的真实业务身份

        返回:
        - 对应请求 ID, 无记录或已清理时为空字符串
        """
        if kind not in {"run", "turn", "fork", "action"} or not self.path.is_file():
            return ""
        with self.connect() as db:
            row = db.execute("SELECT l.request_id FROM debug_links l JOIN debug_requests r ON r.request_id=l.request_id WHERE l.kind=? AND l.link_key=? ORDER BY r.updated_at DESC LIMIT 1", (kind, key)).fetchone()
            return row[0] if row else ""

    def action_source(self, key: str) -> tuple[DebugObject, str, str, str] | None:
        """
        为延后审批恢复已采集的真实来源, 不从动作目标反推操作者所在群

        参数:
        - key: 已加入平台数据库命名空间的动作摘要

        返回:
        - 对象, 来源请求, 上下文及标题, 没有既有证据时返回 None
        """
        request_id = self.find_link("action", key)
        if not request_id:
            return None
        with self.connect() as db:
            row = db.execute("SELECT o.identity_json,r.context_id,r.title FROM debug_requests r JOIN debug_objects o ON o.object_key=r.object_key WHERE r.request_id=?", (request_id,)).fetchone()
            return (DebugObject.parse(json.loads(row[0])), request_id, row[1], row[2]) if row else None

    def request_info(self, request_id: str) -> dict[str, object]:
        """
        精确读取导出目标摘要, 不扫描其它请求

        参数:
        - request_id: 当前服务中的实际请求 ID

        返回:
        - 请求原始摘要, 不存在时明确失败
        """
        if not request_id or len(request_id) > 512 or not self.path.is_file():
            raise ValueError("诊断请求不存在")
        with self.connect() as db:
            row = db.execute("SELECT * FROM debug_requests WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise ValueError("诊断请求不存在")
            return self._request_summary(db, row, {})

    def _request_summary(self, db: sqlite3.Connection, row: sqlite3.Row, runtimes: dict[str, bool | None]) -> dict[str, object]:
        """
        共用完整性与中断判定, 单条详情和分页列表保持一致

        参数:
        - db: 当前只读连接
        - row: 已定位请求
        - runtimes: 本页已检查过的写入器租约

        返回:
        - 带当前完整性和已确认中断状态的请求摘要
        """
        item = dict(row)
        stored_level = item.get("diagnostic_level")
        item["diagnostic_level"] = RANK_LEVEL.get(stored_level, "unknown") if isinstance(stored_level, int) else "unknown"
        count, first, last_sequence = db.execute("SELECT count(*),min(sequence),max(sequence) FROM debug_events WHERE request_id=?", (row["request_id"],)).fetchone()
        complete = first == 0 and last_sequence is not None and count == last_sequence+1
        item["incomplete"] = int(bool(row["incomplete"]) or (row["status"] != "running" and not complete))
        if item["incomplete"] and item["diagnostic_level"] != "error":
            item["diagnostic_level"] = "warning"
        if row["status"] == "running":
            start = db.execute("SELECT metadata_json FROM debug_events WHERE request_id=? AND category='request' AND phase='start' ORDER BY sequence LIMIT 1", (row["request_id"],)).fetchone()
            identity = json.loads(start[0]).get("runtime_id") if start else None
            if isinstance(identity, str):
                if identity not in runtimes:
                    runtimes[identity] = runtime_alive(self.path.parent, self.service, identity)
                if runtimes[identity] is False:
                    item.update(status="interrupted", incomplete=1)
                    if item["diagnostic_level"] != "error":
                        item["diagnostic_level"] = "warning"
        return item

    def contexts(self, *, limit: int = 50, cursor: str = "", object_key: str = "", query: str = "",
                 platform_id: str = "", context_kind: str = "", recording_keys: Sequence[str] | None = None,
                 exclude_keys: bool = False, group_objects: bool = False) -> dict[str, object]:
        """
        按最近活动读取上下文目录, 不解码任何正文

        参数:
        - limit: 分页条数
        - cursor: 可选稳定复合游标
        - object_key: 可选对象过滤
        - query: 标题, 上下文或对象身份文本
        - platform_id: 可选精确平台实例
        - context_kind: main 或 workflow
        - recording_keys: 设置服务解析出的对象键, None 时不过滤
        - exclude_keys: 是否排除这些键, 用于动态继承默认
        - group_objects: 是否先按完整对象聚合, 默认保留上下文分页

        返回:
        - 含身份和上下文标识的有界目录
        """
        self._limit(limit)
        if len(query) > 512 or len(platform_id) > 512 or context_kind not in ("", "main", "workflow"):
            raise ValueError("上下文过滤参数无效")
        if not self.path.is_file():
            return {"objects" if group_objects else "contexts": [], "has_more": False}
        with self.connect() as db:
            where, args = ["EXISTS(SELECT 1 FROM debug_request_contexts l CROSS JOIN debug_requests r ON r.request_id=l.request_id WHERE l.context_id=c.context_id AND r.object_key=c.object_key)"], []
            if cursor and not group_objects:
                before, keys = _parse_cursor(cursor, 2)
                where.append("(c.updated_at<? OR (c.updated_at=? AND (c.object_key>? OR (c.object_key=? AND c.context_id>?))))")
                args.extend((before, before, keys[0], keys[0], keys[1]))
            if object_key:
                where.append("c.object_key=?")
                args.append(object_key)
            if query:
                escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                where.append("(c.title||' '||c.context_id||' '||o.identity_json) LIKE ? ESCAPE '!' ")
                args.append("%" + escaped + "%")
            if platform_id:
                where.append("json_extract(o.identity_json,'$.platform_id')=?")
                args.append(platform_id)
            if context_kind:
                where.append(("NOT " if context_kind == "workflow" else "") + "EXISTS(SELECT 1 FROM debug_requests root WHERE root.object_key=c.object_key AND root.context_id=c.context_id)")
            if recording_keys is not None:
                where.append("c.object_key " + ("NOT IN" if exclude_keys else "IN") + " (SELECT value FROM json_each(?))")
                args.append(json.dumps(list(recording_keys)))
            if group_objects:
                page_where, page_args = "", []
                if cursor:
                    before, keys = _parse_cursor(cursor, 1)
                    page_where = "WHERE (updated_at<? OR (updated_at=? AND object_key>?))"
                    page_args.extend((before, before, keys[0]))
                rows = db.execute(f"""WITH matching AS (
                          SELECT c.* FROM debug_contexts c JOIN debug_objects o ON o.object_key=c.object_key
                          WHERE {' AND '.join(where)}
                        ), related AS (
                          SELECT DISTINCT r.request_id,r.object_key,r.status FROM matching c
                          JOIN debug_request_contexts l ON l.context_id=c.context_id
                          JOIN debug_requests r ON r.request_id=l.request_id AND r.object_key=c.object_key
                        ), grouped AS (
                          SELECT object_key,max(updated_at) updated_at,count(*) context_count FROM matching GROUP BY object_key
                        ) SELECT g.*,o.identity_json,
                          (SELECT count(*) FROM related r WHERE r.object_key=g.object_key) request_count,
                          (SELECT count(*) FROM related r WHERE r.object_key=g.object_key AND r.status IN ('failed','timeout','unknown','interrupted')) error_count
                        FROM (SELECT * FROM grouped {page_where} ORDER BY updated_at DESC,object_key LIMIT ?) g
                        JOIN debug_objects o ON o.object_key=g.object_key ORDER BY g.updated_at DESC,g.object_key""",
                                  (*args, *page_args, limit + 1)).fetchall()
                object_values = [{**{key: row[key] for key in row.keys() if key != "identity_json"},
                           "object": json.loads(row["identity_json"]), "service": self.service} for row in rows[:limit]]
                last = rows[limit - 1] if len(rows) > limit else None
                return {"objects": object_values, "has_more": len(rows) > limit,
                        "next_cursor": _cursor([last["updated_at"], last["object_key"]]) if last else None}
            rows = db.execute(f"""SELECT c.*,o.identity_json,
                       (SELECT count(*) FROM debug_request_contexts l CROSS JOIN debug_requests r ON r.request_id=l.request_id WHERE l.context_id=c.context_id AND r.object_key=c.object_key) request_count,
                       (SELECT count(*) FROM debug_request_contexts l CROSS JOIN debug_requests r ON r.request_id=l.request_id WHERE l.context_id=c.context_id AND r.object_key=c.object_key AND r.status IN ('failed','timeout','unknown','interrupted')) error_count,
                       CASE WHEN EXISTS(SELECT 1 FROM debug_requests r WHERE r.object_key=c.object_key AND r.context_id=c.context_id) THEN 'main' ELSE 'workflow' END context_kind
                       FROM debug_contexts c JOIN debug_objects o ON o.object_key=c.object_key
                       WHERE {' AND '.join(where)} ORDER BY c.updated_at DESC,c.object_key,c.context_id LIMIT ?""", (*args, limit + 1)).fetchall()
            values: list[dict[str, object]] = []
            for row in rows[:limit]:
                item = dict(row)
                item["object"] = json.loads(item.pop("identity_json"))
                item["service"] = self.service
                values.append(item)
            last = rows[limit-1] if len(rows) > limit else None
            return {"contexts": values, "has_more": len(rows) > limit,
                    "next_cursor": _cursor([last["updated_at"], last["object_key"], last["context_id"]]) if last else None}

    def requests(self, *, context_id: str | None = None, object_key: str = "", cursor: str = "",
                 status: str = "", limit: int = 50, started_after: float | None = None, started_before: float | None = None) -> dict[str, object]:
        """
        分页读取请求摘要, 正文仅在展开事件时读取

        参数:
        - context_id: 可选上下文
        - object_key: 可选完整对象摘要
        - cursor: 可选稳定分页游标
        - status: 可选终态
        - limit: 分页条数
        - started_after: 可选开始时间下界
        - started_before: 可选开始时间上界

        返回:
        - 请求列表和后续页标记
        """
        self._limit(limit)
        if any(value is not None and (not math.isfinite(value) or value < 0) for value in (started_after, started_before)):
            raise ValueError("请求时间范围无效")
        if started_after is not None and started_before is not None and started_after > started_before:
            raise ValueError("请求开始时间不能晚于结束时间")
        if not self.path.is_file():
            return {"requests": [], "has_more": False}
        clauses, args = ["1=1"], []
        if context_id is not None:
            clauses.append("EXISTS(SELECT 1 FROM debug_request_contexts l WHERE l.request_id=debug_requests.request_id AND l.context_id=?)")
            args.append(context_id)
        for field, value in (("object_key", object_key), ("status", status)):
            if value:
                clauses.append(f"{field}=?")
                args.append(value)
        for operator, value in ((">=", started_after), ("<=", started_before)):
            if value is not None:
                clauses.append(f"started_at{operator}?")
                args.append(value)
        if cursor:
            before, keys = _parse_cursor(cursor, 1)
            clauses.append("(updated_at<? OR (updated_at=? AND request_id>?))")
            args.extend((before, before, keys[0]))
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM debug_requests WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC,request_id LIMIT ?", (*args, limit + 1)).fetchall()
            last = rows[limit-1] if len(rows) > limit else None
            values: list[dict[str, object]] = []
            runtimes: dict[str, bool | None] = {}
            for row in rows[:limit]:
                values.append(self._request_summary(db, row, runtimes))
            return {"requests": values, "has_more": len(rows) > limit,
                    "next_cursor": _cursor([last["updated_at"], last["request_id"]]) if last else None}

    def events(self, request_id: str, *, after: int = -1, limit: int = 100) -> EventPage:
        """
        增量读取单请求事件及可展开槽位, 不泄露全局正文摘要

        参数:
        - request_id: 已授权请求标识
        - after: 上次序号, -1 表示开始
        - limit: 最大事件数

        返回:
        - 事件摘要, 正文槽位及完整性资料
        """
        self._limit(limit)
        if after < -1 or not request_id or len(request_id) > 512:
            raise ValueError("诊断请求或游标无效")
        if not self.path.is_file():
            return {"events": [], "has_more": False}
        with self.connect() as db:
            rows = db.execute("SELECT * FROM debug_events WHERE request_id=? AND sequence>? ORDER BY sequence LIMIT ?", (request_id, after, limit + 1)).fetchall()
            values: list[EventSummary] = []
            slots: dict[str, list[str]] = {}
            if rows:
                keys = [row["event_id"] for row in rows[:limit]]
                for row in db.execute("SELECT event_id,slot FROM debug_event_bodies WHERE event_id IN (" + ",".join("?" for _ in keys) + ")", keys):
                    slots.setdefault(row[0], []).append(row[1])
            for row in rows[:limit]:
                item = dict(row)
                metadata = json.loads(item.pop("metadata_json"))
                item["metadata"] = {**metadata, **event_diagnostic(row["category"], row["status"], metadata, legacy=True)}
                item["body_slots"] = slots.get(row["event_id"], [])
                values.append(cast(EventSummary, item))
            return {"events": values, "has_more": len(rows) > limit}

    def body(self, request_id: str, event_id: str, slot: str, *, max_bytes: int = 64_000_000) -> DebugData:
        """
        通过真实事件关联读取正文, 不接受任意内容摘要

        参数:
        - request_id: 当前请求
        - event_id: 请求内事件
        - slot: 事件中的正文槽位
        - max_bytes: 解码原始字节预算

        返回:
        - 还原正文, 关联不符或正文已失效时明确失败
        """
        with self.connect() as db:
            row = db.execute("""SELECT b.body_key FROM debug_event_bodies b JOIN debug_events e ON e.event_id=b.event_id
                                WHERE e.request_id=? AND e.event_id=? AND b.slot=?""", (request_id, event_id, slot)).fetchone()
            if row is None:
                raise ValueError("诊断正文不存在, 已清理或不属于当前事件")
            return DebugBodyCodec(db).read(row[0], max_bytes=max_bytes)

    def body_delta(self, request_id: str, event_id: str, slot: str) -> DebugData:
        """
        按需读取动作正文, 模型输入只返回当前新增或变更部分

        参数:
        - request_id: 当前请求
        - event_id: 请求内事件
        - slot: 正文槽位, 非模型输入保持原样

        返回:
        - 浏览增量或原始动作正文, 不跨请求或父步骤比较模型历史
        """
        with self.connect() as db:
            row = db.execute("""SELECT e.*,b.body_key FROM debug_events e JOIN debug_event_bodies b ON b.event_id=e.event_id
                                WHERE e.request_id=? AND e.event_id=? AND b.slot=?""", (request_id, event_id, slot)).fetchone()
            if row is None:
                raise ValueError("诊断正文不存在, 已清理或不属于当前事件")
            codec = DebugBodyCodec(db)
            if row["category"] != "model" or row["phase"] != "start" or slot != "input":
                return codec.read(row["body_key"])
            previous = db.execute("""SELECT b.body_key FROM debug_events e JOIN debug_event_bodies b ON b.event_id=e.event_id
                                     WHERE e.request_id=? AND e.parent_span_id=? AND e.category='model' AND e.phase='start'
                                     AND e.sequence<? AND b.slot='input' ORDER BY e.sequence DESC LIMIT 1""",
                                  (request_id, row["parent_span_id"], row["sequence"])).fetchone()
            return codec.read_delta(row["body_key"], previous[0] if previous else "")

    def cleanup(self, *, retention_days: int, target_bytes: int) -> dict[str, int]:
        """
        清理已结束链路并回收无引用正文, 不碰业务恢复数据

        参数:
        - retention_days: 结束请求保留天数
        - target_bytes: 当前服务目标库大小

        返回:
        - 删除数量及库和 WAL 占用, 不宣称逻辑删除立即缩小磁盘文件
        """
        if not self.path.is_file():
            return {"deleted_requests": 0, "database_bytes": 0, "wal_bytes": 0}
        with self.connect(write=True) as db:
            unfinished = db.execute("SELECT r.request_id,e.metadata_json FROM debug_requests r JOIN debug_events e ON e.request_id=r.request_id AND e.category='request' AND e.phase='start' WHERE r.status='running' ORDER BY r.updated_at LIMIT 100").fetchall()
            for row in unfinished:
                if runtime_alive(self.path.parent, self.service, json.loads(row[1]).get("runtime_id")) is False:
                    db.execute("UPDATE debug_requests SET status='interrupted',incomplete=1 WHERE request_id=?", (row[0],))
            removed = db.execute("DELETE FROM debug_requests WHERE status<>'running' AND updated_at<?", (time.time() - retention_days * 86400,)).rowcount
            used = (db.execute("PRAGMA page_count").fetchone()[0] - db.execute("PRAGMA freelist_count").fetchone()[0]) * db.execute("PRAGMA page_size").fetchone()[0]
            if used > target_bytes:
                rows = db.execute("""SELECT r.request_id FROM debug_requests r WHERE r.status<>'running' AND
                                   ((SELECT count(*) FROM debug_events e WHERE e.request_id=r.request_id)=
                                    (SELECT max(sequence)+1 FROM debug_events e WHERE e.request_id=r.request_id)
                                    OR r.updated_at<?) ORDER BY r.updated_at LIMIT 100""", (time.time()-60,)).fetchall()
                db.executemany("DELETE FROM debug_requests WHERE request_id=?", ((row[0],) for row in rows))
                removed += len(rows)
            db.execute("""WITH RECURSIVE live(id) AS (
                          SELECT b.id FROM debug_event_bodies roots JOIN debug_bodies b ON b.key=roots.body_key
                          UNION SELECT e.child FROM debug_body_edges e JOIN live l ON e.parent=l.id)
                          DELETE FROM debug_bodies WHERE id NOT IN (SELECT id FROM live)""")
            db.execute("DELETE FROM debug_contexts WHERE NOT EXISTS (SELECT 1 FROM debug_request_contexts l JOIN debug_requests r ON r.request_id=l.request_id WHERE r.object_key=debug_contexts.object_key AND l.context_id=debug_contexts.context_id)")
            db.execute("DELETE FROM debug_objects WHERE NOT EXISTS (SELECT 1 FROM debug_contexts c WHERE c.object_key=debug_objects.object_key)")
        with self.connect(write=True) as db:
            db.execute("PRAGMA incremental_vacuum(512)")
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        wal = self.path.with_name(self.path.name + "-wal")
        return {"deleted_requests": removed, "database_bytes": self.path.stat().st_size,
                "wal_bytes": wal.stat().st_size if wal.exists() else 0}

    def stats(self) -> dict[str, int]:
        """
        分开报告实际文件, WAL 与有效页占用

        返回:
        - 有界空间统计, 不创建尚不存在的库
        """
        if not self.path.is_file():
            return {"database_bytes": 0, "wal_bytes": 0, "used_bytes": 0}
        with self.connect() as db:
            used = (db.execute("PRAGMA page_count").fetchone()[0] - db.execute("PRAGMA freelist_count").fetchone()[0]) * db.execute("PRAGMA page_size").fetchone()[0]
        wal = self.path.with_name(self.path.name + "-wal")
        return {"database_bytes": self.path.stat().st_size, "wal_bytes": wal.stat().st_size if wal.exists() else 0, "used_bytes": used}
