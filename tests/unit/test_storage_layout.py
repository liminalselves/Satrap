"""Satrap v2 平台与会话数据布局测试"""
import json
import sqlite3
from pathlib import Path

import pytest

from satrap.core.storage import (
    StorageLayout,
    StorageMaintenanceService,
    StorageScope,
    delete_session_domain_rows,
    storage_key,
)
from satrap.core.storage.database import restore_session_domain


def test_storage_key_is_stable_and_collision_resistant():
    """不同原始 ID 即使旧式替换结果相同也必须产生不同目录键"""
    assert storage_key("a:b", fallback="session") != storage_key("a_b", fallback="session")
    assert storage_key("a:b", fallback="session") == storage_key("a:b", fallback="session")


def test_platform_uses_one_database(tmp_path: Path):
    """
    同一平台的全部领域应解析到唯一 platform.db

    参数:
    - tmp_path: 临时目录
    """
    layout = StorageLayout(tmp_path / "data")

    assert layout.platform_db("onebot-main").name == "platform.db"
    assert layout.platform_db("onebot-main").parent == layout.platform_root("onebot-main")
    assert layout.platform_db("onebot-main") != layout.platform_db("onebot-backup")


def test_ensure_session_creates_isolated_directories(tmp_path: Path):
    """
    每个会话必须拥有独立 sandbox、uploads、artifacts、indexes 和 cache

    参数:
    - tmp_path: 临时目录
    """
    layout = StorageLayout(tmp_path / "data")
    first = StorageScope("chat", user_id="local", session_id="conv:1", project_id="p1")
    second = StorageScope("chat", user_id="local", session_id="conv_1", project_id="p1")

    first_root = layout.ensure_session(first)
    second_root = layout.ensure_session(second)

    assert first_root != second_root
    for name in ("sandbox", "uploads", "artifacts", "indexes", "cache"):
        assert (first_root / name).is_dir()
        assert (second_root / name).is_dir()
    assert json.loads((first_root / "meta.json").read_text(encoding="utf-8"))["session_id"] == "conv:1"


def test_user_manifest_and_session_trash_lifecycle(tmp_path: Path):
    """
    用户身份单独落盘, 删除会话时整体移入同平台回收区

    参数:
    - tmp_path: 临时目录
    """
    layout = StorageLayout(tmp_path / "data")
    scope = StorageScope("onebot-main", user_id="10001", session_id="session:1")
    user_root = layout.ensure_user(scope)
    session_root = layout.ensure_session(scope)
    (session_root / "sandbox" / "result.txt").write_text("data", encoding="utf-8")

    trashed = layout.trash_session(scope.platform_id, scope.session_id)

    assert json.loads((user_root / "meta.json").read_text(encoding="utf-8"))["user_id"] == "10001"
    assert trashed is not None and trashed.is_dir()
    assert (trashed / "sandbox" / "result.txt").read_text(encoding="utf-8") == "data"
    assert not session_root.exists()


def test_delete_session_domain_rows_preserves_other_sessions(tmp_path: Path):
    """
    会话级联清理只删除目标会话的上下文和记忆

    参数:
    - tmp_path: 临时目录
    """
    database = tmp_path / "platform.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE chat_history (conversation_id TEXT, content TEXT)")
        connection.execute("CREATE TABLE context_runtime_state (conversation_id TEXT, summary TEXT)")
        connection.execute("CREATE TABLE memories (scope TEXT, content TEXT)")
        connection.executemany(
            "INSERT INTO chat_history VALUES (?, ?)",
            [("first", "a"), ("second", "b")],
        )
        connection.executemany(
            "INSERT INTO context_runtime_state VALUES (?, ?)",
            [("first", "摘要 a"), ("second", "摘要 b")],
        )
        connection.executemany(
            "INSERT INTO memories VALUES (?, ?)",
            [("session:first", "a"), ("session:second", "b")],
        )
        connection.commit()

    delete_session_domain_rows(database, "first")

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT conversation_id FROM chat_history").fetchall() == [("second",)]
        assert connection.execute("SELECT conversation_id FROM context_runtime_state").fetchall() == [("second",)]
        assert connection.execute("SELECT scope FROM memories").fetchall() == [("session:second",)]


def test_storage_maintenance_scans_orphans_without_modifying_data(tmp_path: Path):
    """
    维护扫描应区分孤儿目录和孤儿数据库行且保持只读

    参数:
    - tmp_path: 临时目录
    """
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("onebot-main")
    orphan_root = layout.ensure_session(
        StorageScope("onebot-main", session_id="orphan-files")
    )
    database = layout.platform_db("onebot-main")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE session_configs (session_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TABLE chat_history (conversation_id TEXT, content TEXT)"
        )
        connection.execute(
            "INSERT INTO chat_history VALUES (?, ?)",
            ("orphan-rows", "data"),
        )
        connection.commit()

    service = StorageMaintenanceService(layout)
    items = service.scan({"onebot-main"})

    assert any(
        item.category == "orphan_session_directory"
        and item.session_id == "orphan-files"
        for item in items
    )
    assert any(
        item.category == "orphan_database_rows"
        and item.session_id == "orphan-rows"
        for item in items
    )
    assert orphan_root.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0] == 1


def test_recoverable_session_archive_roundtrip(tmp_path: Path):
    """
    完整回收包应恢复会话配置、上下文和私有文件

    参数:
    - tmp_path: 临时目录
    """
    layout = StorageLayout(tmp_path / "data")
    session_id = "session:restore"
    root = layout.ensure_session(StorageScope("chat", session_id=session_id))
    (root / "sandbox" / "answer.txt").write_text("42", encoding="utf-8")
    database = layout.platform_db("chat")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE session_configs ("
            "session_id TEXT PRIMARY KEY, session_type_name TEXT, provider_name TEXT, "
            "created_at REAL, last_used_at REAL, message_count INTEGER, session_config TEXT)"
        )
        connection.execute(
            "CREATE TABLE chat_history ("
            "id INTEGER PRIMARY KEY, conversation_id TEXT, role TEXT, content TEXT)"
        )
        connection.execute(
            "CREATE TABLE conversation_meta ("
            "conversation_id TEXT PRIMARY KEY, model TEXT, think TEXT, project_id TEXT, created_at REAL)"
        )
        connection.execute(
            "CREATE TABLE display_turns ("
            "id INTEGER PRIMARY KEY, conversation_id TEXT, turn_index INTEGER, "
            "user_input TEXT, thinking TEXT, answer TEXT, attachments TEXT, segments TEXT, "
            "created_at REAL, active_variant INTEGER)"
        )
        connection.execute(
            "CREATE TABLE display_tool_calls ("
            "id INTEGER PRIMARY KEY, turn_id INTEGER, variant_index INTEGER, seq INTEGER, name TEXT, "
            "arguments TEXT, success INTEGER, call_id TEXT, created_at REAL)"
        )
        connection.execute(
            "CREATE TABLE display_turn_variants ("
            "id INTEGER PRIMARY KEY, turn_id INTEGER, variant_index INTEGER, thinking TEXT, "
            "answer TEXT, segments TEXT, context_messages TEXT, created_at REAL)"
        )
        connection.execute(
            "INSERT INTO session_configs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, "assistant", "edictum", 1.0, 2.0, 1, "{}"),
        )
        connection.execute(
            "INSERT INTO chat_history VALUES (?, ?, ?, ?)",
            (1, session_id, "user", "hello"),
        )
        connection.execute(
            "INSERT INTO chat_history VALUES (?, ?, ?, ?)",
            (2, f"{session_id}_main", "assistant", "workflow"),
        )
        connection.execute(
            "INSERT INTO conversation_meta VALUES (?, ?, ?, ?, ?)",
            (session_id, "default", "off", None, 1.0),
        )
        connection.execute(
            "INSERT INTO display_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (10, session_id, 0, "hello", None, "world", None, None, 2.0, 0),
        )
        connection.execute(
            "INSERT INTO display_tool_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (20, 10, 0, 0, "search", "{}", 1, "call-1", 2.0),
        )
        connection.execute(
            "INSERT INTO display_turn_variants VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                30,
                10,
                0,
                None,
                "world",
                None,
                '[{"role":"user","content":"hello"},{"role":"assistant","content":"world"}]',
                2.0,
            ),
        )
        connection.commit()

    service = StorageMaintenanceService(layout)
    archived = service.archive_session("chat", session_id)

    assert not root.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM session_configs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversation_meta").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM display_turns").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM display_tool_calls").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM display_turn_variants").fetchone()[0] == 0

    restored = service.restore_archive("chat", str(archived["archive_id"]))

    assert restored["ok"] is True
    assert (root / "sandbox" / "answer.txt").read_text(encoding="utf-8") == "42"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT session_id FROM session_configs").fetchone()[0] == session_id
        assert connection.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0] == 2
        assert connection.execute("SELECT user_input FROM display_turns").fetchone()[0] == "hello"
        assert connection.execute("SELECT name FROM display_tool_calls").fetchone()[0] == "search"
        assert connection.execute("SELECT answer FROM display_turn_variants").fetchone()[0] == "world"


def test_restore_session_domain_rejects_unknown_archive_columns(tmp_path: Path):
    """
    恢复归档时应拒绝不属于目标架构的列名

    参数:
    - tmp_path: 临时目录
    """
    database = tmp_path / "platform.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE session_configs (session_id TEXT PRIMARY KEY, session_config TEXT)"
        )

    records = {
        "session_configs": [{
            "session_id": "malicious",
            "session_config) VALUES ('x'); DROP TABLE session_configs; --": "payload",
        }],
    }
    with pytest.raises(ValueError, match="未知列"):
        restore_session_domain(database, "malicious", records)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_configs'"
        ).fetchone() is not None
        assert connection.execute("SELECT COUNT(*) FROM session_configs").fetchone()[0] == 0


def test_restore_archive_rejects_path_traversal(tmp_path: Path):
    """
    恢复回收包时不得读取或删除回收目录外的路径

    参数:
    - tmp_path: 临时目录
    """
    layout = StorageLayout(tmp_path / "data")
    service = StorageMaintenanceService(layout)
    sessions_root = layout.trash_root("chat") / "sessions"
    outside = sessions_root.parent / "outside"
    outside.mkdir(parents=True)
    (outside / "marker.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="非法回收包路径"):
        service.restore_archive("chat", "../outside")
    with pytest.raises(ValueError, match="非法回收包路径"):
        service.restore_archive("chat", str(outside.resolve()))

    assert (outside / "marker.txt").read_text(encoding="utf-8") == "keep"


def test_restore_archive_rejects_symlink_package(tmp_path: Path):
    """
    恢复回收包时不得跟随指向回收目录外的符号链接

    参数:
    - tmp_path: 临时目录
    """
    layout = StorageLayout(tmp_path / "data")
    service = StorageMaintenanceService(layout)
    sessions_root = layout.trash_root("chat") / "sessions"
    sessions_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    archive_link = sessions_root / "linked"
    try:
        archive_link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"当前环境无法创建目录符号链接: {error}")

    with pytest.raises(ValueError, match="符号链接回收包"):
        service.restore_archive("chat", "linked")


def test_storage_maintenance_batch_purges_selected_and_expired_archives(tmp_path: Path):
    """
    回收区应支持显式批量删除和按保留期清理

    参数:
    - tmp_path: 临时目录
    """
    layout = StorageLayout(tmp_path / "data")
    service = StorageMaintenanceService(layout)
    archives: list[dict[str, object]] = []
    for session_id in ("selected", "expired"):
        root = layout.ensure_session(StorageScope("onebot-main", session_id=session_id))
        (root / "sandbox" / "data.txt").write_text(session_id, encoding="utf-8")
        database = layout.platform_db("onebot-main")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS session_configs ("
                "session_id TEXT PRIMARY KEY, session_type_name TEXT, provider_name TEXT, "
                "created_at REAL, last_used_at REAL, message_count INTEGER, session_config TEXT)"
            )
            connection.execute(
                "INSERT INTO session_configs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, "assistant", "edictum", 1.0, 2.0, 0, "{}"),
            )
            connection.commit()
        archives.append(service.archive_session("onebot-main", session_id))

    selected_result = service.purge_archives(archive_refs=[{
        "platform_id": "onebot-main",
        "archive_id": str(archives[0]["archive_id"]),
    }])
    expired_result = service.purge_archives(older_than_days=0)

    assert selected_result[0]["ok"] is True
    assert expired_result[0]["ok"] is True
    assert not list((layout.trash_root("onebot-main") / "sessions").iterdir())
