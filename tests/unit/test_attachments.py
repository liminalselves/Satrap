"""语音转写与文件正文的内容补全与投影"""
from unittest.mock import AsyncMock
from typing import Any, Callable
import base64
import os

import pytest

from satrap.core.pipeline import attachments as module
from satrap.core.pipeline import audio_convert
from satrap.core.pipeline.attachments import AttachmentResult, asr_resolver_from_manager, render_attachments, resolve_attachments
from satrap.core.pipeline.input_projection import project_input
from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.utils.outbound import OutboundHTTPResponse, UnsafeOutboundURLError
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.onebot.admin import AdminActionUnconfirmed, UnsupportedAdminAction
from aiocqhttp.exceptions import ActionFailed
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform import PlatformConfig
from satrap.core.type import ASRConfig
from satrap.core.framework.providers import BindingState, BindingStatus


async def make_event(segments: list[dict[str, object]], settings: dict[str, object] | None = None):
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings or {})))
    adapter._bot = AsyncMock()
    adapter._bot.send_group_msg.return_value = {"message_id": 1}
    # 默认模拟不提供 get_record/fetch_ptt_text 的实现, 需要时由测试覆盖 return_value
    adapter._bot.get_record.side_effect = ActionFailed({"retcode": 1404})
    adapter._bot.fetch_ptt_text.side_effect = ActionFailed({"retcode": 1404})
    await adapter._handle_group_message({"self_id": 10000, "user_id": 123, "group_id": 456, "message_id": 77, "message_type": "group",
        "message": [{"type": "at", "data": {"qq": "10000"}}, *segments]})
    return adapter, adapter._event_queue.get_nowait()


class _RunnableRegistry:
    """绑定判定恒为可运行的会话定义注册表替身"""

    @staticmethod
    def binding_status(*_args: object) -> BindingStatus:
        """
        恒定答复可运行

        返回:
        - BindingStatus: 可运行
        """
        return BindingStatus(BindingState.RUNNABLE)


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
    assert projected.message == "[语音 转写内容: 你好世界]\n[用户 123, 消息 77] @10000"
    assert projected.attachment_status == "resolved"
    again = await resolve_attachments(event, lambda name: asr_config())
    assert again[0].text == "你好世界" and len(calls) == 1


@pytest.mark.asyncio
async def test_download_verifies_tls_unless_host_is_trusted(monkeypatch: pytest.MonkeyPatch):
    # 默认始终校验
    adapter, event = await make_event([file_segment()])
    download, calls = fake_download({"https://files.example.com/notes.txt": b"hello"})
    monkeypatch.setattr(module, "safe_async_get", download)
    await resolve_attachments(event, None)
    assert calls[0][2:] == (True, True)
    # media_insecure_tls 对未登记主机不生效, 仍校验
    adapter, event = await make_event([file_segment()], {"media_insecure_tls": True})
    await resolve_attachments(event, None)
    assert calls[1][2:] == (True, True)
    # 主机登记进 media_trusted_hosts 后才关闭校验
    adapter, event = await make_event(
        [file_segment()], {"media_insecure_tls": True, "media_trusted_hosts": ["files.example.com"]})
    await resolve_attachments(event, None)
    assert calls[2][2:] == (False, True)


@pytest.mark.asyncio
async def test_temp_file_is_cleaned_up_when_write_fails(monkeypatch: pytest.MonkeyPatch):
    """写入在提取线程内完成, 失败时由辅助函数内部清理, 不向事件登记"""
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
    assert tracked == []


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
    download, _ = fake_download({"https://media.example.com/voice.xyz": b"\x00\x01garbage"})
    monkeypatch.setattr(module, "safe_async_get", download)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "unsupported" and results[0].reason == "unknown_codec"


@pytest.mark.asyncio
async def test_voice_prefers_platform_transcode_via_get_record(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await make_event([record()], {"asr_model": "speech"})
    wav = b"RIFF" + bytes(4) + b"WAVEfmt "
    adapter._bot.get_record.side_effect = None
    adapter._bot.get_record.return_value = {"base64": base64.b64encode(wav).decode()}
    download, calls = fake_download({})
    monkeypatch.setattr(module, "safe_async_get", download)
    seen: dict[str, Any] = {}

    async def transcribe(data: bytes, filename: str, config: ASRConfig) -> str:
        seen.update(data=data, filename=filename)
        return "平台转码"

    monkeypatch.setattr(module, "_transcribe", transcribe)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "resolved" and results[0].text == "平台转码"
    assert seen == {"data": wav, "filename": "voice.wav"} and not calls
    adapter._bot.get_record.assert_awaited_once_with(file="https://media.example.com/voice.amr.wav", out_format="wav")


@pytest.mark.asyncio
async def test_voice_get_record_failure_falls_back_to_download(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await make_event([record()], {"asr_model": "speech"})
    adapter._bot.get_record.side_effect = RuntimeError("network")
    download, calls = fake_download({"https://media.example.com/voice.amr.wav": b"OggS" + bytes(16)})
    monkeypatch.setattr(module, "safe_async_get", download)
    seen: dict[str, Any] = {}

    async def transcribe(data: bytes, filename: str, config: ASRConfig) -> str:
        seen["filename"] = filename
        return "ok"

    monkeypatch.setattr(module, "_transcribe", transcribe)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "resolved" and seen["filename"] == "voice.ogg" and len(calls) == 1


@pytest.mark.asyncio
async def test_silk_voice_without_platform_transcode_is_marked(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await make_event([record("https://media.example.com/abc.amr")], {"asr_model": "speech"})
    download, _ = fake_download({"https://media.example.com/abc.amr": b"\x02#!SILK_V3" + bytes(8)})
    monkeypatch.setattr(module, "safe_async_get", download)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "unsupported" and results[0].reason == "silk_needs_platform_transcode"
    assert "SILK" in render_attachments(results)


@pytest.mark.asyncio
async def test_amr_voice_is_converted_locally(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await make_event([record("https://media.example.com/abc.amr")], {"asr_model": "speech"})
    download, _ = fake_download({"https://media.example.com/abc.amr": b"#!AMR\n" + bytes(32)})
    monkeypatch.setattr(module, "safe_async_get", download)
    def _convert(data: bytes, *, max_seconds: float) -> bytes:
        return b"RIFFconverted"

    monkeypatch.setattr(module, "convert_to_wav", _convert)
    seen: dict[str, Any] = {}

    async def transcribe(data: bytes, filename: str, config: ASRConfig) -> str:
        seen.update(data=data, filename=filename)
        return "本地转码"

    monkeypatch.setattr(module, "_transcribe", transcribe)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].text == "本地转码" and seen == {"data": b"RIFFconverted", "filename": "voice.wav"}
    monkeypatch.setitem(audio_convert._AV_STATE, "module", None)
    adapter, event = await make_event([record("https://media.example.com/abc.amr")], {"asr_model": "speech"})
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "unsupported" and results[0].reason == "av_missing"
    assert "av 包" in render_attachments(results)


@pytest.mark.asyncio
async def test_voice_transcribe_modes(monkeypatch: pytest.MonkeyPatch):
    download, calls = fake_download({})
    monkeypatch.setattr(module, "safe_async_get", download)
    adapter, event = await make_event([record()], {"asr_model": "speech", "voice_transcribe": "off"})
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "disabled" and results[0].reason == "voice_transcribe_off"
    adapter, event = await make_event([record()], {"voice_transcribe": "platform"})
    adapter._bot.fetch_ptt_text.side_effect = None
    adapter._bot.fetch_ptt_text.return_value = {"text": "原生转写"}
    results = await resolve_attachments(event, None)
    assert results[0].status == "resolved" and results[0].text == "原生转写" and not calls
    adapter._bot.fetch_ptt_text.assert_awaited_once_with(message_id=77)
    adapter, event = await make_event([record()], {"voice_transcribe": "platform"})
    results = await resolve_attachments(event, None)
    assert results[0].status == "unsupported" and results[0].reason == "platform_transcribe_unavailable"
    adapter, event = await make_event([record()], {"asr_model": "speech", "voice_transcribe": "asr_then_platform"})
    adapter._bot.fetch_ptt_text.side_effect = None
    adapter._bot.fetch_ptt_text.return_value = {"text": "兜底"}
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "resolved" and results[0].text == "兜底" and len(calls) == 1
    adapter, event = await make_event([record()], {"asr_model": "speech", "voice_transcribe": "asr_then_platform"})
    adapter._bot.fetch_ptt_text.side_effect = AdminActionUnconfirmed("timeout")
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "failed" and results[0].reason == "OutboundHTTPError"


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
    manager.provider_registry = _RunnableRegistry()
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
    manager.provider_registry = _RunnableRegistry()
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


@pytest.mark.parametrize("settings", [{"asr_model": 1}, {"asr_model": "x" * 129}, {"media_trusted_hosts": "host"}, {"media_trusted_hosts": [""]}, {"attachment_extract": "no"}, {"voice_transcribe": "auto"}, {"voice_transcribe": 1}])
def test_attachment_policy_validation(settings: dict[str, object]):
    with pytest.raises(ValueError):
        validate_wake_policy(settings)


def test_attachment_policy_accepts_valid():
    validate_wake_policy({"asr_model": "speech", "media_trusted_hosts": ["snowluma.local"], "attachment_extract": False, "voice_transcribe": "asr_then_platform"})


class _CloseFailingClient:
    async def close(self) -> None:
        raise RuntimeError("close boom")


class _StubASR:
    def __init__(self, text: str | None = "你好", error: Exception | None = None) -> None:
        self.suppress_error = False
        self.client = _CloseFailingClient()
        self._text = text
        self._error = error

    async def transcribe(self, data: bytes, filename: str | None = None) -> Any:
        if self._error is not None:
            raise self._error
        from satrap.core.type import ASRResponse
        return ASRResponse(text=self._text or "", model="m")


def _stub_build(stub: _StubASR) -> Callable[..., _StubASR]:
    """构造 build_asr_from_config 的定型替身"""
    def build(config: ASRConfig, async_: bool = True) -> _StubASR:
        return stub
    return build


@pytest.mark.asyncio
async def test_transcribe_close_failure_does_not_mask_result(monkeypatch: pytest.MonkeyPatch):
    """关闭客户端失败不覆盖成功的转写结果"""
    monkeypatch.setattr(module, "build_asr_from_config", _stub_build(_StubASR()))
    assert await module._transcribe(b"x", "a.wav", ASRConfig()) == "你好"


@pytest.mark.asyncio
async def test_transcribe_close_failure_preserves_original_error(monkeypatch: pytest.MonkeyPatch):
    """转写与关闭同时失败时抛出原始转写异常"""
    monkeypatch.setattr(module, "build_asr_from_config", _stub_build(_StubASR(error=RuntimeError("transcribe boom"))))
    with pytest.raises(RuntimeError, match="transcribe boom"):
        await module._transcribe(b"x", "a.wav", ASRConfig())


@pytest.mark.asyncio
async def test_insecure_tls_only_applies_to_trusted_hosts(monkeypatch: pytest.MonkeyPatch):
    """media_insecure_tls 只对登记主机关闭校验, 公网主机始终校验"""
    calls: list[dict[str, Any]] = []

    class _Resp:
        content = b"x"

        def raise_for_status(self) -> None:
            return None

    async def fake_get(url: str, **kwargs: Any) -> Any:
        calls.append({"url": url, **kwargs})
        return _Resp()

    monkeypatch.setattr(module, "safe_async_get", fake_get)
    await module._download("https://public.example.com/a.png", 100, ("nas.local",), verify_tls=False)
    await module._download("https://nas.local/a.png", 100, ("nas.local",), verify_tls=False)
    await module._download("https://other.local/a.png", 100, ("nas.local",), verify_tls=False)
    assert calls[0]["ssl_verify"] is True
    assert calls[1]["ssl_verify"] is False
    assert calls[2]["ssl_verify"] is True


@pytest.mark.asyncio
async def test_attachment_total_budget_marks_remaining(monkeypatch: pytest.MonkeyPatch):
    """事件级总预算耗尽后, 其余附件标记 attachment_budget_exceeded 且不再下载"""
    now = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    payload = {
        "https://files.example.com/a.txt": b"a",
        "https://files.example.com/b.txt": b"b",
        "https://files.example.com/c.txt": b"c",
    }
    download, calls = fake_download(payload)

    async def slow_download(url: str, **kwargs: Any) -> OutboundHTTPResponse:
        now[0] += 100.0
        return await download(url, **kwargs)

    monkeypatch.setattr(module, "safe_async_get", slow_download)
    adapter, event = await make_event([
        file_segment("a.txt", "https://files.example.com/a.txt"),
        file_segment("b.txt", "https://files.example.com/b.txt"),
        file_segment("c.txt", "https://files.example.com/c.txt"),
    ])
    results = await resolve_attachments(event, None)
    assert [item.status for item in results] == ["resolved", "failed", "failed"]
    assert results[1].reason == results[2].reason == "attachment_budget_exceeded"
    assert len(calls) == 1
    rendered = render_attachments(results)
    assert "附件处理超时" in rendered


@pytest.mark.asyncio
async def test_frozen_transcript_is_truncated(monkeypatch: pytest.MonkeyPatch):
    """长转写冻结进组件时同样按 TRANSCRIPT_LIMIT 截断"""
    adapter, event = await make_event([record()], {"asr_model": "speech"})
    download, _ = fake_download({"https://media.example.com/voice.amr.wav": b"RIFF"})
    monkeypatch.setattr(module, "safe_async_get", download)

    async def transcribe(data: bytes, filename: str, config: ASRConfig) -> str:
        return "长" * (module.TRANSCRIPT_LIMIT + 100)

    monkeypatch.setattr(module, "_transcribe", transcribe)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert len(results[0].text) == module.TRANSCRIPT_LIMIT
    record_comp = event.get_messages()[1]
    assert len(record_comp.text) == module.TRANSCRIPT_LIMIT


@pytest.mark.asyncio
async def test_display_name_is_sanitized(monkeypatch: pytest.MonkeyPatch):
    """平台上报文件名的换行与 ANSI 转义不进入结果与渲染块"""
    evil = "evil\x1b[31m\nname.txt"
    adapter, event = await make_event([file_segment(evil, "https://files.example.com/x.txt")])
    download, _ = fake_download({"https://files.example.com/x.txt": b"hello"})
    monkeypatch.setattr(module, "safe_async_get", download)
    results = await resolve_attachments(event, None)
    assert results[0].name == "evil [31m name.txt"
    assert "\n" not in results[0].name and "\x1b" not in results[0].name
    rendered = render_attachments(results)
    assert "\x1b" not in rendered
    label_line = rendered.splitlines()[0]
    assert "evil [31m name.txt" in label_line
    assert module._safe_display("长" * 100) == "长" * 80


@pytest.mark.asyncio
async def test_accepted_voice_over_duration_is_rejected(monkeypatch: pytest.MonkeyPatch):
    """直收 wav 时长超过 AUDIO_MAX_SECONDS 时标记 audio_too_long, 不送 ASR"""
    import io
    import wave
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(bytes(2 * 8000 * int(module.AUDIO_MAX_SECONDS + 30)))
    adapter, event = await make_event([record()], {"asr_model": "speech"})
    download, _ = fake_download({"https://media.example.com/voice.amr.wav": buffer.getvalue()})
    monkeypatch.setattr(module, "safe_async_get", download)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "unsupported" and results[0].reason == "audio_too_long"


@pytest.mark.asyncio
async def test_plaintext_http_requires_user_switch(monkeypatch: pytest.MonkeyPatch):
    """公网明文 http 默认拒绝, 开启 media_plaintext_http 或登记主机后放行"""
    calls: list[str] = []

    class _Resp:
        content = b"x"

        def raise_for_status(self) -> None:
            return None

    async def fake_get(url: str, **kwargs: Any) -> Any:
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(module, "safe_async_get", fake_get)
    with pytest.raises(UnsafeOutboundURLError, match="https"):
        await module._download("http://public.example.com/a.png", 100, ())
    with pytest.raises(UnsafeOutboundURLError, match="https"):
        await module._download("http://nas.local/a.png", 100, ())
    await module._download("http://nas.local/a.png", 100, ("nas.local",))
    await module._download("http://public.example.com/a.png", 100, (), allow_plaintext=True)
    assert calls == ["http://nas.local/a.png", "http://public.example.com/a.png"]


@pytest.mark.asyncio
async def test_plaintext_http_setting_flows_through_resolve(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await make_event(
        [file_segment("notes.txt", "http://files.example.com/notes.txt")])
    download, calls = fake_download({"http://files.example.com/notes.txt": b"hello"})
    monkeypatch.setattr(module, "safe_async_get", download)
    results = await resolve_attachments(event, None)
    assert results[0].status == "failed" and results[0].reason == "UnsafeOutboundURLError"
    adapter, event = await make_event(
        [file_segment("notes.txt", "http://files.example.com/notes.txt")], {"media_plaintext_http": True})
    results = await resolve_attachments(event, None)
    assert results[0].status == "resolved"
    assert len(calls) == 1


@pytest.mark.parametrize("settings", [{"media_plaintext_http": "yes"}, {"media_plaintext_http": 1}])
def test_plaintext_http_policy_must_be_boolean(settings: dict[str, object]):
    with pytest.raises(ValueError, match="media_plaintext_http"):
        validate_wake_policy(settings)
