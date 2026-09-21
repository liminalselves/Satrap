"""语音转写与文件正文的内容补全与投影"""
from unittest.mock import AsyncMock
from typing import Any
import os

import pytest

from satrap.core.pipeline import attachments as module
from satrap.core.pipeline.attachments import AttachmentResult, asr_resolver_from_manager, render_attachments, resolve_attachments
from satrap.core.pipeline.input_projection import project_input
from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.utils.outbound import OutboundHTTPResponse
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform import PlatformConfig
from satrap.core.type import ASRConfig


async def make_event(segments: list[dict[str, object]], settings: dict[str, object] | None = None):
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings or {})))
    adapter._bot = AsyncMock()
    adapter._bot.send_group_msg.return_value = {"message_id": 1}
    await adapter._handle_group_message({"self_id": 10000, "user_id": 123, "group_id": 456, "message_id": 77, "message_type": "group",
        "message": [{"type": "at", "data": {"qq": "10000"}}, *segments]})
    return adapter, adapter._event_queue.get_nowait()


def record(url: str = "https://media.example.com/voice.amr.wav") -> dict[str, object]:
    return {"type": "record", "data": {"file": "voice.wav", "url": url}}


def file_segment(name: str = "notes.txt", url: str = "https://files.example.com/notes.txt") -> dict[str, object]:
    return {"type": "file", "data": {"name": name, "file": "f1", "url": url}}


def asr_config() -> ASRConfig:
    return ASRConfig(name="speech", model="whisper-1", api_key="k", base_url="https://asr.example.com/v1")


def fake_download(payload: dict[str, bytes]):
    calls: list[tuple[str, int, bool, bool]] = []

    async def download(url: str, *, timeout: float, max_response_bytes: int, trusted_hosts: tuple[str, ...], ssl_verify: bool, restrict_redirects_to_origin: bool) -> OutboundHTTPResponse:
        calls.append((url, max_response_bytes, ssl_verify, restrict_redirects_to_origin))
        if url not in payload:
            return OutboundHTTPResponse(url=url, status_code=404, headers={}, content=b"")
        return OutboundHTTPResponse(url=url, status_code=200, headers={}, content=payload[url])
    return download, calls


@pytest.mark.asyncio
async def test_voice_is_transcribed_and_projected(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await make_event([record()], {"asr_model": "speech"})
    download, calls = fake_download({"https://media.example.com/voice.amr.wav": b"RIFF"})
    monkeypatch.setattr(module, "safe_async_get", download)
    seen: dict[str, Any] = {}

    async def transcribe(data: bytes, filename: str, config: ASRConfig) -> str:
        seen.update(data=data, filename=filename, model=config.model)
        return "你好世界"

    monkeypatch.setattr(module, "_transcribe", transcribe)
    results = await resolve_attachments(event, lambda name: asr_config() if name == "speech" else None)
    assert results == (AttachmentResult("record", "voice.amr.wav", "resolved", "你好世界"),)
    assert seen == {"data": b"RIFF", "filename": "voice.wav", "model": "whisper-1"}
    assert calls[0][1] == module.AUDIO_MAX_BYTES
    assert event.get_messages()[1].text == "你好世界"
    projected = project_input(event, "none", "none", results)
    assert projected.message == "[语音 转写内容: 你好世界]\n@10000"
    assert projected.attachment_status == "resolved"
    again = await resolve_attachments(event, lambda name: asr_config())
    assert again[0].text == "你好世界" and len(calls) == 1


@pytest.mark.asyncio
async def test_download_verifies_tls_unless_explicitly_disabled(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await make_event([file_segment()])
    download, calls = fake_download({"https://files.example.com/notes.txt": b"hello"})
    monkeypatch.setattr(module, "safe_async_get", download)
    await resolve_attachments(event, None)
    assert calls[0][2:] == (True, True)
    adapter, event = await make_event([file_segment()], {"media_insecure_tls": True})
    await resolve_attachments(event, None)
    assert calls[1][2:] == (False, True)


@pytest.mark.asyncio
async def test_temp_file_is_tracked_even_when_write_fails(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await make_event([file_segment()])
    download, _ = fake_download({"https://files.example.com/notes.txt": b"hello"})
    monkeypatch.setattr(module, "safe_async_get", download)
    tracked: list[str] = []
    monkeypatch.setattr(event, "track_temporary_local_file", tracked.append)

    class _Broken:
        name = os.path.join(os.getcwd(), "never-created.txt")

        def write(self, data: bytes) -> None:
            raise OSError("disk full")

        def close(self) -> None:
            pass

    monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", lambda **kwargs: _Broken())
    results = await resolve_attachments(event, None)
    assert results[0].status == "failed" and results[0].reason == "OSError"
    assert tracked == [_Broken.name]


@pytest.mark.asyncio
async def test_voice_without_asr_binding_is_marked_disabled_and_not_downloaded(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await make_event([record()])
    download, calls = fake_download({})
    monkeypatch.setattr(module, "safe_async_get", download)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "disabled" and results[0].reason == "asr_not_configured" and not calls
    adapter, event = await make_event([record()], {"asr_model": "missing"})
    results = await resolve_attachments(event, lambda name: None)
    assert results[0].reason == "asr_config_missing"
    projected = project_input(event, "none", "none", results)
    assert projected.message.startswith("[语音: 未启用转写]") and "attachment_disabled" in projected.notes


@pytest.mark.asyncio
async def test_voice_download_or_asr_failure_degrades_without_raising(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await make_event([record("https://media.example.com/missing.wav")], {"asr_model": "speech"})
    download, _ = fake_download({})
    monkeypatch.setattr(module, "safe_async_get", download)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "failed" and results[0].reason == "OutboundHTTPError"
    monkeypatch.undo()
    adapter, event = await make_event([record("http://10.0.0.5/voice.wav")], {"asr_model": "speech"})
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "failed" and results[0].reason == "UnsafeOutboundURLError"
    adapter, event = await make_event([record("file:///etc/passwd.wav")], {"asr_model": "speech"})
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "failed" and results[0].reason == "UnsafeOutboundURLError"
    adapter, event = await make_event([record("https://media.example.com/voice.xyz")], {"asr_model": "speech"})
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "unsupported"


@pytest.mark.asyncio
async def test_file_is_downloaded_extracted_and_temp_file_cleaned(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await make_event([file_segment()])
    download, calls = fake_download({"https://files.example.com/notes.txt": "第一行\n第二行".encode("utf-8")})
    monkeypatch.setattr(module, "safe_async_get", download)
    results = await resolve_attachments(event, None)
    assert results[0].status == "resolved" and "第一行" in results[0].text and "第二行" in results[0].text
    assert calls[0][1] == module.FILE_MAX_BYTES
    temp_paths = list(event._temporary_local_files)
    assert len(temp_paths) == 1 and os.path.exists(temp_paths[0])
    event.cleanup_temporary_local_files()
    assert not os.path.exists(temp_paths[0])
    projected = project_input(event, "none", "none", results)
    assert projected.message.startswith("[文件 notes.txt 内容:\n第一行") and projected.message.endswith("@10000")


@pytest.mark.asyncio
async def test_file_unsupported_disabled_and_limit(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await make_event([file_segment("archive.zip", "https://files.example.com/archive.zip")])
    download, calls = fake_download({})
    monkeypatch.setattr(module, "safe_async_get", download)
    results = await resolve_attachments(event, None)
    assert results[0].status == "unsupported" and not calls
    adapter, event = await make_event([file_segment()], {"attachment_extract": False})
    results = await resolve_attachments(event, None)
    assert results[0].status == "disabled" and not calls
    adapter, event = await make_event([file_segment(f"n{i}.txt", f"https://files.example.com/n{i}.txt") for i in range(6)])
    download, calls = fake_download({f"https://files.example.com/n{i}.txt": b"x" for i in range(6)})
    monkeypatch.setattr(module, "safe_async_get", download)
    results = await resolve_attachments(event, None)
    assert [r.status for r in results] == ["resolved"] * 4 + ["skipped", "skipped"]
    assert len(calls) == 4
    projected = project_input(event, "none", "none", results)
    assert projected.attachment_status == "partial" and projected.message.count("[文件") == 6


@pytest.mark.asyncio
async def test_scheduler_passes_attachment_context_to_session(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await make_event([record()], {"asr_model": "speech"})
    download, _ = fake_download({"https://media.example.com/voice.amr.wav": b"RIFF"})
    monkeypatch.setattr(module, "safe_async_get", download)

    async def transcribe(data: bytes, filename: str, config: ASRConfig) -> str:
        return "请帮我总结"

    monkeypatch.setattr(module, "_transcribe", transcribe)
    manager = AsyncMock()
    manager.handle_call_async.return_value = "好的"
    scheduler = PipelineScheduler(manager)
    scheduler.asr_resolver = lambda name: asr_config()
    await scheduler.execute(event)
    call = manager.handle_call_async.call_args.args[0]
    assert call.message.startswith("[语音 转写内容: 请帮我总结]")
    assert event.get_extra("input_projection").attachment_status == "resolved"


@pytest.mark.asyncio
async def test_unwoken_voice_message_never_downloads(monkeypatch: pytest.MonkeyPatch):
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={"asr_model": "speech"}))
    adapter._bot = AsyncMock()
    await adapter._handle_group_message({"self_id": 10000, "user_id": 123, "group_id": 456, "message_id": 78, "message_type": "group", "message": [record()]})
    event = adapter._event_queue.get_nowait()
    download, calls = fake_download({"https://media.example.com/voice.amr.wav": b"RIFF"})
    monkeypatch.setattr(module, "safe_async_get", download)
    manager = AsyncMock()
    scheduler = PipelineScheduler(manager)
    scheduler.asr_resolver = lambda name: asr_config()
    await scheduler.execute(event)
    assert not calls
    manager.handle_call_async.assert_not_awaited()


def test_render_and_resolver_from_manager():
    assert render_attachments(()) == ""
    text = render_attachments((AttachmentResult("file", "a.pdf", "failed", reason="x"), AttachmentResult("record", "v", "skipped")))
    assert text == "[文件 a.pdf: 获取或处理失败]\n[语音: 超出附件处理数量]"

    class _Manager:
        def has_config(self, target: str, name: str) -> bool:
            return (target, name) == ("asr", "speech")

        def get_asr_config(self, name: str) -> ASRConfig:
            return asr_config()

    resolver = asr_resolver_from_manager(_Manager())
    assert resolver is not None and resolver("speech") is not None and resolver("other") is None
    assert asr_resolver_from_manager(None) is None
    assert asr_resolver_from_manager(object()) is None


@pytest.mark.parametrize("settings", [{"asr_model": 1}, {"asr_model": "x" * 129}, {"media_trusted_hosts": "host"}, {"media_trusted_hosts": [""]}, {"attachment_extract": "no"}])
def test_attachment_policy_validation(settings: dict[str, object]):
    with pytest.raises(ValueError):
        validate_wake_policy(settings)


def test_attachment_policy_accepts_valid():
    validate_wake_policy({"asr_model": "speech", "media_trusted_hosts": ["snowluma.local"], "attachment_extract": False})
