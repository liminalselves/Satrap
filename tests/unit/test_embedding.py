from types import SimpleNamespace
from typing import Any

from satrap.core.APICall.EmbedCall import parse_embedding_response


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
