"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
import pytest
from typing import Any
from satrap.core.database import DataBase, LiteVectorDB
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import subprocess
import sqlite3
import faiss
import numpy as np
import json
import sys
from satrap.core.database import VectorDataUnavailable
import satrap.core.database as module
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import cast
from satrap.core.pipeline.manual_wake_store import MANIFEST_SPEC as STORE_SPEC, ManualWakeStore
from satrap.core.platform.onebot.request_registry import LEDGER_MANIFEST_SPEC as LEDGER_SPEC, RequestApprovalLedger
from satrap.core.storage import durability as durability_module
from satrap.core.storage.durability import DurabilityManifest, Manifest, ManifestSpec, validate_manifest
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.storage.database import restore_session_domain
from satrap.core.storage import StorageLayout as _storage_layout_StorageLayout
from satrap.core.storage import StorageMaintenanceService as _storage_layout_StorageMaintenanceService, StorageScope
from satrap.core.storage import delete_session_domain_rows, storage_key
from satrap.core.storage.maintenance import StorageMaintenanceService as _storage_restore_StorageMaintenanceService
from satrap.core.storage.layout import StorageLayout as _storage_restore_StorageLayout

@pytest.mark.parametrize("db_class", [LiteVectorDB, DataBase])
def test_vector_database_rejects_empty_vectors_before_creating_collection(
    tmp_path: Path,
    db_class: type[LiteVectorDB] | type[DataBase],
):
    """空向量在创建集合或写入数据前被拒绝"""
    db = db_class(persist_path=str(tmp_path / "vec"))

    with pytest.raises(ValueError, match="向量不能为空"):
        db.add_to_collection("docs", ["a"], [[]], [{}])

    assert db.get_collection_names() == []

@pytest.mark.parametrize("db_class", [LiteVectorDB, DataBase])
def test_vector_database_rejects_inconsistent_batch_dimensions(
    tmp_path: Path,
    db_class: type[LiteVectorDB] | type[DataBase],
):
    """同一批次的向量维度不一致时不产生部分写入"""
    db = db_class(persist_path=str(tmp_path / "vec"))

    with pytest.raises(ValueError, match="同一批次的向量维度必须一致"):
        db.add_to_collection("docs", ["a", "b"], [[1.0], [0.0, 1.0]], [{}, {}])

    assert db.get_collection_names() == []

@pytest.mark.parametrize("db_class", [LiteVectorDB, DataBase])
def test_vector_database_rejects_dimension_change_without_partial_write(
    tmp_path: Path,
    db_class: type[LiteVectorDB] | type[DataBase],
):
    """后续写入的向量维度变化时保留原集合内容"""
    db = db_class(persist_path=str(tmp_path / "vec"))
    db.add_to_collection("docs", ["a"], [[1.0, 0.0]], [{}])

    with pytest.raises(ValueError, match="向量维度不一致"):
        db.add_to_collection("docs", ["b"], [[1.0, 0.0, 0.0]], [{}])

    assert db.get_collection_stats("docs")["document_count"] == 1

def test_lite_vector_add_search_roundtrip(tmp_path: Path):
    """添加文档后可搜索, 相似度排序正确"""
    db = LiteVectorDB(persist_path=str(tmp_path / "vec"))
    db.create_collection("docs")
    db.add_to_collection(
        "docs",
        ["苹果", "香蕉", "西瓜"],
        [[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]],
        [{"tag": "fruit"}, {"tag": "fruit"}, {"tag": "fruit"}],
    )

    results = db.search("docs", [1.0, 0.0], k=3, threshold=0.0)
    assert len(results) == 3
    assert results[0]["document"] == "苹果"
    assert results[0]["score"] > results[1]["score"]
    assert results[0]["metadata"] == {"tag": "fruit"}

def test_lite_vector_search_threshold_filters(tmp_path: Path):
    """低于阈值的文档被过滤"""
    db = LiteVectorDB(persist_path=str(tmp_path / "vec"))
    metadata: list[dict[str, Any]] = [{}] * 2
    db.add_to_collection(
        "docs",
        ["近", "远"],
        [[1.0, 0.0], [0.0, 1.0]],
        metadata,
    )
    results = db.search("docs", [1.0, 0.0], k=4, threshold=0.5)
    assert [r["document"] for r in results] == ["近"]

def test_lite_vector_missing_and_empty_collection(tmp_path: Path):
    """不存在的集合 / 空集合返回空结果"""
    db = LiteVectorDB(persist_path=str(tmp_path / "vec"))
    assert db.search("missing", [1.0, 0.0]) == []
    db.create_collection("empty")
    assert db.search("empty", [1.0, 0.0]) == []

def test_lite_vector_stats_and_delete(tmp_path: Path):
    """集合统计与删除"""
    db = LiteVectorDB(persist_path=str(tmp_path / "vec"))
    db.create_collection("docs")
    assert db.get_collection_stats("docs") == {"document_count": 0, "vector_dimension": 0}
    assert db.get_collection_stats("missing") == {"document_count": 0, "vector_dimension": 0}

    metadata2: list[dict[str, Any]] = [{}] * 2
    db.add_to_collection("docs", ["a", "b"], [[1.0, 0.0], [0.0, 1.0]], metadata2)
    stats = db.get_collection_stats("docs")
    assert stats["document_count"] == 2
    assert stats["vector_dimension"] == 2

    assert db.delete_collection("docs") is True
    assert db.get_collection_names() == []

def test_lite_vector_persist_roundtrip(tmp_path: Path):
    """数据持久化: 重建实例后从磁盘恢复"""
    persist = str(tmp_path / "vec")
    db = LiteVectorDB(persist_path=persist)
    db.add_to_collection(
        "docs", ["苹果"], [[1.0, 0.0]], [{"tag": "fruit"}],
    )

    db2 = LiteVectorDB(persist_path=persist)
    assert db2.get_collection_names() == ["docs"]
    results = db2.search("docs", [1.0, 0.0], k=1, threshold=0.0)
    assert results[0]["document"] == "苹果"

def test_lite_vector_add_auto_creates_collection(tmp_path: Path):
    """未创建集合时添加数据自动创建"""
    db = LiteVectorDB(persist_path=str(tmp_path / "vec"))
    added = db.add_to_collection("auto", ["x"], [[1.0, 0.0]], None)
    assert added == 1
    assert db.get_collection_names() == ["auto"]

def test_database_add_search_roundtrip(tmp_path: Path):
    """faiss 数据库添加与搜索"""
    db = DataBase(persist_path=str(tmp_path / "vec"))
    db.create_collection("docs")
    db.add_to_collection(
        "docs",
        ["苹果", "香蕉"],
        [[1.0, 0.0], [0.0, 1.0]],
        [{"tag": "fruit"}, {"tag": "fruit"}],
    )

    results = db.search("docs", [1.0, 0.0], k=2, threshold=0.0)
    assert len(results) == 2
    assert results[0]["document"] == "苹果"
    assert results[0]["score"] >= results[1]["score"]
    assert results[0]["metadata"] == {"tag": "fruit"}

    stats = db.get_collection_stats("docs")
    assert stats["document_count"] == 2
    assert stats["vector_dimension"] == 2

def test_database_missing_and_empty(tmp_path: Path):
    """不存在的集合返回空结果"""
    db = DataBase(persist_path=str(tmp_path / "vec"))
    assert db.search("missing", [1.0, 0.0]) == []
    assert db.get_collection_stats("missing") == {"document_count": 0, "vector_dimension": 0}

def test_database_length_mismatch_raises(tmp_path: Path):
    """documents / vectors / metadata 长度不一致抛 ValueError"""
    db = DataBase(persist_path=str(tmp_path / "vec"))
    db.create_collection("docs")
    metadata3: list[dict[str, Any]] = [{}] * 2
    with pytest.raises(ValueError, match="长度必须一致"):
        db.add_to_collection("docs", ["a"], [[1.0, 0.0], [0.0, 1.0]], metadata3)

def test_database_dim_mismatch_raises(tmp_path: Path):
    """向量维度不一致抛 ValueError"""
    db = DataBase(persist_path=str(tmp_path / "vec"))
    db.create_collection("docs")
    db.add_to_collection("docs", ["a"], [[1.0, 0.0]], [{}])
    with pytest.raises(ValueError, match="维度不一致"):
        db.add_to_collection("docs", ["b"], [[1.0, 0.0, 0.0]], [{}])

def test_database_delete_collection(tmp_path: Path):
    """删除集合: 数据与索引文件一并清理"""
    persist = str(tmp_path / "vec")
    db = DataBase(persist_path=persist)
    db.create_collection("docs")
    db.add_to_collection("docs", ["a"], [[1.0, 0.0]], [{}])

    assert db.delete_collection("docs") is True
    assert db.get_collection_names() == []
    assert db.get_collection_stats("docs") == {"document_count": 0, "vector_dimension": 0}

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
from unittest.mock import patch
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
from unittest.mock import patch
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

SAMPLE_SPEC = ManifestSpec(
    version=1,
    expected_files={"main": frozenset({"present", "missing"})},
    keep_extra_keys=False,
    degraded_requires_reason=True,
    normalize_degraded_at=True,
)

def _payload(**overrides: Any) -> dict[str, Any]:
    """一份合法清单, 按需覆写字段"""
    base: dict[str, Any] = {
        "version": 1,
        "initialized_at": 100.0,
        "updated_at": 200.0,
        "expected_files": {"main": "present"},
        "degraded": None,
    }
    base.update(overrides)
    return base

def _store_payload(**overrides: Any) -> dict[str, Any]:
    """存储清单样本: main 与 archive 都必须声明"""
    payload = _payload()
    payload["expected_files"] = {"main": "present", "archive": "absent"}
    payload.update(overrides)
    return payload

def _manifest(payload: object, spec: ManifestSpec = SAMPLE_SPEC) -> Manifest:
    return validate_manifest(payload, spec)

def _harness(
    tmp_path: Path,
    spec: ManifestSpec = SAMPLE_SPEC,
    validate: Callable[[object], Manifest] | None = None,
) -> tuple[DurabilityManifest, list[str]]:
    """构造组件与事务进入记录, 便于断言事务边界"""
    entered: list[str] = []

    @contextmanager
    def transaction() -> Iterator[None]:
        entered.append("enter")
        yield
        entered.append("exit")

    component = DurabilityManifest(
        tmp_path / "store.json", spec,
        transaction=transaction,
        validate=validate if validate is not None else (lambda raw: validate_manifest(raw, spec)),
        log_prefix="[Test]",
    )
    return component, entered

class TestStructureValidation:
    def test_missing_manifest_file_returns_none(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        assert component.read() is None

    def test_version_mismatch_rejected(self):
        with pytest.raises(ValueError, match="版本"):
            _manifest(_payload(version=2))

    def test_non_object_and_missing_expected_files_rejected(self):
        with pytest.raises(ValueError, match="对象"):
            _manifest(["not", "a", "dict"])
        with pytest.raises(ValueError, match="expected_files"):
            _manifest(_payload(expected_files=None))

    def test_expected_state_and_required_keys(self):
        # 未声明的状态与缺失的必需键都属于清单损坏
        with pytest.raises(ValueError, match="文件状态"):
            _manifest(_payload(expected_files={"main": "corrupt"}))
        with pytest.raises(ValueError, match="文件状态"):
            _manifest(_payload(expected_files={}))
        # 声明的键之外的 expected_files 键按声明忽略
        manifest = _manifest(_payload(expected_files={"main": "missing", "extra": "whatever"}))
        assert manifest.expected == {"main": "missing"}

    def test_time_fields_must_be_numbers(self):
        with pytest.raises(ValueError, match="时间字段"):
            _manifest(_payload(updated_at="200"))
        with pytest.raises(ValueError, match="时间字段"):
            _manifest(_payload(initialized_at=True))

    def test_read_round_trips_file(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        (tmp_path / "store.manifest.json").write_text(
            json.dumps(_payload(expected_files={"main": "missing"})), encoding="utf-8",
        )
        manifest = component.read()
        assert manifest is not None
        assert manifest.expected == {"main": "missing"}
        assert manifest.initialized_at == 100.0

    def test_validator_hook_runs_business_cross_check(self, tmp_path: Path):
        # 业务方在薄层里追加交叉校验 (存储要求未降级时主文件必须 present)
        def cross_checked(raw: object) -> Manifest:
            manifest = validate_manifest(raw, SAMPLE_SPEC)
            if manifest.degraded is None and manifest.expected["main"] != "present":
                raise ValueError("清单声明主文件缺失但未降级")
            return manifest

        component, _ = _harness(tmp_path, SAMPLE_SPEC, cross_checked)
        (tmp_path / "store.manifest.json").write_text(
            json.dumps(_payload(expected_files={"main": "missing"})), encoding="utf-8",
        )
        with pytest.raises(ValueError, match="未降级"):
            component.read()

    def test_broken_json_raises_decode_error(self, tmp_path: Path):
        (tmp_path / "store.manifest.json").write_text("not-json", encoding="utf-8")
        component, _ = _harness(tmp_path)
        with pytest.raises(json.JSONDecodeError):
            component.read()

class TestExtraKeyAndDegradedSemantics:
    def test_extra_keys_dropped_when_spec_disallows(self):
        manifest = _manifest(_payload(extra_key="keep-me", expected_files={"main": "present", "extra": "x"}))
        assert "extra_key" not in manifest.payload()
        assert manifest.payload()["expected_files"] == {"main": "present"}

    def test_extra_keys_kept_when_spec_allows(self):
        spec = ManifestSpec(version=1, expected_files={"entries": frozenset({"present"})}, keep_extra_keys=True)
        manifest = _manifest({"version": 1, "initialized_at": 1.0, "updated_at": 2.0,
                              "expected_files": {"entries": "present"}, "degraded": None, "note": {"a": 1}}, spec)
        assert manifest.payload()["note"] == {"a": 1}
        # 额外键不影响声明字段的归一化结果
        assert manifest.expected == {"entries": "present"}

    def test_degraded_reason_strictness(self):
        with pytest.raises(ValueError, match="降级原因"):
            _manifest(_payload(degraded={"reason": "", "at": 1.0}))
        with pytest.raises(ValueError, match="降级原因"):
            _manifest(_payload(degraded={"at": 1.0}))
        with pytest.raises(ValueError, match="降级字段"):
            _manifest(_payload(degraded="broken"))
        lenient = ManifestSpec(version=1, expected_files={"entries": frozenset({"present"})},
                               keep_extra_keys=True, degraded_requires_reason=False, normalize_degraded_at=False)
        manifest = _manifest({"version": 1, "initialized_at": 1.0, "updated_at": 2.0,
                              "expected_files": {"entries": "present"}, "degraded": {"reason": "", "at": "raw"}}, lenient)
        assert manifest.degraded is not None
        assert manifest.degraded.reason == ""
        # 归一化视图不解释 at, 写回时按原值写回而不是替换成 0
        assert manifest.degraded.at == 0.0
        assert manifest.payload()["degraded"] == {"reason": "", "at": "raw"}

    def test_degraded_at_normalized_when_spec_requires(self):
        assert _manifest(_payload(degraded={"reason": "x"})).degraded is not None
        manifest = _manifest(_payload(degraded={"reason": "x", "at": "12"}))
        assert manifest.degraded is not None and manifest.degraded.at == 12.0
        # 非数字的 at 按清单损坏处理, 与迁移前的行为一致
        with pytest.raises(ValueError):
            _manifest(_payload(degraded={"reason": "x", "at": "not-a-number"}))

    def test_mark_degraded_sets_marker_without_touching_expected(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        manifest = component.fresh(initialized_at=10.0, expected={"main": "present"})
        written: list[Manifest] = []
        outcome = component.mark_degraded(manifest, reason="main_corrupt", persist=written.append)
        assert outcome.persisted is True
        assert outcome.manifest.degraded is not None
        assert outcome.manifest.degraded.reason == "main_corrupt"
        assert outcome.manifest.expected == {"main": "present"}
        assert written and written[0].payload()["degraded"]["reason"] == "main_corrupt"

class TestDegradeOrdering:
    def test_marker_failure_keeps_original_file(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        bad = tmp_path / "store.json"
        bad.write_text("not-json{", encoding="utf-8")

        def broken_persist(manifest: Manifest) -> None:
            raise OSError("disk full")

        outcome = component.degrade_then_quarantine(
            component.fresh(initialized_at=1.0, expected={"main": "present"}),
            reason="main_corrupt", persist=broken_persist, bad_files=[bad],
        )
        assert outcome.persisted is False
        assert outcome.quarantined == ()
        assert bad.is_file() and bad.read_text(encoding="utf-8") == "not-json{"
        assert list(tmp_path.glob("store.json.corrupt-*")) == []
        # 内存清单仍带降级标记, 调用方据此保持降级
        assert outcome.manifest.degraded is not None

    def test_timeout_during_marker_keeps_original_file(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        bad = tmp_path / "store.json"
        bad.write_text("not-json{", encoding="utf-8")

        def timeout_persist(manifest: Manifest) -> None:
            raise TimeoutError("lock busy")

        outcome = component.degrade_then_quarantine(
            component.fresh(initialized_at=1.0, expected={"main": "present"}),
            reason="main_corrupt", persist=timeout_persist, bad_files=[bad],
        )
        assert outcome.persisted is False and bad.is_file()

    def test_marker_written_before_quarantine(self, tmp_path: Path):
        component, entered = _harness(tmp_path)
        bad = tmp_path / "store.json"
        bad.write_text("not-json{", encoding="utf-8")
        order: list[str] = []

        def persist(manifest: Manifest) -> None:
            order.append("marker")
            assert bad.is_file(), "标记必须先于隔离"

        outcome = component.degrade_then_quarantine(
            component.fresh(initialized_at=1.0, expected={"main": "present"}),
            reason="main_corrupt", persist=persist, bad_files=[bad],
        )
        order.append("after")
        assert order == ["marker", "after"]
        assert outcome.persisted is True
        assert [item.original for item in outcome.quarantined] == [bad]
        assert not bad.is_file()
        # 标记与隔离各进一次事务
        assert len(entered) == 4

    def test_quarantine_failure_does_not_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        component, _ = _harness(tmp_path)
        bad = tmp_path / "store.json"
        bad.write_text("not-json{", encoding="utf-8")

        def broken_quarantine(path: Path) -> Path:
            raise OSError("rename failed")

        monkeypatch.setattr(durability_module, "quarantine_file", broken_quarantine)
        outcome = component.degrade_then_quarantine(
            component.fresh(initialized_at=1.0, expected={"main": "present"}),
            reason="main_corrupt", persist=lambda manifest: None, bad_files=[bad],
        )
        assert outcome.persisted is True and outcome.quarantined == ()
        assert bad.is_file()

    def test_quarantine_preserves_bytes_and_scan_ignores_manifest_corruptions(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        bad = tmp_path / "store.json"
        bad.write_text("not-json{", encoding="utf-8")
        outcome = component.degrade_then_quarantine(
            component.fresh(initialized_at=1.0, expected={"main": "present"}),
            reason="main_corrupt", persist=lambda manifest: None, bad_files=[bad],
        )
        item = outcome.quarantined[0]
        assert item.target.name.startswith("store.json.corrupt-")
        assert item.target.read_text(encoding="utf-8") == "not-json{"
        # 扫描只认数据文件的隔离件, 清单自己的隔离件不算目录曾初始化
        (tmp_path / "store.manifest.json.corrupt-20250101-000000").write_text("{}", encoding="utf-8")
        assert component.quarantine_names() == [item.target.name]

class TestFrozenManifestSamples:
    """两类存储的旧清单样本: 迁移前后必须给出同一结论

    样本取自迁移前的读取路径 (manual_wake_store._validate_manifest 与
    request_registry._read_manifest) 及其测试覆盖的组合
    """

    def test_store_samples(self):
        manifest = _manifest(_store_payload(), STORE_SPEC)
        assert manifest.expected == {"main": "present", "archive": "absent"}
        assert manifest.degraded is None
        # 未降级却声明主文件缺失: 本组件放行, 业务交叉校验拒绝
        contested = _manifest(_store_payload(expected_files={"main": "missing", "archive": "absent"}), STORE_SPEC)
        assert contested.degraded is None
        assert contested.expected == {"main": "missing", "archive": "absent"}
        # 版本非法, 缺键, 非法状态, 空原因都拒读
        with pytest.raises(ValueError):
            _manifest(_store_payload(version=2), STORE_SPEC)
        with pytest.raises(ValueError):
            _manifest(_store_payload(expected_files={"archive": "absent"}), STORE_SPEC)
        with pytest.raises(ValueError):
            _manifest(_store_payload(expected_files={"main": "present", "archive": "gone"}), STORE_SPEC)
        with pytest.raises(ValueError):
            _manifest(_store_payload(degraded={"reason": "", "at": 1.0}), STORE_SPEC)
        # 降级清单的额外键按存储语义丢弃, 归档三态保留
        degraded = _manifest(
            _store_payload(expected_files={"main": "missing", "archive": "missing"},
                           degraded={"reason": "main_corrupt", "at": 3.0, "note": "drop"}),
            STORE_SPEC,
        )
        assert degraded.payload() == {
            "version": 1, "initialized_at": 100.0, "updated_at": 200.0,
            "expected_files": {"main": "missing", "archive": "missing"},
            "degraded": {"reason": "main_corrupt", "at": 3.0},
        }

    def test_ledger_samples(self):
        sample: dict[str, Any] = {"version": 1, "initialized_at": 1.0, "updated_at": 2.0,
                                  "expected_files": {"entries": "present"}, "degraded": None, "history": ["kept"]}
        manifest = _manifest(sample, LEDGER_SPEC)
        assert manifest.expected == {"entries": "present"}
        # 账本保留历史额外键: 无改动时写回与读入逐键一致
        assert manifest.payload() == sample
        lenient = _manifest({**sample, "degraded": {"reason": "", "at": "raw", "note": "x"}}, LEDGER_SPEC)
        assert lenient.degraded is not None and lenient.degraded.reason == ""
        # 降级原因允许空串, 不解释 at, 嵌套额外键按原值写回
        assert lenient.payload() == {**sample, "degraded": {"reason": "", "at": "raw", "note": "x"}}
        # 账本要求 entries 恒为 present: 其它取值仍是清单损坏
        with pytest.raises(ValueError):
            _manifest({**sample, "expected_files": {"entries": "missing"}}, LEDGER_SPEC)
        with pytest.raises(ValueError):
            _manifest({**sample, "degraded": {"reason": None}}, LEDGER_SPEC)
        with pytest.raises(ValueError):
            _manifest({**sample, "version": 2}, LEDGER_SPEC)

    def test_ledger_snapshot_write_back_keeps_all_extra_keys(self):
        """旧清单完整写回: 顶层, expected_files 与 degraded 的额外键都要原样保留"""
        sample: dict[str, Any] = {
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"entries": "present", "future": "keep"},
            "degraded": {"reason": "", "at": "raw", "note": "keep"},
            "history": ["kept"],
        }
        manifest = _manifest(sample, LEDGER_SPEC)
        assert manifest.payload() == sample
        # 声明键仍以归一化视图为准, 额外键不参与判定
        assert manifest.expected == {"entries": "present"}
        assert manifest.degraded is not None and manifest.degraded.at == 0.0

    def test_active_marker_replaces_nested_payload_only(self):
        """主动写入新降级标记时按旧行为整体替换标记, 其它位置的额外键不受影响"""
        sample: dict[str, Any] = {
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"entries": "present", "future": "keep"},
            "degraded": {"reason": "old", "at": 5.0, "note": "drop"},
            "history": ["kept"],
        }
        payload = _manifest(sample, LEDGER_SPEC).with_degraded("probe", 9.0).payload()
        assert payload["degraded"] == {"reason": "probe", "at": 9.0}
        assert payload["expected_files"] == {"entries": "present", "future": "keep"}
        assert payload["history"] == ["kept"]

class TestFreshManifest:
    def test_fresh_payload_matches_legacy_skeleton(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        manifest = component.fresh(initialized_at=5.0, expected={"main": "present"}, updated_at=6.0)
        assert manifest.payload() == {
            "version": 1, "initialized_at": 5.0, "updated_at": 6.0,
            "expected_files": {"main": "present"}, "degraded": None,
        }
        assert manifest.version == 1

    def test_with_expected_and_updated_at_keep_degraded(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        manifest = component.fresh(initialized_at=5.0, expected={"main": "present"})
        marked = manifest.with_degraded("main_corrupt", 7.0)
        updated = marked.with_expected({"main": "missing"}).with_updated_at(8.0)
        assert updated.degraded is not None and updated.degraded.at == 7.0
        assert updated.updated_at == 8.0
        assert updated.payload()["expected_files"] == {"main": "missing"}
        # 原清单不被就地改动
        assert manifest.degraded is None and manifest.expected == {"main": "present"}

    def test_payload_does_not_alias_declared_fields(self):
        spec = ManifestSpec(version=1, expected_files={"entries": frozenset({"present"})}, keep_extra_keys=True)
        source: dict[str, Any] = {"version": 1, "initialized_at": 1.0, "updated_at": 2.0,
                                  "expected_files": {"entries": "present"}, "degraded": {"reason": "x", "at": 1.0}}
        manifest = _manifest(source, spec)
        payload = cast(dict[str, Any], manifest.payload())
        payload["expected_files"]["entries"] = "missing"
        payload["degraded"]["reason"] = "rewritten"
        assert manifest.expected == {"entries": "present"}
        assert manifest.degraded is not None and manifest.degraded.reason == "x"

class TestStorageDecisions:
    """用冻结清单样本驱动两类存储, 固定迁移后的接受/拒绝与降级结论"""

    @staticmethod
    def _store(tmp_path: Path) -> ManualWakeStore:
        return ManualWakeStore(tmp_path / "store.json")

    @staticmethod
    def _write_manifest(tmp_path: Path, payload: dict[str, Any]) -> None:
        (tmp_path / "store.manifest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_store_manifest_with_extra_key_and_reason_is_accepted(self, tmp_path: Path):
        store = self._store(tmp_path)
        store.accept_request("bot", "r1", "fp", "group:20", "op")
        self._write_manifest(tmp_path, {
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"main": "present", "archive": "absent"},
            "degraded": {"reason": "main_corrupt", "at": 3.0, "note": "旧版本的额外字段"},
            "history": ["ignored"],
        })
        restarted = self._store(tmp_path)
        assert restarted.degraded is True and restarted.degraded_reason == "main_corrupt"
        # 降级期间不加载记录, 额外键不影响判定; recover 校验通过后才恢复读取
        assert restarted.lookup_request("r1", "bot") is None
        assert restarted.recover() is True
        assert restarted.degraded is False
        assert restarted.lookup_request("r1", "bot") is not None
        rewritten = json.loads((tmp_path / "store.manifest.json").read_text(encoding="utf-8"))
        assert rewritten["degraded"] is None and "history" not in rewritten

    def test_store_manifest_empty_reason_is_unreadable(self, tmp_path: Path):
        store = self._store(tmp_path)
        store.accept_request("bot", "r1", "fp", "group:20", "op")
        self._write_manifest(tmp_path, {
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"main": "present", "archive": "absent"},
            "degraded": {"reason": "", "at": 3.0},
        })
        # 不可读清单按"有数据文件"迁移, 不静默当空库, 也不报告降级
        restarted = self._store(tmp_path)
        assert restarted.degraded is False
        assert restarted.lookup_request("r1", "bot") is not None
        rewritten = json.loads((tmp_path / "store.manifest.json").read_text(encoding="utf-8"))
        assert rewritten["degraded"] is None
        assert "history" not in rewritten

    def test_store_manifest_contradiction_is_unreadable(self, tmp_path: Path):
        store = self._store(tmp_path)
        store.accept_request("bot", "r1", "fp", "group:20", "op")
        # 未降级却声明主文件缺失: 自相矛盾, 按不可读迁移而不是接受
        self._write_manifest(tmp_path, {
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"main": "missing", "archive": "absent"}, "degraded": None,
        })
        restarted = self._store(tmp_path)
        assert restarted.degraded is False
        assert restarted.lookup_request("r1", "bot") is not None
        rewritten = json.loads((tmp_path / "store.manifest.json").read_text(encoding="utf-8"))
        assert rewritten["expected_files"] == {"main": "present", "archive": "absent"}

    def test_store_marker_failure_still_degrades_in_memory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        path = tmp_path / "store.json"
        path.write_text("not-json{", encoding="utf-8")

        def broken_write(self: ManualWakeStore, manifest: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(ManualWakeStore, "_write_manifest", broken_write)
        store = self._store(tmp_path)
        assert store.degraded is True
        assert store.degraded_reason == "manifest_missing_with_data"
        assert path.read_text(encoding="utf-8") == "not-json{"
        assert list(tmp_path.glob("store.json.corrupt-*")) == []

    def test_store_quarantine_failure_keeps_degraded_after_restart(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        path = tmp_path / "store.json"
        path.write_text("not-json{", encoding="utf-8")

        def broken_quarantine(item: Path) -> Path:
            raise OSError("rename failed")

        monkeypatch.setattr(durability_module, "quarantine_file", broken_quarantine)
        store = self._store(tmp_path)
        assert store.degraded is True
        # 隔离失败不解除降级: 重启按清单与坏文件重新判定
        monkeypatch.undo()
        restarted = self._store(tmp_path)
        assert restarted.degraded is True and restarted.degraded_reason != ""

    def test_ledger_samples(self, tmp_path: Path):
        ledger = RequestApprovalLedger(tmp_path / "request_ledger.json")
        assert ledger.degraded is False
        # 额外键与空降级原因: 账本保留额外键, 空原因回落为 degraded
        (tmp_path / "request_ledger.manifest.json").write_text(json.dumps({
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"entries": "present"},
            "degraded": {"reason": "", "at": "raw", "note": "旧字段"},
            "history": ["kept"],
        }, ensure_ascii=False), encoding="utf-8")
        restarted = RequestApprovalLedger(tmp_path / "request_ledger.json")
        assert restarted.degraded is True and restarted.degraded_reason == "degraded"
        # 清单状态与数据文件不一致: 未降级清单要求 entries 恒存在, 其它取值按清单损坏迁移
        (tmp_path / "request_ledger.manifest.json").write_text(json.dumps({
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"entries": "missing"}, "degraded": None,
        }, ensure_ascii=False), encoding="utf-8")
        migrated = RequestApprovalLedger(tmp_path / "request_ledger.json")
        assert migrated.degraded is False
        assert json.loads((tmp_path / "request_ledger.manifest.json").read_text(encoding="utf-8"))["expected_files"] == {"entries": "present"}

    def test_ledger_degrade_write_keeps_manifest_extras(self, tmp_path: Path):
        """真实降级写入不得丢掉旧清单的 expected_files 与顶层扩展键"""
        ledger = RequestApprovalLedger(tmp_path / "request_ledger.json")
        assert ledger.degraded is False
        (tmp_path / "request_ledger.manifest.json").write_text(json.dumps({
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"entries": "present", "future": "keep"},
            "degraded": None, "history": ["kept"],
        }, ensure_ascii=False), encoding="utf-8")
        restarted = RequestApprovalLedger(tmp_path / "request_ledger.json")
        assert restarted.degraded is False
        assert restarted._mark_degraded("probe", "审计反例: 降级写入") is True
        rewritten = json.loads((tmp_path / "request_ledger.manifest.json").read_text(encoding="utf-8"))
        assert rewritten["expected_files"] == {"entries": "present", "future": "keep"}
        assert rewritten["history"] == ["kept"]
        # 标记是本次新写入的: 按声明字段生成, 带真实时间
        assert rewritten["degraded"]["reason"] == "probe"
        assert rewritten["degraded"]["at"] > 0

    def test_ledger_recover_failure_keeps_degraded(self, tmp_path: Path):
        path = tmp_path / "request_ledger.json"
        path.write_text("{ 不是 JSON", encoding="utf-8")
        ledger = RequestApprovalLedger(path)
        assert ledger.degraded is True
        assert ledger.recover() is False
        assert ledger.degraded is True and ledger.degraded_reason == "manifest_missing_with_data"

    def test_memory_ledger_creates_no_files(self, tmp_path: Path):
        ledger = RequestApprovalLedger(None)
        ledger.degraded = True
        assert ledger._mark_degraded("init_failed", "内存账本") is True
        assert ledger.degraded is True and ledger.degraded_reason == "init_failed"
        # 无持久路径的账本不写任何文件
        assert list(tmp_path.iterdir()) == []

def test_storage_key_is_stable_and_collision_resistant():
    """不同原始 ID 即使旧式替换结果相同也必须产生不同目录键"""
    assert storage_key("a:b", fallback="session") != storage_key("a_b", fallback="session")
    assert storage_key("a:b", fallback="session") == storage_key("a:b", fallback="session")

def test_platform_uses_one_database(tmp_path: Path):
    """同一平台的全部领域应解析到唯一 platform.db"""
    layout = _storage_layout_StorageLayout(tmp_path / "data")

    assert layout.platform_db("onebot-main").name == "platform.db"
    assert layout.platform_db("onebot-main").parent == layout.platform_root("onebot-main")
    assert layout.platform_db("onebot-main") != layout.platform_db("onebot-backup")

def test_session_binding_and_overrides_do_not_create_session_directories(tmp_path: Path):
    """会话身份和参数覆盖存入数据库, 绑定多个会话也不生成空目录"""
    layout = _storage_layout_StorageLayout(tmp_path / "data")
    store = SessionOverrideStore(layout.platform_db("chat"))
    for number in range(20):
        session_id = f"conversation-{number}"
        root = layout.bind_session(StorageScope("chat", session_id=session_id))
        store.replace(session_id, "plugins.example", {"enabled": False}, expected_revision=0)
        assert not root.exists()
    assert store.read("conversation-10", "plugins.example")["overrides"] == {"enabled": False}

def test_session_sizes_only_scan_on_explicit_refresh(tmp_path: Path, monkeypatch):
    """列表读取旧缓存也不扫描磁盘, 刷新操作才更新大小和时间"""
    layout = _storage_layout_StorageLayout(tmp_path / "data")
    maintenance = _storage_layout_StorageMaintenanceService(layout)
    assert maintenance.session_size_snapshot("chat")["storage_size_bytes"] is None
    root = layout.bind_session(StorageScope("chat", session_id="one"))
    (root / "uploads").mkdir(parents=True)
    document = root / "uploads" / "text.txt"
    document.write_bytes(b"hello")
    snapshot = maintenance.session_size_snapshot("chat", refresh=True)
    assert snapshot["storage_size_bytes"] == 5
    document.write_bytes(b"changed")
    def unexpected_scan(*args):
        raise AssertionError("列表不能遍历目录")
    monkeypatch.setattr(maintenance, "_directory_size", unexpected_scan)
    assert maintenance.session_size_snapshot("chat") == snapshot

def test_ensure_session_creates_isolated_directories(tmp_path: Path):
    """每个会话必须拥有独立 sandbox、uploads、artifacts、indexes 和 cache"""
    layout = _storage_layout_StorageLayout(tmp_path / "data")
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
    """用户身份单独落盘, 删除会话时整体移入同平台回收区"""
    layout = _storage_layout_StorageLayout(tmp_path / "data")
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
    """会话级联清理只删除目标会话的上下文和记忆"""
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
    """维护扫描应区分孤儿目录和孤儿数据库行且保持只读"""
    layout = _storage_layout_StorageLayout(tmp_path / "data")
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

    service = _storage_layout_StorageMaintenanceService(layout)
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
    """完整回收包应恢复会话配置、上下文和私有文件"""
    layout = _storage_layout_StorageLayout(tmp_path / "data")
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

    service = _storage_layout_StorageMaintenanceService(layout)
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
    """恢复归档时应拒绝不属于目标架构的列名"""
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
    """恢复回收包时不得读取或删除回收目录外的路径"""
    layout = _storage_layout_StorageLayout(tmp_path / "data")
    service = _storage_layout_StorageMaintenanceService(layout)
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
    """恢复回收包时不得跟随指向回收目录外的符号链接"""
    layout = _storage_layout_StorageLayout(tmp_path / "data")
    service = _storage_layout_StorageMaintenanceService(layout)
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
    """回收区应支持显式批量删除和按保留期清理"""
    layout = _storage_layout_StorageLayout(tmp_path / "data")
    service = _storage_layout_StorageMaintenanceService(layout)
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

@pytest.fixture
def archive_case(tmp_path):
    layout = _storage_restore_StorageLayout(tmp_path / "data")
    database = layout.platform_db("audit")
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute("CREATE TABLE conversation_meta (conversation_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO conversation_meta VALUES ('conversation')")
    root = layout.session_root("audit", "conversation")
    root.mkdir(parents=True)
    (root / "payload.txt").write_text("待恢复内容", encoding="utf-8")
    service = _storage_restore_StorageMaintenanceService(layout)
    manifest = service.archive_session("audit", "conversation")
    archive = layout.trash_root("audit") / "sessions" / manifest["archive_id"]
    return service, database, root, archive, manifest

@pytest.mark.parametrize("damaged", ["{invalid", "[]"], ids=["invalid-json", "wrong-root-type"])
def test_restore_must_preserve_invalid_archive(tmp_path, damaged):
    layout = _storage_restore_StorageLayout(tmp_path / "data")
    database = layout.platform_db("audit")
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE conversation_meta (conversation_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO conversation_meta VALUES ('conversation')")
    conn.close()
    service = _storage_restore_StorageMaintenanceService(layout)
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
    layout = _storage_restore_StorageLayout(tmp_path / "data")
    root = layout.session_root("audit", "files-only")
    root.mkdir(parents=True)
    (root / "payload").write_text("files", encoding="utf-8")
    service = _storage_restore_StorageMaintenanceService(layout)
    manifest = service.archive_session("audit", "files-only")
    assert manifest["tables"] == []
    assert service.restore_archive("audit", manifest["archive_id"])["ok"]
    assert (root / "payload").read_text(encoding="utf-8") == "files"
