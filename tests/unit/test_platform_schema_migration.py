"""平台数据库结构迁移的并发水位回归测试"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import sqlite3
import threading

from satrap.core.config.platform_schema import PLATFORM_SCHEMA_VERSION, ensure_platform_tables
from satrap.core.config.session_overrides import SessionOverrideStore


_ACTION_COLUMNS = (
    "action_revision", "decision", "decided_by_json", "decision_note", "risk", "risk_source",
)


def _make_v11_database(path: Path) -> None:
    """先建立完整结构再移除 v12 增量, 得到可重复迁移的 v11 数据库"""
    with closing(sqlite3.connect(path)) as connection, connection:
        ensure_platform_tables(connection)
        for table in ("group_actions", "friend_actions", "memory_proposals"):
            connection.execute(f"DROP TRIGGER IF EXISTS {table}_action_revision")
            for column in _ACTION_COLUMNS:
                connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        connection.execute("PRAGMA user_version = 11")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11


def _run_stale_race(
    path: Path,
    target: Callable[[threading.Event], None],
    *,
    started: threading.Event | None = None,
) -> None:
    """持锁连接完成迁移但不提交, 目标连接读取旧水位后再竞争写锁"""
    begin_started = started or threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    holder = sqlite3.connect(path, timeout=10)
    try:
        holder.execute("BEGIN IMMEDIATE")
        ensure_platform_tables(holder)
        assert holder.execute("PRAGMA user_version").fetchone()[0] == PLATFORM_SCHEMA_VERSION

        def worker() -> None:
            try:
                target(begin_started)
            except BaseException as error:   # 记录线程内失败, 交给主线程断言
                errors.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=worker)
        thread.start()
        assert begin_started.wait(10), "目标连接未进入 BEGIN IMMEDIATE"
        holder.commit()
        assert finished.wait(10), "目标连接等待迁移超时"
        thread.join(10)
        assert not thread.is_alive()
    finally:
        holder.close()

    if errors:
        raise errors[0]


def test_ensure_platform_tables_rereads_version_after_write_lock(tmp_path: Path):
    """直接入口在获得写锁后重读水位, 不复用竞争前的 v11 迁移决定"""
    path = tmp_path / "platform.db"
    _make_v11_database(path)

    def run(started: threading.Event) -> None:
        connection = sqlite3.connect(path, timeout=10)
        connection.set_trace_callback(
            lambda statement: started.set() if statement.strip().upper().startswith("BEGIN IMMEDIATE") else None
        )
        try:
            ensure_platform_tables(connection)
            connection.commit()
        finally:
            connection.close()

    _run_stale_race(path, run)

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == PLATFORM_SCHEMA_VERSION
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(group_actions)")}
        assert set(_ACTION_COLUMNS) <= columns


def test_session_override_store_rereads_version_after_write_lock(tmp_path: Path, monkeypatch):
    """SessionOverrideStore 包装入口同样在写锁后重读水位并跳过重复迁移"""
    path = tmp_path / "platform.db"
    _make_v11_database(path)
    started = threading.Event()
    original_connect = SessionOverrideStore._connect

    def traced_connect(self: SessionOverrideStore) -> sqlite3.Connection:
        connection = original_connect(self)
        connection.set_trace_callback(
            lambda statement: started.set() if statement.strip().upper().startswith("BEGIN IMMEDIATE") else None
        )
        return connection

    monkeypatch.setattr(SessionOverrideStore, "_connect", traced_connect)

    def run(_: threading.Event) -> None:
        SessionOverrideStore(path)

    _run_stale_race(path, run, started=started)

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == PLATFORM_SCHEMA_VERSION
    store = SessionOverrideStore(path)
    assert store.read("session", "plugins.example")["revision"] == 0
