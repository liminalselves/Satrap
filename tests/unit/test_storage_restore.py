"""归档结构校验, 文件发布故障和幂等清理回归"""
from contextlib import closing
from pathlib import Path
import sqlite3
import pytest
import json

from satrap.core.storage.maintenance import StorageMaintenanceService
from satrap.core.storage.layout import StorageLayout


@pytest.fixture
def archive_case(tmp_path):
    layout = StorageLayout(tmp_path / "data")
    database = layout.platform_db("audit")
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute("CREATE TABLE conversation_meta (conversation_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO conversation_meta VALUES ('conversation')")
    root = layout.session_root("audit", "conversation")
    root.mkdir(parents=True)
    (root / "payload.txt").write_text("待恢复内容", encoding="utf-8")
    service = StorageMaintenanceService(layout)
    manifest = service.archive_session("audit", "conversation")
    archive = layout.trash_root("audit") / "sessions" / manifest["archive_id"]
    return service, database, root, archive, manifest


@pytest.mark.parametrize("damaged", ["{invalid", "[]"], ids=["invalid-json", "wrong-root-type"])
def test_restore_must_preserve_invalid_archive(tmp_path, damaged):
    layout = StorageLayout(tmp_path / "data")
    database = layout.platform_db("audit")
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE conversation_meta (conversation_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO conversation_meta VALUES ('conversation')")
    conn.close()
    service = StorageMaintenanceService(layout)
    manifest = service.archive_session("audit", "conversation")
    archive = layout.trash_root("audit") / "sessions" / manifest["archive_id"]
    (archive / "records.json").write_text(damaged, encoding="utf-8")
    result = None
    try:
        result = service.restore_archive("audit", manifest["archive_id"])
    except (ValueError, OSError):
        pass
    assert archive.exists(), f"无效归档被删除; restore_result={result}"
    assert not result or not result.get("ok"), "无效归档不应返回成功"


@pytest.mark.parametrize("damage", ["missing", "row", "tables", "identity", "digest", "files", "version"])
def test_invalid_archive_preserves_files_and_database(archive_case, damage):
    service, database, root, archive, manifest = archive_case
    records_path = archive / "records.json"
    records = json.loads(records_path.read_text(encoding="utf-8"))
    if damage == "missing":
        records_path.unlink()
    elif damage == "files":
        (archive / "files").rename(archive / "saved-files")
    elif damage == "version":
        manifest["archive_version"] = 99
    elif damage == "tables":
        manifest["tables"] = []
    elif damage == "digest":
        records_path.write_text(json.dumps(records) + " ", encoding="utf-8")
    else:
        manifest["archive_version"] = 1   # 老归档没有摘要, 仍需逐行校验
        records["conversation_meta"] = [False] if damage == "row" else [{"conversation_id": "other"}]
        records_path.write_text(json.dumps(records), encoding="utf-8")
    (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        service.restore_archive("audit", manifest["archive_id"])
    assert archive.exists() and not root.exists()
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversation_meta").fetchone()[0] == 0


def test_restore_retries_after_file_publish_failure(archive_case, monkeypatch):
    service, database, root, archive, manifest = archive_case
    original_rename = Path.rename

    def fail_publish(path, target):
        if path == archive / "files":
            raise OSError("simulated publish failure")
        return original_rename(path, target)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "rename", fail_publish)
        with pytest.raises(OSError, match="publish"):
            service.restore_archive("audit", manifest["archive_id"])
    assert (archive / "files" / "payload.txt").exists() and not root.exists()
    result = service.restore_archive("audit", manifest["archive_id"])
    assert result["ok"] and not result["cleanup_pending"]
    assert (root / "payload.txt").read_text(encoding="utf-8") == "待恢复内容"
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversation_meta").fetchone()[0] == 1


def test_restore_retries_partial_cleanup_without_records(archive_case, monkeypatch):
    service, database, root, archive, manifest = archive_case

    def fail_cleanup(path):
        (Path(path) / "records.json").unlink()
        (Path(path) / "manifest.json").unlink()
        raise OSError("simulated cleanup failure")

    with monkeypatch.context() as scoped:
        scoped.setattr("satrap.core.storage.maintenance.shutil.rmtree", fail_cleanup)
        result = service.restore_archive("audit", manifest["archive_id"])
    assert result["ok"] and result["cleanup_pending"]
    listed = service.list_archives("audit")
    assert listed[0]["restorable"] and listed[0]["cleanup_pending"]
    assert service.restore_archive("audit", manifest["archive_id"])["ok"]
    assert root.exists() and not archive.exists()
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversation_meta").fetchone()[0] == 1


def test_files_only_archive_is_valid(tmp_path):
    layout = StorageLayout(tmp_path / "data")
    root = layout.session_root("audit", "files-only")
    root.mkdir(parents=True)
    (root / "payload").write_text("files", encoding="utf-8")
    service = StorageMaintenanceService(layout)
    manifest = service.archive_session("audit", "files-only")
    assert manifest["tables"] == []
    assert service.restore_archive("audit", manifest["archive_id"])["ok"]
    assert (root / "payload").read_text(encoding="utf-8") == "files"
