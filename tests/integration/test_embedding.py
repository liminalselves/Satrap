import os

import pytest

from satrap.core.APICall.EmbedCall import Embedding


pytestmark = [pytest.mark.integration, pytest.mark.requires_api]


def test_embedding_roundtrip():
    api_key = os.getenv("TEST_EMBED_API_KEY")
    base_url = os.getenv("TEST_EMBED_BASE_URL")
    if not api_key or not base_url:
        pytest.skip("设置 TEST_EMBED_BASE_URL 和 TEST_EMBED_API_KEY 后运行")

    client = Embedding(
        api_key=api_key,
        base_url=base_url,
        model=os.getenv("TEST_EMBED_MODEL", "BAAI/bge-large-zh-v1.5"),
        dimensions=int(os.getenv("TEST_EMBED_DIMENSIONS", "512")),
        max_batch_size=11,
        suppress_error=False,
    )

    single = client.embed("Hello, world!")
    batch = client.embed(["这是第一句话。", "这是第二句话。"])

    assert isinstance(single, list)
    assert single
    assert isinstance(batch, list)
    assert len(batch) == 2
    assert all(batch_item for batch_item in batch)
    assert client.check_embedding() == len(single)
