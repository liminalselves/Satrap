"""模型只读捕获的副本, 旧格式及损坏来源契约"""
from pathlib import Path

import pytest

from satrap.core.config.model_snapshot import ModelConfigSnapshot, llm_config_fingerprint
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.type import ASRConfig, EmbeddingConfig, LLMConfig, ReRankConfig
from satrap.edictum.plugin_config import parse_config_schema
from satrap.edictum.plugin_resources import PluginResources, model_reference_fingerprint


def test_read_snapshot_does_not_reload_write_or_mutate_copies(tmp_path: Path, monkeypatch) -> None:
    """
    捕获四类模型不会回写文件, 配置与资源均只读独立副本

    参数:
    - tmp_path: 隔离来源
    - monkeypatch: 禁止回写夹具
    """
    manager = ModelConfigManager(tmp_path / "models.json")
    manager.set_llm_config(LLMConfig(model="chat", api_key="test", thinking_fields=["reasoning_effort"]), "chat")
    manager.set_embedding_config(EmbeddingConfig(model="embed", api_key="test"), "embed")
    manager.set_rerank_config(ReRankConfig(model="rank", api_key="test", base_url="http://localhost:1"), "rank")
    manager.set_asr_config(ASRConfig(model="voice", api_key="test"), "voice")
    before = manager.storage_path.read_bytes()
    modified_at = manager.storage_path.stat().st_mtime_ns

    def forbidden():
        """只读捕获不得刷新或保存原管理器"""
        raise AssertionError("只读捕获发生回写")

    monkeypatch.setattr(manager, "reload", forbidden)
    monkeypatch.setattr(manager, "save", forbidden)
    snapshot = ModelConfigSnapshot.read(manager)
    copy = snapshot.get_llm_config("chat")
    first = llm_config_fingerprint(copy)
    copy.model = "outside"
    if copy.thinking_fields is not None:
        copy.thinking_fields.append("outside")
    assert snapshot.get_llm_config("chat").thinking_fields == ["reasoning_effort"]
    assert llm_config_fingerprint(snapshot.get_llm_config("chat")) == first
    assert snapshot.get_embedding_config("embed").model == "embed"
    assert snapshot.get_rerank_config("rank").model == "rank"
    assert snapshot.get_asr_config("voice").model == "voice"
    assert manager.storage_path.read_bytes() == before and manager.storage_path.stat().st_mtime_ns == modified_at
    assert not snapshot.has_config("llm", "missing") and snapshot.get_llm_config("missing").api_key is None
    assert not hasattr(snapshot, "save") and not hasattr(snapshot, "reload")
    schema = parse_config_schema({"config_schema": {"llm": {"type": "llm"}}})
    resources = PluginResources(snapshot, schema, {"llm": "chat"}, async_=False)
    assert resources._configs["llm"][1].model == "chat"
    assert model_reference_fingerprint(snapshot, schema, {"llm": "chat"})
    assert resources._clients == {}


@pytest.mark.parametrize("content", ["broken", "[]", '{"llm":[]}', '{"llm":{"bad":5}}', '{"llm":{"bad":{"supports_visual_input":"yes"}}}'])
def test_snapshot_rejects_corrupt_content_without_rewriting(tmp_path: Path, content: str) -> None:
    """
    损坏来源不成为默认模型, 不修写用户文件

    参数:
    - tmp_path: 隔离来源
    - content: 损坏内容
    """
    manager = ModelConfigManager(tmp_path / "models.json")
    manager.storage_path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        ModelConfigSnapshot.read(manager)
    assert manager.storage_path.read_text(encoding="utf-8") == content


def test_snapshot_supports_legacy_single_config_and_missing_source_is_error(tmp_path: Path) -> None:
    """
    旧单模型格式沿用同一解析语义, 来源消失明确抛出

    参数:
    - tmp_path: 隔离来源
    """
    manager = ModelConfigManager(tmp_path / "models.json")
    content = '{"llm":{"model":"legacy","api_key":"test"}}'
    manager.storage_path.write_text(content, encoding="utf-8")
    snapshot = ModelConfigSnapshot.read(manager)
    assert snapshot.get_llm_config().model == "legacy"
    assert manager.storage_path.read_text(encoding="utf-8") == content
    manager.storage_path.unlink()
    with pytest.raises(FileNotFoundError):
        ModelConfigSnapshot.read(manager)
    assert not manager.storage_path.exists()
