from types import SimpleNamespace
from typing import Any, cast

import pytest

from satrap.core.APICall.EmbedCall import AsyncEmbedding, Embedding, parse_embedding_response


class _SyncEmbeddingEndpoint:
    """按顺序返回响应或抛出异常的同步 Embedding 端点替身"""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.inputs: list[list[str]] = []

    def create(self, **kwargs: Any) -> Any:
        """记录输入并返回下一项预设结果"""
        self.inputs.append(cast(list[str], kwargs["input"]))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _AsyncEmbeddingEndpoint(_SyncEmbeddingEndpoint):
    """按顺序返回响应或抛出异常的异步 Embedding 端点替身"""

    async def create(self, **kwargs: Any) -> Any:
        """记录输入并异步返回下一项预设结果"""
        self.inputs.append(cast(list[str], kwargs["input"]))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(*items: tuple[int, list[Any]]) -> SimpleNamespace:
    """构造包含指定 index 和向量的响应替身"""
    return SimpleNamespace(
        data=[SimpleNamespace(index=index, embedding=embedding) for index, embedding in items],
    )


def _sync_embedding(
    endpoint: _SyncEmbeddingEndpoint,
    *,
    max_batch_size: int = 2,
    suppress_error: bool = True,
    return_false: bool = False,
) -> Embedding:
    """构造不访问网络的同步 Embedding 实例"""
    embedding = object.__new__(Embedding)
    embedding.model = "test-model"
    embedding.dimensions = None
    embedding.encoding_format = "float"
    embedding.suppress_error = suppress_error
    embedding.return_false = return_false
    embedding.max_batch_size = max_batch_size
    cast(Any, embedding).client = SimpleNamespace(embeddings=endpoint)
    return embedding


def _async_embedding(
    endpoint: _AsyncEmbeddingEndpoint,
    *,
    max_batch_size: int = 2,
    suppress_error: bool = True,
    return_false: bool = False,
) -> AsyncEmbedding:
    """构造不访问网络的异步 Embedding 实例"""
    embedding = object.__new__(AsyncEmbedding)
    embedding.model = "test-model"
    embedding.dimensions = None
    embedding.encoding_format = "float"
    embedding.suppress_error = suppress_error
    embedding.return_false = return_false
    embedding.max_batch_size = max_batch_size
    cast(Any, embedding).client = SimpleNamespace(embeddings=endpoint)
    return embedding


def test_parse_embedding_response_returns_empty_for_missing_response():
    assert parse_embedding_response(None) == []


def test_parse_embedding_response_orders_object_items_by_index():
    response = SimpleNamespace(
        data=[
            SimpleNamespace(index=1, embedding=[0.1, 0.2, 0.3]),
            SimpleNamespace(index=0, embedding=[0.4, 0.5, 0.6]),
        ],
    )

    assert parse_embedding_response(response) == [
        [0.4, 0.5, 0.6],
        [0.1, 0.2, 0.3],
    ]


def test_parse_embedding_response_supports_mapping_items():
    response: dict[str, Any] = {
        "data": [
            {"index": 1, "embedding": [1.0]},
            {"index": 0, "embedding": [0.0]},
        ],
    }

    assert parse_embedding_response(response) == [[0.0], [1.0]]


def test_sync_embedding_preserves_failed_batch_positions_and_continues():
    """中间批次失败时保留空位并继续处理后续批次"""
    endpoint = _SyncEmbeddingEndpoint([
        _response((0, [1.0, 0.0]), (1, [0.0, 1.0])),
        RuntimeError("batch failed"),
        _response((0, [0.5, 0.5])),
    ])
    embedding = _sync_embedding(endpoint)

    result = embedding.embed(["a", "b", "c", "d", "e"])

    assert result == [[1.0, 0.0], [0.0, 1.0], [], [], [0.5, 0.5]]
    assert endpoint.inputs == [["a", "b"], ["c", "d"], ["e"]]


def test_sync_embedding_preserves_missing_response_index():
    """部分响应缺失时使用空向量保留原输入位置"""
    endpoint = _SyncEmbeddingEndpoint([
        _response((2, [0.0, 1.0]), (0, [1.0, 0.0])),
    ])
    embedding = _sync_embedding(endpoint, max_batch_size=3)

    assert embedding.embed(["a", "b", "c"]) == [[1.0, 0.0], [], [0.0, 1.0]]


def test_sync_embedding_rejects_duplicate_response_index():
    """重复 index 对应的位置失效且不影响其他有效项"""
    endpoint = _SyncEmbeddingEndpoint([
        _response((0, [1.0, 0.0]), (0, [0.5, 0.5]), (1, [0.0, 1.0])),
    ])
    embedding = _sync_embedding(endpoint)

    assert embedding.embed(["a", "b"]) == [[], [0.0, 1.0]]


def test_sync_embedding_rejects_dimension_change_between_batches():
    """后续批次的异常维度使用空向量占位"""
    endpoint = _SyncEmbeddingEndpoint([
        _response((0, [1.0, 0.0])),
        _response((0, [1.0])),
    ])
    embedding = _sync_embedding(endpoint, max_batch_size=1)

    assert embedding.embed(["a", "b"]) == [[1.0, 0.0], []]


def test_sync_embedding_return_false_rejects_partial_result():
    """return_false 启用时部分响应使整个调用返回 False"""
    endpoint = _SyncEmbeddingEndpoint([
        _response((0, [1.0, 0.0])),
    ])
    embedding = _sync_embedding(endpoint, return_false=True)

    assert embedding.embed(["a", "b"]) is False


def test_sync_embedding_unsuppressed_error_is_raised():
    """关闭异常抑制时向调用方传播批次异常"""
    endpoint = _SyncEmbeddingEndpoint([RuntimeError("batch failed")])
    embedding = _sync_embedding(endpoint, suppress_error=False)

    with pytest.raises(RuntimeError, match="batch failed"):
        embedding.embed(["a"])


@pytest.mark.asyncio
async def test_async_embedding_preserves_failed_batch_positions_and_continues():
    """异步调用在中间批次失败后保留空位并继续处理"""
    endpoint = _AsyncEmbeddingEndpoint([
        _response((0, [1.0, 0.0]), (1, [0.0, 1.0])),
        RuntimeError("batch failed"),
        _response((0, [0.5, 0.5])),
    ])
    embedding = _async_embedding(endpoint)

    result = await embedding.embed(["a", "b", "c", "d", "e"])

    assert result == [[1.0, 0.0], [0.0, 1.0], [], [], [0.5, 0.5]]
    assert endpoint.inputs == [["a", "b"], ["c", "d"], ["e"]]


def test_embedding_rejects_non_positive_batch_size():
    """同步和异步构造方法均拒绝非正批次大小"""
    with pytest.raises(ValueError, match="max_batch_size 必须大于 0"):
        Embedding(api_key="test", max_batch_size=0)
    with pytest.raises(ValueError, match="max_batch_size 必须大于 0"):
        AsyncEmbedding(api_key="test", max_batch_size=-1)
