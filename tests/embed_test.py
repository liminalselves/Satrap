from satrap.core.ApiCall.EmbedCall import Embedding, parse_embedding_response
import os, sys

# 将项目根目录添加到 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 示例: 初始化 Embedding 客户端 (请替换为实际的 key)
embed_client = Embedding(
    api_key="", 
    base_url="https://api.siliconflow.cn/v1",
    model="BAAI/bge-large-zh-v1.5",
    dimensions=512,
    suppress_error=True,
    max_batch_size=11
)


def test_parse_embedding_response():
    """测试 parse_embedding_response 函数"""
    
    # 测试空响应
    result = parse_embedding_response(None, suppress_error=True)
    print("空响应测试:", result)
    assert result == [], f"预期 [], 实际 {result}"
    
    # 测试模拟的响应对象
    class MockEmbeddingItem:
        def __init__(self, index, embedding):
            self.index = index
            self.embedding = embedding
    
    class MockResponse:
        def __init__(self, data):
            self.data = data
    
    # 模拟正常响应
    mock_data = [
        MockEmbeddingItem(index=1, embedding=[0.1, 0.2, 0.3]),
        MockEmbeddingItem(index=0, embedding=[0.4, 0.5, 0.6]),
    ]
    mock_response = MockResponse(data=mock_data)
    
    result = parse_embedding_response(mock_response)
    print("模拟响应测试:", result)
    # 应该按 index 排序
    assert len(result) == 2, f"预期 2 个 embedding, 实际 {len(result)}"
    assert result[0] == [0.4, 0.5, 0.6], f"预期第一个 embedding 为 [0.4, 0.5, 0.6]"
    assert result[1] == [0.1, 0.2, 0.3], f"预期第二个 embedding 为 [0.1, 0.2, 0.3]"
    
    print("parse_embedding_response 所有测试通过!")


def main():
    """主测试函数"""
    
    # 测试 parse_embedding_response
    print("=" * 50)
    print("测试 parse_embedding_response")
    print("=" * 50)
    test_parse_embedding_response()
    
    # 测试 Embedding 类
    print("\n" + "=" * 50)
    print("测试 Embedding 类")
    print("=" * 50)
    
    # 单个文本嵌入
    single_text = "Hello, world!"
    print(f"\n单个文本嵌入测试: '{single_text}'")
    embedding = embed_client.embed(single_text)
    print(f"返回类型: {type(embedding)}")
    print(f"向量维度: {len(embedding) if embedding else 'N/A'}")   # type: ignore
    
    # 批量文本嵌入
    texts = [
        "这是第一句话。",
        "这是第二句话。",
        "这是第三句话。",
    ]
    print(f"\n批量文本嵌入测试: {len(texts)} 个文本")
    embeddings = embed_client.embed(texts)
    print(f"返回类型: {type(embeddings)}")
    print(f"返回数量: {len(embeddings) if embeddings else 'N/A'}")   # type: ignore
    if embeddings and len(embeddings) > 0:   # type: ignore
        print(f"每个向量维度: {len(embeddings[0])}")   # type: ignore
    
    # 测试空输入
    print("\n空输入测试:")
    empty_result = embed_client.embed([])
    print(f"空列表输入返回: {empty_result}")
    
    print("\n所有测试完成!")


    print("=====")

if __name__ == "__main__":
    main()
    print(embed_client.check_embedding())