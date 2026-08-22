"""
LiteVectorDB / DataBase 向量库单元测试

覆盖:
- LiteVectorDB: 集合管理 / 添加 / 搜索排序与阈值 / 统计 / 删除 / 持久化 roundtrip
- DataBase (faiss + SQLite): 添加 / 搜索 / 统计 / 维度与长度校验
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from satrap.core.database import DataBase, LiteVectorDB


# ================= LiteVectorDB 测试 =================


def test_lite_vector_add_search_roundtrip(tmp_path: Path):
    """
    添加文档后可搜索, 相似度排序正确

    参数:
    - tmp_path: tmp路径
    """
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
    """
    低于阈值的文档被过滤

    参数:
    - tmp_path: tmp路径
    """
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
    """
    不存在的集合 / 空集合返回空结果

    参数:
    - tmp_path: tmp路径
    """
    db = LiteVectorDB(persist_path=str(tmp_path / "vec"))
    assert db.search("missing", [1.0, 0.0]) == []
    db.create_collection("empty")
    assert db.search("empty", [1.0, 0.0]) == []


def test_lite_vector_stats_and_delete(tmp_path: Path):
    """
    集合统计与删除

    参数:
    - tmp_path: tmp路径
    """
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
    """
    数据持久化: 重建实例后从磁盘恢复

    参数:
    - tmp_path: tmp路径
    """
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
    """
    未创建集合时添加数据自动创建

    参数:
    - tmp_path: tmp路径
    """
    db = LiteVectorDB(persist_path=str(tmp_path / "vec"))
    added = db.add_to_collection("auto", ["x"], [[1.0, 0.0]], None)   # type: ignore[arg-type]
    assert added == 1
    assert db.get_collection_names() == ["auto"]


# ================= DataBase (faiss + SQLite) 测试 =================


def test_database_add_search_roundtrip(tmp_path: Path):
    """
    faiss 数据库添加与搜索

    参数:
    - tmp_path: tmp路径
    """
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
    """
    不存在的集合返回空结果

    参数:
    - tmp_path: tmp路径
    """
    db = DataBase(persist_path=str(tmp_path / "vec"))
    assert db.search("missing", [1.0, 0.0]) == []
    assert db.get_collection_stats("missing") == {"document_count": 0, "vector_dimension": 0}


def test_database_length_mismatch_raises(tmp_path: Path):
    """
    documents / vectors / metadata 长度不一致抛 ValueError

    参数:
    - tmp_path: tmp路径
    """
    db = DataBase(persist_path=str(tmp_path / "vec"))
    db.create_collection("docs")
    metadata3: list[dict[str, Any]] = [{}] * 2
    with pytest.raises(ValueError, match="长度必须一致"):
        db.add_to_collection("docs", ["a"], [[1.0, 0.0], [0.0, 1.0]], metadata3)


def test_database_dim_mismatch_raises(tmp_path: Path):
    """
    向量维度不一致抛 ValueError

    参数:
    - tmp_path: tmp路径
    """
    db = DataBase(persist_path=str(tmp_path / "vec"))
    db.create_collection("docs")
    db.add_to_collection("docs", ["a"], [[1.0, 0.0]], [{}])
    with pytest.raises(ValueError, match="维度不一致"):
        db.add_to_collection("docs", ["b"], [[1.0, 0.0, 0.0]], [{}])


def test_database_delete_collection(tmp_path: Path):
    """
    删除集合: 数据与索引文件一并清理

    参数:
    - tmp_path: tmp路径
    """
    persist = str(tmp_path / "vec")
    db = DataBase(persist_path=persist)
    db.create_collection("docs")
    db.add_to_collection("docs", ["a"], [[1.0, 0.0]], [{}])

    assert db.delete_collection("docs") is True
    assert db.get_collection_names() == []
    assert db.get_collection_stats("docs") == {"document_count": 0, "vector_dimension": 0}
