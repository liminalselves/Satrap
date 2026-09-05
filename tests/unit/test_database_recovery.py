"""向量源数据, 缓存故障和旧格式迁移回归"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import subprocess
from pathlib import Path
import sqlite3
import pytest
import faiss
import numpy as np
import json
import sys

from satrap.core.database import DataBase, VectorDataUnavailable


def test_sql_failure_rolls_back_entire_vector_batch(tmp_path):
    db = DataBase(str(tmp_path))
    db.add_to_collection("a", ["before"], [[1, 0]], None)
    with db._connect() as conn:
        revision = conn.execute("SELECT revision FROM collections").fetchone()[0]
        conn.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON documents WHEN NEW.document='fail' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        db.add_to_collection("a", ["partial", "fail"], [[0, 1], [0, 1]], None)
    with db._connect() as conn:
        assert conn.execute("SELECT revision FROM collections").fetchone()[0] == revision
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert DataBase(str(tmp_path)).search("a", [1, 0])[0]["document"] == "before"


def test_partial_index_write_keeps_committed_vectors(tmp_path, monkeypatch):
    db = DataBase(str(tmp_path))
    db.add_to_collection("a", ["before"], [[1, 0]], None)
    assert Path(db._index_path("a")).is_file()

    def fail_write(index, path):
        Path(path).write_bytes(b"partial")
        raise OSError("injected cache failure")

    monkeypatch.setattr(db.faiss, "write_index", fail_write)
    assert db.add_to_collection("a", ["after"], [[0, 1]], None) == 1
    assert DataBase(str(tmp_path)).search("a", [0, 1])[0]["document"] == "after"
    assert not list(tmp_path.glob(".faiss-build-*"))


def test_missing_or_corrupt_cache_rebuilds_from_sqlite(tmp_path):
    db = DataBase(str(tmp_path))
    db.add_to_collection("a", ["before"], [[1, 0]], None)
    index_path = Path(db._index_path("a"))
    index_path.write_bytes(b"invalid index")
    assert DataBase(str(tmp_path)).search("a", [1, 0])[0]["document"] == "before"
    index_path.unlink()
    assert DataBase(str(tmp_path)).search("a", [1, 0])[0]["document"] == "before"


def test_process_exit_after_sql_commit_recovers(tmp_path):
    script = """
import logging, os, sys
with patch('logging.FileHandler', lambda *a, **k: logging.NullHandler()):
    from satrap.core.database import DataBase
db = DataBase(sys.argv[1])
db._save_index = lambda name: os._exit(77)
db.add_to_collection('a', ['durable'], [[1, 0]], None)
"""
    child = subprocess.run([sys.executable, "-B", "-c", script, str(tmp_path)], capture_output=True, encoding="utf-8", timeout=30)
    assert child.returncode == 77, child.stderr
    assert DataBase(str(tmp_path)).search("a", [1, 0])[0]["document"] == "durable"


def test_instances_and_colliding_names_keep_independent_data(tmp_path):
    first = DataBase(str(tmp_path))
    second = DataBase(str(tmp_path))
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(db.add_to_collection, "a/b", [str(i)], [[1, 0]], None) for i, db in enumerate([first, second] * 4)]
        assert all(future.result() == 1 for future in futures)
    second.add_to_collection("a_b", ["other"], [[0, 1]], None)
    assert len(first.search("a/b", [1, 0], k=20)) == 8
    first.delete_collection("a/b")
    assert second.search("a/b", [1, 0]) == []
    assert DataBase(str(tmp_path)).search("a_b", [0, 1])[0]["document"] == "other"


def test_cross_process_writers_and_stale_cache_publish(tmp_path):
    first = DataBase(str(tmp_path))
    first.add_to_collection("a", ["before"], [[1, 0]], None)
    script = """
import logging, sys
with patch('logging.FileHandler', lambda *a, **k: logging.NullHandler()):
    from satrap.core.database import DataBase
db = DataBase(sys.argv[1])
db.add_to_collection('a', [sys.argv[2]], [[1, 0]], None)
"""
    children = [subprocess.Popen([sys.executable, "-B", "-c", script, str(tmp_path), str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") for i in range(2)]
    try:
        for child in children:
            _, errors = child.communicate(timeout=30)
            assert child.returncode == 0, errors
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate()
    first._save_index("a")
    reloaded = DataBase(str(tmp_path))
    assert {row["document"] for row in reloaded.search("a", [1, 0], k=10)} == {"before", "0", "1"}


def test_document_deletion_invalidates_other_instance_cache(tmp_path, monkeypatch):
    first = DataBase(str(tmp_path))
    first.add_to_collection("a", ["keep", "delete"], [[1, 0], [1, 0]], None)
    second = DataBase(str(tmp_path))
    assert len(first.search("a", [1, 0])) == 2
    with second._connect() as conn:
        doc_id = conn.execute("SELECT id FROM documents WHERE document='delete'").fetchone()[0]
    monkeypatch.setattr(second, "_save_index", lambda name: (_ for _ in ()).throw(OSError("cache failure")))
    assert second.delete_documents("a", [doc_id]) == 1
    assert [row["document"] for row in first.search("a", [1, 0])] == ["keep"]
    assert DataBase(str(tmp_path)).search("a", [1, 0])[0]["document"] == "keep"


def legacy_database(root, collision=False):
    with closing(sqlite3.connect(root / "metadata.sqlite")) as conn, conn:
        conn.execute("CREATE TABLE collections (name TEXT PRIMARY KEY, dim INTEGER NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, collection_name TEXT NOT NULL, document TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}')")
        for doc_id, name, vector in [(1, "a/b", [1, 0]), (2, "a_b" if collision else "second", [0, 1])]:
            conn.execute("INSERT INTO collections VALUES (?, 2)", (name,))
            conn.execute("INSERT INTO documents VALUES (?, ?, ?, '{}')", (doc_id, name, name))
            index = faiss.IndexIDMap2(faiss.IndexFlatIP(2))
            index.add_with_ids(np.asarray([vector], dtype=np.float32), np.asarray([doc_id], dtype=np.int64))
            faiss.write_index(index, str(root / (name.replace("/", "_") + ".faiss")))


@pytest.mark.parametrize("collision", [False, True])
def test_legacy_migration_preserves_backup_and_reports_missing_vectors(tmp_path, collision):
    legacy_database(tmp_path, collision)
    before = {file.name: file.read_bytes() for file in tmp_path.glob("*.faiss")}
    with pytest.raises(RuntimeError, match="维护迁移"):
        DataBase(str(tmp_path))
    report = DataBase.migrate_legacy(str(tmp_path))
    assert report["missing_document_ids"] == ([1] if collision else [])
    backup = Path(report["backup"])
    assert json.loads((backup / "migration-report.json").read_text(encoding="utf-8")) == report
    with closing(sqlite3.connect(backup / "metadata.sqlite")) as conn:
        assert "vector" not in {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    assert {file.name: file.read_bytes() for file in tmp_path.glob("*.faiss")} == before
    db = DataBase(str(tmp_path))
    if collision:
        with pytest.raises(VectorDataUnavailable):
            db.search("a/b", [1, 0])
        assert db.missing_vector_ids("a/b") == [1]
        db.repair_missing_vectors("a/b", [1], [[1, 0]])
    assert DataBase(str(tmp_path)).search("a/b", [1, 0])[0]["document"] == "a/b"
    assert DataBase.migrate_legacy(str(tmp_path))["already_migrated"]


def test_migration_interruption_rolls_back_schema(tmp_path, monkeypatch):
    legacy_database(tmp_path)
    import satrap.core.database as module
    original_uuid = module.uuid.uuid4
    calls = 0

    def interrupted():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("injected interruption")
        return original_uuid()

    with monkeypatch.context() as scoped:
        scoped.setattr(module.uuid, "uuid4", interrupted)
        with pytest.raises(RuntimeError, match="interruption"):
            DataBase.migrate_legacy(str(tmp_path))
    with closing(sqlite3.connect(tmp_path / "metadata.sqlite")) as conn:
        assert "collection_id" not in {row[1] for row in conn.execute("PRAGMA table_info(collections)")}
    assert not DataBase.migrate_legacy(str(tmp_path))["missing_document_ids"]


def test_failed_index_write_must_not_leave_invisible_documents(tmp_path, monkeypatch):
    db = DataBase(str(tmp_path / "vector"))
    db.add_to_collection("audit", ["before"], [[1.0, 0.0]], None)

    def fail_save(name):
        raise OSError("audit: simulated index write failure")

    monkeypatch.setattr(db, "_save_index", fail_save)
    assert db.add_to_collection("audit", ["after"], [[0.0, 1.0]], None) == 1
    reloaded = DataBase(str(tmp_path / "vector"))
    with sqlite3.connect(reloaded.sqlite_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM documents WHERE document='after'").fetchone()[0]
    conn.close()
    found = reloaded.search("audit", [0.0, 1.0], threshold=0.9)
    assert count == 1 and any(row["document"] == "after" for row in found), f"committed={count}, search={found}"


def test_collection_names_must_survive_restart(tmp_path):
    db = DataBase(str(tmp_path / "vector"))
    db.add_to_collection("a/b", ["first"], [[1.0, 0.0]], None)
    db.add_to_collection("a_b", ["second"], [[0.0, 1.0]], None)
    reloaded = DataBase(str(tmp_path / "vector"))
    assert any(r["document"] == "first" for r in reloaded.search("a/b", [1.0, 0.0])), "重启后首个集合不可检索"
