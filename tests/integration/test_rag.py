from pathlib import Path
import pytest
import os

from satrap.expend.tools.rag import LiteVectorRAG


pytestmark = [pytest.mark.integration, pytest.mark.requires_api, pytest.mark.asyncio]


@pytest.fixture
def rag(tmp_path: Path) -> LiteVectorRAG:
    base_url = os.getenv("TEST_EMBED_BASE_URL")
    api_key = os.getenv("TEST_EMBED_API_KEY")
    if not base_url or not api_key:
        pytest.skip("设置 TEST_EMBED_BASE_URL 和 TEST_EMBED_API_KEY 后运行")

    return LiteVectorRAG(
        base_url=base_url,
        api_key=api_key,
        embed_model=os.getenv("TEST_EMBED_MODEL", "BAAI/bge-large-en-v1.5"),
        persist_directory=str(tmp_path / "vectorstore"),
        default_vectorstore_name="test_collection",
        k_default=2,
        chunk_size=50,
        chunk_overlap=10,
        threshold=0.0,
        batch_size=2,
    )


async def test_simple_query(rag: LiteVectorRAG):
    await rag.add_documents(
        [
            "The quick brown fox jumps over the lazy dog.",
            "A fast brown fox leaps over a sleepy hound.",
            "Python is a popular programming language.",
        ],
        collection_name="test_collection",
    )

    results = await rag.simple_query("fox jumps over dog", k=2, threshold=0.0)

    assert results is not None
    assert len(results) == 2
    assert "fox" in results[0].lower()
    assert "dog" in results[0].lower()


async def test_add_documents(rag: LiteVectorRAG):
    assert await rag.add_documents(
        ["doc1", "doc2", "doc3"],
        collection_name="test_collection",
        batch_size=2,
    )

    stats = rag.vector_db.get_collection_stats("test_collection")
    assert stats["document_count"] >= 3


async def test_add_text_file(rag: LiteVectorRAG, tmp_path: Path):
    file_path = tmp_path / "test_rag.txt"
    file_path.write_text(
        "Line 1: RAG stands for Retrieval-Augmented Generation.\n"
        "Line 2: It combines retrieval systems with generative models.",
        encoding="utf-8",
    )

    assert await rag.add_text_file(
        str(file_path),
        collection_name="test_collection",
        chunk_size=30,
        chunk_overlap=5,
    )

    results = await rag.simple_query("retrieval-augmented generation", k=3)
    assert results
    assert any("retrieval-augmented" in item.lower() for item in results)


async def test_collection_management(rag: LiteVectorRAG):
    collection = "temp_collection"

    assert await rag.create_collection(collection)
    assert collection in await rag.get_collection_names()
    await rag.add_documents(["test doc"], collection_name=collection)
    assert await rag.delete_collection(collection)
    assert collection not in await rag.get_collection_names()


async def test_swift_workflow(rag: LiteVectorRAG):
    docs, scores = await rag.swift(
        collection_name="test_collection",
        add_documents=[
            "Machine learning is a subset of AI.",
            "Deep learning uses neural networks.",
        ],
        query="neural networks deep learning",
        K=2,
        threshold=0.0,
    )

    assert len(docs) == 2
    assert len(scores) == 2
    assert "neural networks" in docs[0].lower() or "deep learning" in docs[0].lower()


async def test_empty_query(rag: LiteVectorRAG):
    await rag.add_documents(["some content"], collection_name="test_collection")

    assert await rag.simple_query("") is None
    assert await rag.simple_query("   ") is None


async def test_vectorstore_overview(rag: LiteVectorRAG):
    await rag.add_documents(["doc A", "doc B"], collection_name="test_collection")

    overview = await rag.get_vectorstore_overview()

    assert set(overview) >= {"directory", "total_collections", "total_documents", "collections"}
    assert overview["total_documents"] > 0
