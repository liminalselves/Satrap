"""模型引用注入与旧插件工厂兼容性"""
from unittest.mock import Mock
import pytest

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.edictum.plugin_resources import PluginResources, model_reference_fingerprint
from satrap.core.APICall.ReRankCall import parse_rerank_result, ReRank, AsyncReRank
from satrap.core.APICall.EmbedCall import Embedding, AsyncEmbedding
from satrap.edictum.plugin_config import parse_config_schema
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.edictum.plugin import _invoke_factory
from satrap.core.type import EmbeddingConfig, LLMConfig, ReRankConfig


def test_factory_internal_type_error_is_not_retried():
    calls = []

    def factory(session, config=None):
        calls.append(config)
        raise TypeError("内部错误")

    with pytest.raises(TypeError, match="内部错误"):
        _invoke_factory(factory, object(), {"x": 1}, object())
    assert calls == [{"x": 1}]


def test_factory_supports_old_and_new_signatures():
    session, resources = object(), object()
    assert _invoke_factory(lambda: 1, session, {}, resources) == 1
    assert _invoke_factory(lambda s: s, session, {}, resources) is session
    assert _invoke_factory(lambda s, c: c, session, {"a": 1}, resources) == {"a": 1}
    assert _invoke_factory(lambda s, c, *, resources: resources, session, {}, resources) is resources


def test_model_reference_validation_and_revision(tmp_path):
    manager = ModelConfigManager(tmp_path / "models.json", auto_create=False)
    schema = parse_config_schema({"config_schema": {"embed": {"type": "embed", "required": True}}})
    with pytest.raises(ValueError, match="必填"):
        PluginResources(manager, schema, {"embed": ""}, async_=False)
    with pytest.raises(ValueError, match="不存在"):
        PluginResources(manager, schema, {"embed": "missing"}, async_=False)
    with pytest.raises(ValueError, match="缺少"):
        PluginResources(manager, schema, {"embed": "default"}, async_=False)
    manager.set_embedding_config(EmbeddingConfig(model="embedding-a", api_key="test", dimensions=3), "test")
    first = model_reference_fingerprint(manager, schema, {"embed": "test"})
    manager.set_embedding_config(EmbeddingConfig(model="embedding-b", api_key="test", dimensions=3), "test")
    assert first != model_reference_fingerprint(manager, schema, {"embed": "test"})


def test_lazy_resources_share_within_plugin_and_close(tmp_path, monkeypatch):
    manager = ModelConfigManager(tmp_path / "models.json", auto_create=False)
    manager.set_embedding_config(EmbeddingConfig(model="test", api_key="test"), "test")
    schema = parse_config_schema({"config_schema": {"embed": {"type": "embed"}}})
    client = Mock()
    build = Mock(return_value=client)
    monkeypatch.setattr("satrap.edictum.plugin_resources.build_model_client", build)
    resources = PluginResources(manager, schema, {"embed": "test"}, async_=False)
    build.assert_not_called()
    assert resources["embed"] is resources.get("embed")
    build.assert_called_once()
    resources.close()
    client.client.close.assert_called_once()


@pytest.mark.parametrize("payload", [{}, {"results": {}}, {"results": [None]}, {"results": [{"index": 0, "relevance_score": float("nan")}]}, {"results": [{"index": True, "relevance_score": 1}]}])
def test_strict_rerank_rejects_malformed_responses(payload):
    with pytest.raises(ValueError):
        parse_rerank_result(payload, suppress_error=False)


def test_strict_rerank_accepts_empty_and_filters_scores():
    assert parse_rerank_result({"results": []}, suppress_error=False) == []
    result = parse_rerank_result({"results": [{"index": 0, "relevance_score": 0.1}, {"index": 1, "relevance_score": 0.8}]}, min_score=0.5, suppress_error=False)
    assert result == [{"text": "", "score": 0.8, "original_index": 1}]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_all_reference_types_construct_matching_clients_without_network(tmp_path, asynchronous):
    manager = ModelConfigManager(tmp_path / "models.json")
    manager.set_llm_config(LLMConfig(model="chat", api_key="test", base_url="http://localhost:1234"), "chat")
    manager.set_embedding_config(EmbeddingConfig(model="embed", api_key="test", base_url="http://localhost:1234", dimensions=3, max_batch_size=4), "embed")
    manager.set_rerank_config(ReRankConfig(model="rank", api_key="test", base_url="http://localhost:1234", top_k=9), "rank")
    schema = parse_config_schema({"config_schema": {key: {"type": key} for key in ("llm", "embed", "rerank")}})
    resources = PluginResources(manager, schema, {"llm": "chat", "embed": "embed", "rerank": "rank"}, async_=asynchronous)
    try:
        assert isinstance(resources["llm"], AsyncLLM if asynchronous else LLM)
        assert isinstance(resources["embed"], AsyncEmbedding if asynchronous else Embedding)
        assert isinstance(resources["rerank"], AsyncReRank if asynchronous else ReRank)
        assert resources["embed"].dimensions == 3
        assert resources["embed"].max_batch_size == 4
        assert resources["rerank"].top_k == 9
    finally:
        if asynchronous:
            await resources.aclose()
        else:
            resources.close()
    assert resources._clients == {}
