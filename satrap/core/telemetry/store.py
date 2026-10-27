"""
按服务独立存储轻量观测数据

后台写入器用短事务维护去重和汇总, 与平台业务库隔离, 只读查询不创建或迁移数据库
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import asdict
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
from typing import cast
import json
import time

from satrap.core.config_paths import get_config_path
from satrap.core.log.managed import LogFileLock, atomic_json
from .contracts import ActivityEvent, JsonValue, MessageMetric, MetricScope, ModelCall, Service, TelemetrySettings


COUNTERS = ("inbound", "outbound", "commands", "calls", "succeeded", "failed", "cancelled", "usage_reported_calls",
            "usage_missing_calls", "input_tokens", "output_tokens", "cached_tokens", "total_tokens",
            "input_tokens_reported", "output_tokens_reported", "cached_tokens_reported", "total_tokens_reported")
HISTOGRAM_MS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, 120000, 300000)
HISTOGRAM_KEYS = tuple(f"h{index}" for index in range(len(HISTOGRAM_MS) + 1))


def json_text(value: object) -> str:
    """
    编码内部已收窄的数据, 不序列化任意对象或非有限数值

    参数:
    - value: 已知的 JSON 容器

    返回:
    - 可比较和计算身份摘要的 UTF-8 JSON 文本
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class SettingsConflict(ValueError):
    """观测设置的读取版本已过期"""


class SettingsStore:
    """全局观测设置的原子读写, 热路径只使用写入器的内存快照"""

    def __init__(self, path: Path | None = None) -> None:
        """
        绑定受宿主控制的配置路径

        参数:
        - path: 测试或宿主指定位置, 默认集中配置目录
        """
        self.path = (path or get_config_path("telemetry.json")).resolve()

    def read(self) -> tuple[TelemetrySettings, str]:
        """
        读取完整设置和内容修订, 缺少文件时不写默认文件

        返回:
        - 已校验设置和内容摘要, 损坏时显式失败
        """
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return TelemetrySettings(), "missing"
        return TelemetrySettings.parse(json.loads(raw)), hashlib.sha256(raw).hexdigest()

    def save(self, value: object, expected_revision: object) -> dict[str, JsonValue]:
        """
        只在修订匹配时保存整体设置

        参数:
        - value: 完整设置
        - expected_revision: 客户端读取到的修订

        返回:
        - 保存后的设置和修订, 冲突时不写文件
        """
        settings = TelemetrySettings.parse(value)
        if not isinstance(expected_revision, str):
            raise ValueError("保存统计设置必须提供 expected_revision")
        lock = LogFileLock(self.path.with_suffix(".json.lock"))
        if not lock.acquire(1):
            raise TimeoutError("统计设置正在保存")
        try:
            if self.read()[1] != expected_revision:
                raise SettingsConflict("统计设置已变化, 请刷新后重试")
            atomic_json(self.path, settings.payload())
            saved, revision = self.read()
            return {"settings": saved.payload(), "revision": revision}
        finally:
            lock.release()


class TelemetryStore:
    """固定服务的独立 SQLite, 不共享平台业务库的 user_version"""

    def __init__(self, path: Path, service: Service) -> None:
        """
        绑定数据库和服务身份, 构造不访问磁盘

        参数:
        - path: 宿主解析的固定数据库路径
        - service: 当前服务
        """
        self.path = path.resolve()
        self.service: Service = service
        self._initialized = False

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """
        开启短连接, 只读连接不创建目录或迁移结构

        参数:
        - write: 仅允许后台写入器使用的写事务

        返回:
        - 当前连接, 退出时提交或回滚并关闭
        """
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=2)
        else:
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            if write:
                if not self._initialized:
                    self._initialize(db)
                    self._initialized = True
                with db:
                    yield db
            else:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                yield db
        finally:
            db.close()

    @staticmethod
    def _initialize(db: sqlite3.Connection) -> None:
        """
        初始化独立表和时间索引, 未知版本拒绝写入

        参数:
        - db: 写入器连接
        """
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise ValueError("统计数据库结构版本不支持")
        if version == 0:
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS model_calls (
              call_id TEXT PRIMARY KEY, runtime_id TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL NOT NULL,
              duration_ms REAL NOT NULL, status TEXT NOT NULL, scope_json TEXT NOT NULL, model TEXT NOT NULL,
              provider TEXT NOT NULL, usage_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS metric_call_time ON model_calls(started_at,call_id);
            CREATE INDEX IF NOT EXISTS metric_call_session ON model_calls(json_extract(scope_json,'$.platform_id'),json_extract(scope_json,'$.session_id'),started_at DESC);
            CREATE TABLE IF NOT EXISTS message_events (
              message_key TEXT PRIMARY KEY, occurred_at REAL NOT NULL, direction TEXT NOT NULL, command INTEGER NOT NULL,
              scope_json TEXT NOT NULL, runtime_id TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS metric_message_time ON message_events(occurred_at,message_key);
            CREATE TABLE IF NOT EXISTS metric_buckets (
              dimension_key TEXT NOT NULL, granularity INTEGER NOT NULL, bucket INTEGER NOT NULL,
              dimensions_json TEXT NOT NULL, counters_json TEXT NOT NULL,
              PRIMARY KEY(dimension_key,granularity,bucket)) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS metric_bucket_time ON metric_buckets(granularity,bucket);
            CREATE TABLE IF NOT EXISTS activity_events (
              event_id TEXT PRIMARY KEY, occurred_at REAL NOT NULL, kind TEXT NOT NULL, tone TEXT NOT NULL,
              title TEXT NOT NULL, scope_json TEXT NOT NULL, ref_json TEXT);
            CREATE INDEX IF NOT EXISTS metric_activity_time ON activity_events(occurred_at DESC,event_id);
            CREATE TABLE IF NOT EXISTS collection_runs (
              runtime_id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started_at REAL NOT NULL, as_of REAL NOT NULL,
              ended_at REAL, closed INTEGER NOT NULL DEFAULT 0, dropped_events INTEGER NOT NULL DEFAULT 0,
              gaps_json TEXT NOT NULL DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS context_occupancy (
              platform_id TEXT NOT NULL, agent TEXT NOT NULL, session_id TEXT NOT NULL,
              as_of REAL NOT NULL, used INTEGER, window INTEGER, model_config TEXT,
              PRIMARY KEY(platform_id,agent,session_id)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS retention_bounds (
              dataset TEXT PRIMARY KEY, retained_from REAL NOT NULL, reason_code TEXT NOT NULL);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(activity_events)")}
        if "event_count" not in columns:
            db.execute("ALTER TABLE activity_events ADD COLUMN event_count INTEGER NOT NULL DEFAULT 1")
        db.execute("PRAGMA user_version=2")

    def begin_run(self, runtime_id: str, pid: int, started_at: float) -> None:
        """
        登记当前写入器, 既有异常退出记录不改写为正常结束

        参数:
        - runtime_id: 本次服务身份
        - pid: 实际进程号
        - started_at: 服务启动时间
        """
        with self.connect(write=True) as db:
            db.execute("INSERT OR IGNORE INTO collection_runs(runtime_id,pid,started_at,as_of) VALUES(?,?,?,?)",
                       (runtime_id, pid, started_at, started_at))

    @staticmethod
    def _dimensions(scope: MetricScope, model: str = "", provider: str = "") -> dict[str, str]:
        """
        只保存有界聚合维度, 排除每个请求都不同的身份

        参数:
        - scope: 可信调用范围
        - model: 实际模型
        - provider: 提供方身份

        返回:
        - 用于复合聚合的固定字段
        """
        return {**{key: value for key, value in asdict(scope).items() if key not in {"request_id", "context_id"}},
                "model": model, "provider": provider}

    @staticmethod
    def _bump(db: sqlite3.Connection, dimensions: dict[str, str], occurred_at: float, counts: dict[str, int]) -> None:
        """
        在同一事务内更新分钟和日汇总, 允许方向纠正的负增量

        参数:
        - db: 写入器事务
        - dimensions: 固定聚合维度
        - occurred_at: 原始事件时间
        - counts: 本次计数增量
        """
        text = json_text(dimensions)
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        for granularity in (60, 86400):
            bucket = int(occurred_at // granularity) * granularity
            row = db.execute("SELECT counters_json FROM metric_buckets WHERE dimension_key=? AND granularity=? AND bucket=?", (key, granularity, bucket)).fetchone()
            counters = cast(dict[str, int], json.loads(row[0])) if row else {}
            for name, delta in counts.items():
                counters[name] = counters.get(name, 0) + delta
            db.execute("INSERT INTO metric_buckets VALUES(?,?,?,?,?) ON CONFLICT(dimension_key,granularity,bucket) DO UPDATE SET counters_json=excluded.counters_json",
                       (key, granularity, bucket, text, json_text(counters)))

    def append(self, runtime_id: str, events: Sequence[ModelCall | MessageMetric | ActivityEvent], *,
               dropped: int = 0, gaps: Sequence[dict[str, JsonValue]] = ()) -> None:
        """
        批量提交计数和明细, 去重与汇总共用一个短事务

        参数:
        - runtime_id: 当前服务身份
        - events: 队列取出的至多 128 项
        - dropped: 本进程累计未采集项数
        - gaps: 当前有界缺口快照
        """
        if len(events) > 128:
            raise ValueError("统计单批写入超过 128 条")
        with self.connect(write=True) as db:
            for event in events:
                if isinstance(event, ModelCall):
                    usage = {name: getattr(event, name) for name in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens")}
                    changed = db.execute("INSERT OR IGNORE INTO model_calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                                         (event.call_id, runtime_id, event.started_at, event.finished_at, event.duration_ms, event.status,
                                          json_text(asdict(event.scope)), event.model, event.provider, json_text(usage))).rowcount
                    if changed:
                        reported = any(value is not None for value in usage.values())
                        counters = {"calls": 1, event.status: 1, "usage_reported_calls": int(reported), "usage_missing_calls": int(not reported),
                                    **{name: value for name, value in usage.items() if isinstance(value, int)},
                                    **{name + "_reported": int(isinstance(value, int)) for name, value in usage.items()},
                                    HISTOGRAM_KEYS[next((index for index, bound in enumerate(HISTOGRAM_MS) if event.duration_ms <= bound), len(HISTOGRAM_MS))]: 1}
                        self._bump(db, self._dimensions(event.scope, event.model, event.provider), event.started_at, counters)
                        if event.scope.session_id and event.scope.purpose == "conversation":
                            db.execute("INSERT INTO context_occupancy VALUES(?,?,?,?,?,?,?) ON CONFLICT(platform_id,agent,session_id) "
                                       "DO UPDATE SET as_of=excluded.as_of,used=excluded.used,window=excluded.window,model_config=excluded.model_config "
                                       "WHERE excluded.as_of>=context_occupancy.as_of",
                                       (event.scope.platform_id, event.scope.agent, event.scope.session_id, event.started_at,
                                        event.input_tokens, event.context_window, event.scope.model_config or None))
                elif isinstance(event, MessageMetric):
                    identity = [event.scope.platform_id, event.scope.self_id, event.scope.conversation_kind, event.scope.target_id, event.message_id]
                    key = hashlib.sha256(json_text(identity).encode("utf-8")).hexdigest()
                    old = db.execute("SELECT * FROM message_events WHERE message_key=?", (key,)).fetchone()
                    if old and old["direction"] == event.direction:
                        if event.command and not old["command"] and event.direction == "inbound":
                            old_scope = MetricScope(**json.loads(old["scope_json"]))
                            db.execute("UPDATE message_events SET command=1 WHERE message_key=?", (key,))
                            self._bump(db, self._dimensions(old_scope), old["occurred_at"], {"commands": 1})
                        continue
                    if old and old["direction"] == "outbound":
                        continue
                    occurred_at = old["occurred_at"] if old else event.occurred_at
                    if old:
                        old_scope = MetricScope(**json.loads(old["scope_json"]))
                        self._bump(db, self._dimensions(old_scope), occurred_at, {old["direction"]: -1, "commands": -old["command"]})
                    db.execute("INSERT INTO message_events VALUES(?,?,?,?,?,?) ON CONFLICT(message_key) DO UPDATE SET direction=excluded.direction,command=excluded.command,scope_json=excluded.scope_json",
                               (key, occurred_at, event.direction, int(event.command), json_text(asdict(event.scope)), runtime_id))
                    self._bump(db, self._dimensions(event.scope), occurred_at, {event.direction: 1, "commands": int(event.command)})
                else:
                    db.execute("INSERT INTO activity_events(event_id,occurred_at,kind,tone,title,scope_json,ref_json,event_count) VALUES(?,?,?,?,?,?,?,?) "
                               "ON CONFLICT(event_id) DO UPDATE SET event_count=activity_events.event_count+excluded.event_count,scope_json=excluded.scope_json,ref_json=excluded.ref_json "
                               "WHERE excluded.kind='session.completed'",
                               (event.event_id, event.occurred_at, event.kind, event.tone, event.title[:240],
                                json_text(asdict(event.scope)), json_text(event.reference) if event.reference else None, event.count))
            db.execute("UPDATE collection_runs SET as_of=?,dropped_events=?,gaps_json=? WHERE runtime_id=?", (time.time(), dropped, json_text(gaps), runtime_id))

    def finish_run(self, runtime_id: str, *, clean: bool) -> None:
        """
        只有队列成功排空时标记正常退出

        参数:
        - runtime_id: 当前写入器身份
        - clean: 是否确认没有未提交数据
        """
        with self.connect(write=True) as db:
            stamp = time.time()
            db.execute("UPDATE collection_runs SET ended_at=?,closed=?,as_of=CASE WHEN ? THEN ? ELSE as_of END WHERE runtime_id=?",
                       (stamp, int(clean), int(clean), stamp, runtime_id))

    def cleanup(self, settings: TelemetrySettings, *, now: float | None = None) -> dict[str, int]:
        """
        按保留期和空间预算回收, 容量提前清理会留下覆盖缺口

        参数:
        - settings: 当前生效设置
        - now: 测试可注入时间

        返回:
        - 删除项与当前文件占用
        """
        if not self.path.is_file():
            return {"deleted": 0, "database_bytes": 0, "wal_bytes": 0}
        stamp = time.time() if now is None else now
        removed = 0
        with self.connect(write=True) as db:
            for table, column, days in (("model_calls", "started_at", settings.usage_retention_days),
                                        ("message_events", "occurred_at", settings.usage_retention_days),
                                        ("activity_events", "occurred_at", settings.activity_retention_days),
                                        ("metric_buckets", "bucket", settings.aggregate_retention_days)):
                cutoff = stamp - days * 86400
                deleted = db.execute(f"DELETE FROM {table} WHERE {column}<?", (cutoff,)).rowcount
                removed += deleted
                if deleted:
                    db.execute("INSERT INTO retention_bounds VALUES(?,?,?) ON CONFLICT(dataset) DO UPDATE SET "
                               "retained_from=max(retained_from,excluded.retained_from),reason_code=excluded.reason_code",
                               (table, cutoff, "retention_expired"))
            db.execute("DELETE FROM context_occupancy WHERE as_of<?", (stamp - settings.aggregate_retention_days * 86400,))
            db.execute("DELETE FROM collection_runs WHERE ended_at<?", (stamp - settings.aggregate_retention_days * 86400,))
            for datasets in ((("model_calls", "started_at"), ("message_events", "occurred_at"), ("activity_events", "occurred_at")),
                             (("metric_buckets", "bucket"),)):
                for _ in range(32):
                    used = (db.execute("PRAGMA page_count").fetchone()[0] - db.execute("PRAGMA freelist_count").fetchone()[0]) * db.execute("PRAGMA page_size").fetchone()[0]
                    if used <= settings.max_service_mb * 1_000_000:
                        break
                    progress = 0
                    for table, column in datasets:
                        count = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                        if not count:
                            continue
                        cutoff_row = db.execute(f"SELECT {column} FROM {table} ORDER BY {column} LIMIT 1 OFFSET ?", (min(count - 1, max(1023, count // 10)),)).fetchone()
                        cutoff = float(cutoff_row[0])
                        progress += db.execute(f"DELETE FROM {table} WHERE {column}<=?", (cutoff,)).rowcount
                        db.execute("INSERT INTO retention_bounds VALUES(?,?,?) ON CONFLICT(dataset) DO UPDATE SET "
                                   "retained_from=max(retained_from,excluded.retained_from),reason_code=excluded.reason_code",
                                   (table, cutoff + (60 if table == "metric_buckets" else .000001), "storage_budget"))
                    removed += progress
                    if not progress:
                        break
        with self.connect(write=True) as db:
            db.execute("PRAGMA incremental_vacuum(8192)").fetchall()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        wal = self.path.with_name(self.path.name + "-wal")
        return {"deleted": removed, "database_bytes": self.path.stat().st_size, "wal_bytes": wal.stat().st_size if wal.exists() else 0}
