"""业务链路场景与集中边界契约"""
from __future__ import annotations

from unittest.mock import Mock
from pathlib import Path
import json
import pytest
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.config.model_service import ModelConfigService
from satrap.edictum.plugin_resources import build_model_client
from satrap.core.APICall.EmbedCall import Embedding
from satrap.core.type import LLMConfig
from satrap.core.config.model_snapshot import ModelConfigSnapshot, llm_config_fingerprint
from satrap.core.type import ASRConfig, EmbeddingConfig, ReRankConfig
from satrap.edictum.plugin_config import parse_config_schema
from satrap.edictum.plugin_resources import PluginResources, model_reference_fingerprint
from typing import Any
from satrap.core.APICall.LLMCall import AsyncLLM, LLM, build_llm_from_config

def test_legacy_model_config_migrates_once_then_reload_preserves_file(tmp_path: Path) -> None:
    """旧格式仍迁移为命名配置, 后续读取不重复写回"""
    path = tmp_path / 'models.json'
    legacy = {'llm': {'model': 'legacy-model', 'api_key': 'test-secret'}}
    path.write_text(json.dumps(legacy), encoding='utf-8')
    manager = ModelConfigManager(storage_path=path)
    normalized = json.loads(path.read_text(encoding='utf-8'))
    assert normalized['llm']['default']['model'] == 'legacy-model'
    assert normalized['llm']['default']['api_key'] == 'test-secret'
    initial = (path.read_bytes(), path.stat().st_mtime_ns)
    for _ in range(3):
        manager.reload()
        reloaded = ModelConfigManager(storage_path=path)
        assert reloaded.get_llm_config().model == 'legacy-model'
        assert (path.read_bytes(), path.stat().st_mtime_ns) == initial
    manager.update_llm_config(model='updated-model')
    assert ModelConfigManager(storage_path=path).get_llm_config().model == 'updated-model'

def test_clear_embedding_dimensions_persists_and_omits_request_parameter(tmp_path: Path) -> None:
    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    service = ModelConfigService(manager)
    service.create("embedding", "demo", {"model": "embedding-model", "api_key": "test-key", "dimensions": 1024})
    service.update("embedding", "demo", {"dimensions": None})
    manager.reload()
    config = manager.get_embedding_config("demo")
    assert config.dimensions is None
    assert config.api_key == "test-key"
    client = build_model_client("embed", config)
    assert isinstance(client, Embedding)
    endpoint = Mock(return_value={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})
    client.client.embeddings.create = endpoint
    try:
        assert client.embed("测试文本") == [1.0, 0.0]
        assert "dimensions" not in endpoint.call_args.kwargs
    finally:
        client.client.close()

def test_model_config_service_crud_and_masking(tmp_path: Path):
    """共享领域服务应完成增删改查并对展示密钥脱敏"""
    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    service = ModelConfigService(manager)

    service.create(
        "llm",
        "demo",
        {
            "model": "gpt-demo",
            "api_key": "secret-key",
            "lock_api_key": False,
            "temperature": 0.5,
            "thinking_fields": ["thinking.type", "reasoning_effort"],
            "thinking_levels": ["low", "high", "xhigh"],
            "omit_none_thinking_fields": True,
            "context_window": 64000,
            "history_ratio": 0.75,
            "context_strategy": "summarize",
            "context_threshold": 0.85,
            "truncation_floor": 0.35,
            "summary_keep_recent_turns": 4,
        },
    )
    listed = service.list_configs("llm")

    assert listed["demo"]["model"] == "gpt-demo"
    # 展示接口即使允许运行时更新 API Key 也必须始终脱敏
    assert listed["demo"]["api_key"] != "secret-key"
    assert str(listed["demo"]["api_key"]).endswith("-key")
    assert listed["demo"]["thinking_fields"] == ["thinking.type", "reasoning_effort"]
    assert listed["demo"]["thinking_levels"] == ["low", "high", "xhigh"]
    assert listed["demo"]["omit_none_thinking_fields"] is True
    assert listed["demo"]["context_strategy"] == "summarize"
    assert listed["demo"]["context_threshold"] == 0.85
    assert listed["demo"]["truncation_floor"] == 0.35
    assert listed["demo"]["summary_keep_recent_turns"] == 4

    service.update(
        "llm",
        "demo",
        {"name": "renamed", "temperature": 0.2, "api_key": listed["demo"]["api_key"]},
    )

    runtime = manager.get_llm_config("renamed")
    assert runtime.temperature == 0.2
    assert runtime.api_key == "secret-key"
    assert manager.has_config("llm", "demo") is False
    assert service.delete("llm", "renamed") is True
    assert manager.has_config("llm", "renamed") is False
    # 序列化输出的 name 字段恒为存储键 (写入时已强制对齐)
    manager.set_llm_config(LLMConfig(name="original", model="gpt-demo"), name="alias")
    assert manager.list_llm_configs(mask_api_key=False)["alias"]["name"] == "alias"

def test_model_config_service_rejects_invalid_input(tmp_path: Path):
    """共享领域服务应拒绝未知类型, 字段和脱敏创建密钥 (上下文策略校验收敛在 context_policy 侧)"""
    service = ModelConfigService(ModelConfigManager(storage_path=tmp_path / "models.json"))

    with pytest.raises(ValueError, match="未知模型类型"):
        service.list_configs("unknown")
    with pytest.raises(ValueError, match="未知模型配置字段"):
        service.create("llm", "demo", {"unknown": True})
    with pytest.raises(ValueError, match="未知模型配置字段"):
        service.create("llm", "demo", {"reasoning_body": {}})
    with pytest.raises(ValueError, match="不支持的思考强度"):
        service.create("llm", "demo", {"thinking_levels": ["extreme"]})
    with pytest.raises(ValueError, match="脱敏"):
        service.create("llm", "demo", {"api_key": "*****abcd"})

def test_model_config_service_rejects_rename_collision(tmp_path: Path):
    """共享领域服务应拒绝重命名为已有配置"""
    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    service = ModelConfigService(manager)
    service.create("embedding", "first", {"model": "first-model"})
    service.create("embedding", "second", {"model": "second-model"})

    with pytest.raises(ValueError, match="名称已存在"):
        service.update("embedding", "first", {"name": "second"})

    assert manager.get_embedding_config("first").model == "first-model"
    assert manager.get_embedding_config("second").model == "second-model"

def test_asr_config_crud_masking_and_client_build(tmp_path: Path):
    """ASR 作为第四类模型配置: 增删改查, 脱敏读取, 由配置构造客户端"""
    from satrap.core.APICall.ASRCall import ASR, build_asr_from_config

    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    service = ModelConfigService(manager)
    service.create("asr", "speech", {"model": "whisper-1", "api_key": "asr-secret", "base_url": "https://asr.example.com/v1", "language": "zh"})
    listed = service.list_configs("asr")
    assert "speech" in listed and listed["speech"]["api_key"] != "asr-secret"
    manager.reload()
    config = manager.get_asr_config("speech")
    assert (config.model, config.api_key, config.language) == ("whisper-1", "asr-secret", "zh")
    service.update("asr", "speech", {"language": "en", "timeout": 30})
    manager.reload()
    config = manager.get_asr_config("speech")
    assert (config.language, config.timeout, config.api_key) == ("en", 30, "asr-secret")
    client = build_asr_from_config(config)
    assert isinstance(client, ASR) and client.model == "whisper-1" and client.language == "en"
    client.client.close()
    assert service.delete("asr", "speech") is True
    assert not manager.has_config("asr", "speech")

@pytest.mark.asyncio
async def test_asr_test_endpoint_uses_saved_secret_and_rejects_bad_input(tmp_path: Path):
    """转录测试读取已保存密钥, 拒绝空音频/超限/缺配置, 并关闭客户端"""
    from unittest.mock import AsyncMock
    from satrap.core.config import model_service as module
    from satrap.core.type import ASRResponse

    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    service = ModelConfigService(manager)
    with pytest.raises(ValueError, match="不存在"):
        await service.test_asr_config("missing", "a.wav", b"x")
    service.create("asr", "speech", {"model": "whisper-1", "api_key": "asr-secret"})
    with pytest.raises(ValueError, match="为空"):
        await service.test_asr_config("speech", "a.wav", b"")
    with pytest.raises(ValueError, match="上限"):
        await service.test_asr_config("speech", "a.wav", b"x" * (module.ASR_TEST_MAX_AUDIO_BYTES + 1))

    seen: dict[str, object] = {}

    class _FakeClient:
        suppress_error = True

        def __init__(self) -> None:
            self.client = AsyncMock()

        async def transcribe(self, audio: bytes, filename: str | None = None) -> ASRResponse:
            seen["audio"], seen["filename"], seen["suppress"] = audio, filename, self.suppress_error
            return ASRResponse(text="你好", model="whisper-1", language="zh", duration=1.5)

    fake = _FakeClient()

    def _build(cfg: object, *, async_: bool = False) -> _FakeClient:
        seen["api_key"] = getattr(cfg, "api_key", None)
        return fake

    original = module.build_asr_from_config
    module.build_asr_from_config = _build
    try:
        result = await service.test_asr_config("speech", "clip.wav", b"RIFF")
    finally:
        module.build_asr_from_config = original
    assert result["text"] == "你好" and result["model"] == "whisper-1" and result["duration"] == 1.5
    assert isinstance(result["elapsed_ms"], int)
    assert seen == {"api_key": "asr-secret", "audio": b"RIFF", "filename": "clip.wav", "suppress": False}
    assert "converted_from" not in result
    fake.client.close.assert_awaited_once()

@pytest.mark.asyncio
async def test_asr_test_endpoint_probes_and_converts_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """测试音频按魔数判定: SILK 明确拒绝, amr 本地转码后附 converted_from, 未知格式拒绝"""
    from unittest.mock import AsyncMock
    from satrap.core.config import model_service as module
    from satrap.core.pipeline import audio_convert
    from satrap.core.type import ASRResponse

    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    service = ModelConfigService(manager)
    service.create("asr", "speech", {"model": "whisper-1", "api_key": "asr-secret"})
    with pytest.raises(ValueError, match="SILK"):
        await service.test_asr_config("speech", "a.amr", b"#!SILK_V3" + bytes(8))
    with pytest.raises(ValueError, match="无法识别"):
        await service.test_asr_config("speech", "a.bin", bytes(16))
    seen: dict[str, object] = {}

    class _FakeClient:
        suppress_error = True
        client = AsyncMock()

        async def transcribe(self, audio: bytes, filename: str | None = None) -> ASRResponse:
            seen["audio"], seen["filename"] = audio, filename
            return ASRResponse(text="ok", model="whisper-1", language="zh", duration=1.0)

    def _build(cfg: object, *, async_: bool = False) -> _FakeClient:
        return _FakeClient()

    def _convert(data: bytes, *, max_seconds: float) -> bytes:
        return b"RIFFconverted"

    monkeypatch.setattr(module, "build_asr_from_config", _build)
    monkeypatch.setattr(module, "convert_to_wav", _convert)
    result = await service.test_asr_config("speech", "a.amr", b"#!AMR\n" + bytes(8))
    assert result["converted_from"] == "amr" and seen == {"audio": b"RIFFconverted", "filename": "test.wav"}
    monkeypatch.setitem(audio_convert._AV_STATE, "module", None)
    with pytest.raises(ValueError, match="pip install av"):
        await service.test_asr_config("speech", "a.amr", b"#!AMR\n" + bytes(8))

def test_read_snapshot_does_not_reload_write_or_mutate_copies(tmp_path: Path, monkeypatch) -> None:
    """捕获四类模型不会回写文件, 配置与资源均只读独立副本"""
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
    """损坏来源不成为默认模型, 不修写用户文件"""
    manager = ModelConfigManager(tmp_path / "models.json")
    manager.storage_path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        ModelConfigSnapshot.read(manager)
    assert manager.storage_path.read_text(encoding="utf-8") == content

def test_snapshot_supports_legacy_single_config_and_missing_source_is_error(tmp_path: Path) -> None:
    """旧单模型格式沿用同一解析语义, 来源消失明确抛出"""
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

def _cfg(**kw: Any) -> LLMConfig:
    return LLMConfig(
        name=kw.pop("name", "m"),
        model=kw.pop("model", "mod"),
        base_url=kw.pop("base_url", "https://x/v1"),
        api_key=kw.pop("api_key", "k"),
        **kw,
    )

@pytest.mark.parametrize("asynchronous", [False, True])
def test_maps_all_fields(asynchronous: bool):
    """全部字段透传 (同步/异步构造), lock_api_key=False 时不锁定 api_key"""
    llm = build_llm_from_config(
        _cfg(
            temperature=0.3,
            top_p=0.9,
            max_tokens=512,
            lock_api_key=False,
            thinking_field_name="reasoning",
            thinking_fields=["reasoning_effort"],
            omit_none_thinking_fields=True,
        ),
        async_=asynchronous,
    )
    assert isinstance(llm, AsyncLLM if asynchronous else LLM)
    assert llm.temperature == 0.3
    assert llm.top_p == 0.9
    assert llm.max_tokens == 512
    assert llm.api_key == "k"
    assert llm.thinking_field_name == "reasoning"
    assert llm.thinking_fields == ["reasoning_effort"]
    assert llm.omit_none_thinking_fields is True

def test_output_budget_wins_over_max_tokens():
    """context_window + history_ratio 配置时输出预算优先于 max_tokens"""
    llm = build_llm_from_config(_cfg(context_window=128000, history_ratio=0.7, max_tokens=512))
    assert isinstance(llm, LLM)
    assert llm.max_tokens == int(128000 * 0.3)

def test_max_tokens_fallback_4096():
    """无输出预算且未配置 max_tokens 时回退 4096"""
    llm = build_llm_from_config(_cfg())
    assert isinstance(llm, LLM)
    assert llm.max_tokens == 4096

def test_max_tokens_zero_preserved():
    """显式 max_tokens=0 不被 or 吞掉"""
    llm = build_llm_from_config(_cfg(max_tokens=0))
    assert isinstance(llm, LLM)
    assert llm.max_tokens == 0
