"""模型配置共享领域服务测试"""
from __future__ import annotations

from unittest.mock import Mock
from pathlib import Path
import pytest

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.config.model_service import ModelConfigService
from satrap.edictum.plugin_resources import build_model_client
from satrap.core.APICall.EmbedCall import Embedding
from satrap.core.type import LLMConfig


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
    """
    共享领域服务应完成增删改查并对展示密钥脱敏

    参数:
    - tmp_path: 临时目录
    """
    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    service = ModelConfigService(manager)

    service.create(
        "llm",
        "demo",
        {
            "model": "gpt-demo",
            "api_key": "secret-key",
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


def test_model_config_service_rejects_invalid_input(tmp_path: Path):
    """
    共享领域服务应拒绝未知类型, 字段和脱敏创建密钥

    参数:
    - tmp_path: 临时目录
    """
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
    with pytest.raises(ValueError, match="context_strategy"):
        service.create("llm", "demo", {"context_strategy": "unknown"})
    with pytest.raises(ValueError, match="truncation_floor"):
        service.create(
            "llm",
            "demo",
            {"context_threshold": 0.4, "truncation_floor": 0.8},
        )
    with pytest.raises(ValueError, match="summary_keep_recent_turns"):
        service.create("llm", "demo", {"summary_keep_recent_turns": -1})
    with pytest.raises(ValueError, match="context_threshold"):
        service.create("llm", "demo", {"context_threshold": "0.8"})
    with pytest.raises(ValueError, match="context_window"):
        service.create("llm", "demo", {"context_window": True})


def test_model_config_service_rejects_rename_collision(tmp_path: Path):
    """
    共享领域服务应拒绝重命名为已有配置

    参数:
    - tmp_path: 临时目录
    """
    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    service = ModelConfigService(manager)
    service.create("embedding", "first", {"model": "first-model"})
    service.create("embedding", "second", {"model": "second-model"})

    with pytest.raises(ValueError, match="名称已存在"):
        service.update("embedding", "first", {"name": "second"})

    assert manager.get_embedding_config("first").model == "first-model"
    assert manager.get_embedding_config("second").model == "second-model"


def test_masking_does_not_depend_on_lock_api_key(tmp_path: Path) -> None:
    """
    展示接口即使允许运行时更新 API Key 也必须始终脱敏

    参数:
    - tmp_path: 临时目录
    """
    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    service = ModelConfigService(manager)
    service.create("llm", "unlocked", {
        "model": "demo",
        "api_key": "unlocked-secret-key",
        "lock_api_key": False,
    })

    listed = service.list_configs("llm")
    assert listed["unlocked"]["api_key"] != "unlocked-secret-key"
    assert listed["unlocked"]["api_key"].endswith("-key")


def test_dump_uses_storage_key_as_config_name(tmp_path: Path):
    """
    序列化输出的 name 字段恒为存储键 (写入时已强制对齐)

    参数:
    - tmp_path: 临时目录
    """
    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    manager.set_llm_config(LLMConfig(name="original", model="gpt-demo"), name="alias")

    listed = manager.list_llm_configs(mask_api_key=False)

    assert listed["alias"]["name"] == "alias"


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
    fake.client.close.assert_awaited_once()
