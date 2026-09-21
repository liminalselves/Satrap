"""ASR 同步与异步入口的输入校验, 请求构造与错误语义契约"""
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openai import APIError

from satrap.core.APICall.ASRCall import (
    ALLOWED_AUDIO_SUFFIXES,
    ASR,
    AsyncASR,
    build_asr_from_config,
)
from satrap.core.APICall.ASRCall import async_ as async_module
from satrap.core.APICall.ASRCall import sync as sync_module
from satrap.core.APICall.ASRCall.utils import MAX_AUDIO_BYTES, prepare_audio_input
from satrap.core.type import ASRConfig, ASRResponse


def _clients(monkeypatch: pytest.MonkeyPatch, error: Exception | None = None, response: Any = None):
    """替换模块级 SDK 符号, 记录请求并返回固定转录响应"""
    calls: list[list[dict[str, Any]]] = [[], []]
    payload = response if response is not None else SimpleNamespace(text="你好", language="zh", duration=1.5)

    def create(**kwargs: Any) -> Any:
        calls[0].append(kwargs)
        if error is not None:
            raise error
        return payload

    async def acreate(**kwargs: Any) -> Any:
        calls[1].append(kwargs)
        if error is not None:
            raise error
        return payload

    def _fake_sync_client(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))

    def _fake_async_client(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=acreate)))

    monkeypatch.setattr(sync_module, "OpenAI", _fake_sync_client)
    monkeypatch.setattr(async_module, "AsyncOpenAI", _fake_async_client)
    return calls


def _pair(monkeypatch: pytest.MonkeyPatch, **options: Any):
    calls = _clients(monkeypatch, **{k: v for k, v in options.items() if k in {"error", "response"}})
    kwargs = {k: v for k, v in options.items() if k not in {"error", "response"}}
    return (
        ASR(api_key="test", model="whisper-1", **kwargs),
        AsyncASR(api_key="test", model="whisper-1", **kwargs),
        calls,
    )


def test_prepare_audio_input_accepts_path_and_bytes(tmp_path: Path):
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"fake-audio")
    name, data, mime = prepare_audio_input(audio)
    assert (name, data, mime) == ("voice.mp3", b"fake-audio", "audio/mpeg")
    name, data, mime = prepare_audio_input(b"fake-audio", filename="clip.wav")
    assert (name, data, mime) == ("clip.wav", b"fake-audio", "audio/wav")


def test_prepare_audio_input_rejects_bad_inputs(tmp_path: Path):
    with pytest.raises(ValueError, match="不存在"):
        prepare_audio_input(tmp_path / "missing.mp3")
    with pytest.raises(ValueError, match="文件名"):
        prepare_audio_input(b"data")
    with pytest.raises(ValueError, match="路径成分"):
        prepare_audio_input(b"data", filename="a/b.mp3")
    with pytest.raises(ValueError, match="不支持的音频格式"):
        prepare_audio_input(b"data", filename="voice.silk")
    with pytest.raises(ValueError, match="为空"):
        prepare_audio_input(b"", filename="voice.mp3")
    big = tmp_path / "big.wav"
    big.write_bytes(b"x" * (MAX_AUDIO_BYTES + 1))
    with pytest.raises(ValueError, match="上限"):
        prepare_audio_input(big)
    assert ".silk" not in ALLOWED_AUDIO_SUFFIXES and ".mp3" in ALLOWED_AUDIO_SUFFIXES


def test_sync_transcribe_builds_openai_request(monkeypatch: pytest.MonkeyPatch):
    asr, _, calls = _pair(monkeypatch, language="zh", prompt="提示")
    result = asr.transcribe(b"audio-bytes", filename="voice.ogg")
    assert isinstance(result, ASRResponse)
    assert (result.text, result.model, result.language, result.duration) == ("你好", "whisper-1", "zh", 1.5)
    request = calls[0][0]
    assert request["file"][0] == "voice.ogg" and request["file"][2] == "audio/ogg"
    assert request["model"] == "whisper-1" and request["response_format"] == "json"
    assert request["language"] == "zh" and request["prompt"] == "提示"


@pytest.mark.asyncio
async def test_async_transcribe_parity_with_sync(monkeypatch: pytest.MonkeyPatch):
    _, async_asr, calls = _pair(monkeypatch, language="zh")
    result = await async_asr.transcribe(b"audio-bytes", filename="voice.ogg", model="other-model")
    assert result is not None and result.text == "你好" and result.model == "other-model"
    request = calls[1][0]
    assert request["model"] == "other-model" and request["language"] == "zh"
    assert "prompt" not in request


def test_optional_parameters_omitted_when_empty(monkeypatch: pytest.MonkeyPatch):
    asr, _, calls = _pair(monkeypatch)
    asr.transcribe(b"audio-bytes", filename="voice.mp3", language="", prompt="")
    request = calls[0][0]
    assert "language" not in request and "prompt" not in request


def test_dict_response_shape_and_empty_text_kept(monkeypatch: pytest.MonkeyPatch):
    asr, _, _ = _pair(monkeypatch, response={"text": "", "language": "en"})
    result = asr.transcribe(b"audio-bytes", filename="voice.mp3")
    assert result is not None and result.text == "" and result.language == "en" and result.duration == 0.0


def test_api_error_suppressed_or_raised(monkeypatch: pytest.MonkeyPatch):
    import httpx
    error = APIError("boom", request=httpx.Request("POST", "http://x"), body=None)
    asr, _, _ = _pair(monkeypatch, error=error)
    assert asr.transcribe(b"audio-bytes", filename="voice.mp3") is None
    strict, _, _ = _pair(monkeypatch, error=error, suppress_error=False)
    with pytest.raises(APIError):
        strict.transcribe(b"audio-bytes", filename="voice.mp3")


@pytest.mark.asyncio
async def test_async_api_error_suppressed_or_raised(monkeypatch: pytest.MonkeyPatch):
    import httpx
    error = APIError("boom", request=httpx.Request("POST", "http://x"), body=None)
    _, async_asr, _ = _pair(monkeypatch, error=error)
    assert await async_asr.transcribe(b"audio-bytes", filename="voice.mp3") is None
    _, strict, _ = _pair(monkeypatch, error=error, suppress_error=False)
    with pytest.raises(APIError):
        await strict.transcribe(b"audio-bytes", filename="voice.mp3")


def test_invalid_input_suppressed_or_raised(monkeypatch: pytest.MonkeyPatch):
    asr, _, calls = _pair(monkeypatch)
    assert asr.transcribe(b"data", filename="voice.silk") is None
    strict, _, _ = _pair(monkeypatch, suppress_error=False)
    with pytest.raises(ValueError, match="不支持的音频格式"):
        strict.transcribe(b"data", filename="voice.silk")
    assert calls == [[], []]


def test_build_asr_from_config_maps_fields(monkeypatch: pytest.MonkeyPatch):
    _clients(monkeypatch)
    cfg = ASRConfig(name="a", model="whisper-1", base_url="https://api.example.com/v1/", api_key="k",
                    language="zh", prompt="p", timeout=30)
    asr = build_asr_from_config(cfg)
    assert isinstance(asr, ASR)
    assert asr.model == "whisper-1" and asr.get_base_url() == "https://api.example.com/v1"
    assert asr.language == "zh" and asr.prompt == "p"
    assert asr.get_api_key() == "api key locked"
    async_asr = build_asr_from_config(cfg, async_=True)
    assert isinstance(async_asr, AsyncASR)


def test_build_asr_from_config_omits_none_defaults(monkeypatch: pytest.MonkeyPatch):
    _clients(monkeypatch)
    asr = build_asr_from_config(ASRConfig(model="m"))
    assert asr.language == "" and asr.prompt == ""


def test_insecure_base_url_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch):
    _clients(monkeypatch)
    with pytest.raises(ValueError, match="https"):
        ASR(api_key="k", base_url="http://8.8.8.8:8080/v1")
    asr = ASR(api_key="k", base_url="http://8.8.8.8:8080/v1", allow_insecure_base_url=True)
    assert asr.get_base_url() == "http://8.8.8.8:8080/v1"


def test_set_parameters_updates_defaults(monkeypatch: pytest.MonkeyPatch):
    asr, _, calls = _pair(monkeypatch)
    asr.set_parameters(model="m2", language="en", prompt="pp")
    asr.transcribe(b"audio-bytes", filename="voice.mp3")
    request = calls[0][0]
    assert request["model"] == "m2" and request["language"] == "en" and request["prompt"] == "pp"
