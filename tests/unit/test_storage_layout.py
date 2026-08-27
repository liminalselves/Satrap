"""Satrap v2 平台与会话数据布局测试"""
import json
import sqlite3
from pathlib import Path

from satrap.core.storage import (
    StorageLayout,
    StorageScope,
    delete_session_domain_rows,
    storage_key,
)


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
        connection.execute("CREATE TABLE memories (scope TEXT, content TEXT)")
        connection.executemany(
            "INSERT INTO chat_history VALUES (?, ?)",
            [("first", "a"), ("second", "b")],
        )
        connection.executemany(
            "INSERT INTO memories VALUES (?, ?)",
            [("session:first", "a"), ("session:second", "b")],
        )
        connection.commit()

    delete_session_domain_rows(database, "first")

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT conversation_id FROM chat_history").fetchall() == [("second",)]
        assert connection.execute("SELECT scope FROM memories").fetchall() == [("session:second",)]
