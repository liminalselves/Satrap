import asyncio
import os
import tempfile
from pathlib import Path
from satrap.expend.rag import LiteVectorRAG

# 从环境变量读取 API 配置（可选）
BASE_URL = 'https://api.siliconflow.cn/v1'
API_KEY = ""
EMBED_MODEL = "BAAI/bge-large-en-v1.5"  # 默认模型

# 检查必要配置
if not BASE_URL or not API_KEY:
    raise RuntimeError(
        "请设置环境变量 TEST_EMBED_BASE_URL 和 TEST_EMBED_API_KEY 以运行测试。"
    )


async def test_simple_query(rag):
    """测试 simple_query 基础查询"""
    docs = [
        "The quick brown fox jumps over the lazy dog.",
        "A fast brown fox leaps over a sleepy hound.",
        "Python is a popular programming language.",
    ]
    await rag.add_documents(docs, collection_name="test_collection")

    query = "fox jumps over dog"
    results = await rag.simple_query(query, k=2, threshold=0.0)
    assert results is not None, "查询返回 None"
    assert len(results) == 2, f"期望 2 个结果，得到 {len(results)}"
    first = results[0].lower()
    assert "fox" in first and "dog" in first, "最相关文档未命中关键词"
    print("✅ test_simple_query 通过")


async def test_add_documents(rag):
    """测试 add_documents 批量添加和空向量处理"""
    docs = ["doc1", "doc2", "doc3"]
    success = await rag.add_documents(
        docs, collection_name="test_collection", batch_size=2
    )
    assert success, "添加文档失败"
    # 验证数量：可通过统计集合文档数间接验证
    stats = rag.vector_db.get_collection_stats("test_collection")
    assert stats["document_count"] >= 3, f"文档数异常: {stats}"
    print("✅ test_add_documents 通过")


async def test_add_text_file(rag, tmp_path):
    """测试从文本文件添加内容"""
    content = """Line 1: RAG stands for Retrieval-Augmented Generation.
Line 2: It combines retrieval systems with generative models."""
    file_path = tmp_path / "test_rag.txt"
    file_path.write_text(content, encoding="utf-8")

    success = await rag.add_text_file(
        str(file_path),
        collection_name="test_collection",
        chunk_size=30,
        chunk_overlap=5,
    )
    assert success, "从文件添加失败"

    query = "retrieval-augmented generation"
    results = await rag.simple_query(query, k=3)
    assert results, "未检索到任何文档"
    assert any("retrieval-augmented" in r.lower() for r in results), "相关文档未命中"
    print("✅ test_add_text_file 通过")


async def test_collection_management(rag):
    """测试集合的创建、查询和删除"""
    # 创建集合
    new_col = "temp_collection"
    created = await rag.create_collection(new_col)
    assert created, "创建集合失败"

    # 获取所有集合名称
    names = await rag.get_collection_names()
    assert new_col in names, f"新集合 {new_col} 未出现"

    # 添加一点数据
    await rag.add_documents(["test doc"], collection_name=new_col)

    # 删除集合
    deleted = await rag.delete_collection(new_col)
    assert deleted, "删除集合失败"

    # 确认已删除
    names_after = await rag.get_collection_names()
    assert new_col not in names_after, "集合未被删除"
    print("✅ test_collection_management 通过")


async def test_swift_workflow(rag):
    """测试 swift 快速流程：添加并查询"""
    add_docs = [
        "Machine learning is a subset of AI.",
        "Deep learning uses neural networks.",
    ]
    query = "neural networks deep learning"

    docs, scores = await rag.swift(
        collection_name="test_collection",
        add_documents=add_docs,
        query=query,
        K=2,
        threshold=0.0,
    )
    assert len(docs) == 2, f"期望 2 个文档，得到 {len(docs)}"
    assert len(scores) == 2, "分数列表长度错误"
    first = docs[0].lower()
    assert ("neural networks" in first or "deep learning" in first), "最相关文档未命中"
    print("✅ test_swift_workflow 通过")


async def test_empty_query(rag):
    """测试空查询字符串"""
    # 先确保有数据
    await rag.add_documents(["some content"], collection_name="test_collection")

    # 空字符串
    res = await rag.simple_query("")
    assert res is None, "空查询应返回 None"

    # 空白字符串
    res = await rag.simple_query("   ")
    assert res is None, "空白查询应返回 None"
    print("✅ test_empty_query 通过")


async def test_vectorstore_overview(rag):
    """测试获取向量库概览"""
    # 添加一些数据
    await rag.add_documents(["doc A", "doc B"], collection_name="test_collection")

    overview = await rag.get_vectorstore_overview()
    assert "directory" in overview
    assert "total_collections" in overview
    assert "total_documents" in overview
    assert "collections" in overview
    assert overview["total_documents"] > 0, "文档数应为正"
    print("✅ test_vectorstore_overview 通过")


async def run_all_tests():
    """运行所有测试，使用独立的临时目录"""
    with tempfile.TemporaryDirectory(prefix="litevector_rag_test_") as tmpdir:
        rag = LiteVectorRAG(
            base_url=BASE_URL,
            api_key=API_KEY,
            embed_model=EMBED_MODEL,
            persist_directory=tmpdir,
            default_vectorstore_name="test_collection",
            k_default=2,
            chunk_size=50,
            chunk_overlap=10,
            threshold=0.0,          # 阈值设0确保召回全部
            batch_size=2,
        )
        print(f"🚀 开始测试，持久化目录: {tmpdir}")

        # 为需要临时文件的测试准备临时目录
        with tempfile.TemporaryDirectory() as file_tmp:
            file_tmp_path = Path(file_tmp)

            # 依次执行测试
            await test_add_documents(rag)           # 先添加文档
            await test_simple_query(rag)            # 查询
            await test_add_text_file(rag, file_tmp_path)
            await test_collection_management(rag)
            await test_swift_workflow(rag)
            await test_empty_query(rag)
            await test_vectorstore_overview(rag)

        print("\n🎉 所有测试通过！")


if __name__ == "__main__":
    asyncio.run(run_all_tests())