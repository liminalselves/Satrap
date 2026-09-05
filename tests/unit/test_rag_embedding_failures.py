"""RAG 对部分 Embedding 失败结果的处理测试"""
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from satrap.expend.tools.rag import LiteVectorRAG, _filter_embedding_results


class _FakeEmbedding:
    """返回预设批量向量的 Embedding 替身"""

    def __init__(self, result: list[list[float]] | Literal[False]):
        self.result: list[list[float]] | Literal[False] = result
        self.inputs: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]] | Literal[False]:
        """记录当前输入并返回预设结果"""
        self.inputs.append(texts)
        return self.result


class _FakeVectorDB:
    """记录批量写入内容的向量库替身"""

    def __init__(self):
        self.calls: list[tuple[str, list[str], list[list[float]], list[dict[str, Any]]]] = []

    def add_to_collection(
        self,
        name: str,
        documents: list[str],
        vectors: list[list[float]],
        metadata: list[dict[str, Any]],
    ) -> int:
        """记录一次批量写入"""
        self.calls.append((name, documents, vectors, metadata))
        return len(documents)


def _rag_with_fakes(
    embedding_result: list[list[float]] | Literal[False],
) -> tuple[LiteVectorRAG, _FakeEmbedding, _FakeVectorDB]:
    """构造不访问网络和磁盘的 RAG 实例"""
    embedding = _FakeEmbedding(embedding_result)
    vector_db = _FakeVectorDB()
    rag = object.__new__(LiteVectorRAG)
    rag.default_vectorstore_name = "test"
    rag.chunk_size = 100
    rag.chunk_overlap = 0
    rag.batch_size = 3
    rag.k_default = 4
    rag.threshold = 0.5
    rag.embeddings = cast(Any, embedding)
    rag.vector_db = cast(Any, vector_db)
    return rag, embedding, vector_db


def test_filter_embedding_results_rejects_length_mismatch():
    """输入和返回数量不一致时拒绝继续配对"""
    with pytest.raises(ValueError, match="返回数量与输入文本数量不一致"):
        _filter_embedding_results(["a", "b"], [[1.0]])


@pytest.mark.asyncio
async def test_add_documents_filters_texts_with_empty_vectors():
    """部分向量失败时只把位置对应的有效文本写入数据库"""
    rag, embedding, vector_db = _rag_with_fakes([[1.0, 0.0], [], [0.0, 1.0]])

    result = await rag.add_documents(["a", "b", "c"])

    assert result is True
    assert embedding.inputs == [["a", "b", "c"]]
    assert vector_db.calls == [
        (
            "test",
            ["a", "c"],
            [[1.0, 0.0], [0.0, 1.0]],
            [{}, {}],
        ),
    ]


@pytest.mark.asyncio
async def test_add_documents_returns_false_when_all_embeddings_fail():
    """非空文档全部向量化失败时不写入数据库并返回 False"""
    rag, embedding, vector_db = _rag_with_fakes([[], [], []])

    result = await rag.add_documents(["a", "b", "c"])

    assert result is False
    assert embedding.inputs == [["a", "b", "c"]]
    assert vector_db.calls == []


@pytest.mark.asyncio
async def test_add_text_file_filters_empty_vectors(tmp_path: Path):
    """
    文件导入只写入向量化成功的文本块

    参数:
    - tmp_path: 临时目录
    """
    rag, embedding, vector_db = _rag_with_fakes([[1.0, 0.0], [], [0.0, 1.0]])
    file_path = tmp_path / "input.txt"
    file_path.write_text("a\nb\nc", encoding="utf-8")

    result = await rag.add_text_file(
        str(file_path),
        chunk_size=1,
        chunk_overlap=0,
        batch_size=3,
    )

    assert result is True
    assert embedding.inputs == [["a", "\nb", "\nc"]]
    assert vector_db.calls[0][1:3] == (["a", "\nc"], [[1.0, 0.0], [0.0, 1.0]])


@pytest.mark.asyncio
async def test_swift_filters_empty_vectors_before_writing():
    """快速流程的添加阶段同步过滤失败文本和空向量"""
    rag, embedding, vector_db = _rag_with_fakes([[1.0, 0.0], [], [0.0, 1.0]])

    documents, scores = await rag.swift(add_documents=["a", "b", "c"])

    assert documents == []
    assert scores == []
    assert embedding.inputs == [["a", "b", "c"]]
    assert vector_db.calls[0][1:3] == (["a", "c"], [[1.0, 0.0], [0.0, 1.0]])
