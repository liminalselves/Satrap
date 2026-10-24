"""业务链路场景与集中边界契约"""
from __future__ import annotations

from unittest.mock import AsyncMock
from typing import Any, cast
import os
from aiocqhttp.exceptions import ActionFailed
import pytest
from satrap.core.pipeline.input_projection import _MediaBudget, media_sources, resolve_forwards, resolve_quotes
from satrap.core.pipeline.input_projection import select_media
from satrap.core.pipeline.media_resolve import MediaResult, resolve_media
from satrap.core.pipeline.input_projection import project_input
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.components import BaseMessageComponent, Forward, Image, PlatformComponentType, Video
from satrap.core.platform.event import MessageEvent
from satrap.core.type import safe_getattr
from satrap.core.platform import PlatformConfig
from pathlib import Path
from types import SimpleNamespace
import asyncio
import base64
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.components import File, Node, Nodes
from satrap.core.components.message import get_satrap_temp_path
from satrap.core.platform.onebot.onebot_utils import _normalize_file_source
from satrap.core.platform.misskey.adapter import MisskeyAdapter
from satrap.core.platform.misskey.misskey_utils import resolve_component_url_or_path
from satrap.core.platform.event import MessageChain
from satrap.core.storage import StorageLayout
from satrap.core.utils import paths
from collections.abc import Awaitable
from satrap.core.pipeline.input_projection import QUOTE_TEXT_LIMIT
from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.components import Reply
from typing import Callable
from satrap.core.pipeline import attachments as _attachments_module, audio_convert
from satrap.core.pipeline.attachments import AttachmentResult, asr_resolver_from_manager, render_attachments
from satrap.core.pipeline.attachments import resolve_attachments
from satrap.core.utils.outbound import OutboundHTTPResponse, UnsafeOutboundURLError
from satrap.core.platform.onebot.admin import AdminActionUnconfirmed
from satrap.core.type import ASRConfig
import io
import math
import struct
import sys
from array import array
from satrap.core.pipeline import audio_convert as _audio_convert_module
from satrap.core.pipeline.audio_convert import AudioConvertError, AudioTooLong, convert_to_wav, probe_audio
from satrap.core.pipeline.audio_convert import probe_duration
from support.providers import RunnableRegistry

def reference_selection(event: MessageEvent, quote_status: str) -> tuple[list[str], list[str], list[str]]:
    """按重构前的公开实现重算选中结果, 作为等价性基准"""
    top = event.get_messages()
    limit = int(event.policy_settings["input_media_limit"])
    images = media_sources(top, "image")
    videos = media_sources(top, "video")
    notes: list[str] = []
    if len(images) + len(videos) > limit:
        videos = videos[: max(0, limit - len(images))]
        images = images[: limit]
        notes.append("top_media_truncated")
    budget = _MediaBudget(limit - len(images) - len(videos), images, videos, notes)
    replies = [c for c in top if c.type == PlatformComponentType.Reply]
    if replies and quote_status == "resolved":
        chain = safe_getattr(replies[0], "chain")
        quoted = [c for c in cast(list[Any], chain) if isinstance(c, BaseMessageComponent)] if isinstance(chain, list) else []
        budget.merge(quoted, "quote_media_truncated")
    for forward in [c for c in top if isinstance(c, Forward)]:
        for node in forward.nodes or []:
            content = safe_getattr(node, "content")
            node_components = [c for c in cast(list[Any], content) if isinstance(c, BaseMessageComponent)] if isinstance(content, list) else []
            budget.merge(node_components, "forward_media_truncated")
    return images, videos, notes

async def _media_resolve_make_event(settings: dict[str, object], segments: list[dict[str, object]]):
    """构造已通过适配器入站的群消息事件"""
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings)))
    adapter._bot = AsyncMock()
    await adapter._handle_group_message({
        "self_id": 10000, "user_id": 123, "group_id": 456, "message_id": 77,
        "message_type": "group", "message": segments,
    })
    return adapter, adapter._event_queue.get_nowait()

class TestSelectMediaEquivalence:
    """select_media 与重构前的公开实现保持同一选中结论"""

    @pytest.mark.asyncio
    async def test_selection_notes_cover_each_stage(self):
        adapter, event = await _media_resolve_make_event({"input_media_limit": 1}, [
            {"type": "reply", "data": {"id": "5"}},
            {"type": "forward", "data": {"id": "f1"}},
            {"type": "image", "data": {"url": "http://x/a.png"}},
            {"type": "image", "data": {"url": "http://x/b.png"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        adapter._bot.get_msg.return_value = {
            "message_id": 5, "group_id": 456, "message_type": "group", "self_id": 10000,
            "sender": {"user_id": 321, "nickname": "小明"}, "time": 1700000000,
            "message": [{"type": "image", "data": {"url": "http://x/q.png"}}],
        }
        adapter._bot.get_forward_msg.return_value = {"messages": [
            {"type": "node", "data": {"nickname": "小红", "user_id": 654, "content": [
                {"type": "image", "data": {"url": "http://x/f.png"}},
            ]}},
        ]}
        quote_status = await resolve_quotes(event)
        await resolve_forwards(event)

        selection = select_media(event, quote_status)
        images, videos, notes = reference_selection(event, quote_status)

        assert selection.top_notes == ("top_media_truncated",)
        assert selection.quote_notes == ("quote_media_truncated",)
        assert selection.forward_notes == (("forward_media_truncated",),)
        assert selection.sources("image") == images == ["http://x/a.png"]
        assert selection.sources("video") == videos == []
        assert notes == ["top_media_truncated", "quote_media_truncated", "forward_media_truncated"]

def test_media_sources_does_not_trust_generic_path_field():
    component = Image(file="9f2c.image")
    component.path = "C:/secret/local.txt"

    # path 是通用字段, 任何适配器都能写, 不得被当作已验证的本地文件
    assert media_sources([component], "image") == ["9f2c.image"]

def test_media_sources_prefers_resolved_path_then_url():
    component = Image(file="9f2c.image", url="https://cdn/a.png?rkey=x")
    assert media_sources([component], "image") == ["https://cdn/a.png?rkey=x"]

    component.resolved_path = "C:/tmp/satrap-media-abc.png"
    assert media_sources([component], "image") == ["C:/tmp/satrap-media-abc.png"]

def test_media_sources_deduplicates_and_selects_by_type():
    shared = "https://cdn/same.png"
    components: list[BaseMessageComponent] = [
        Image(file="a.image", url=shared),
        Image(file="b.image", url=shared),
        Video(file="v.mp4", url="https://cdn/v.mp4"),
    ]

    assert media_sources(components, "image") == [shared]
    assert media_sources(components, "video") == ["https://cdn/v.mp4"]
    assert media_sources([Image(file="a.image")], "video") == []

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

def _patch_download(monkeypatch: pytest.MonkeyPatch, behaviour: list[bytes | BaseException]):
    """按调用次序返回字节或抛出异常, 并记录被请求的地址"""
    calls: list[str] = []

    async def fake(url: str, limit: int, trusted: tuple[str, ...], verify_tls: bool = True, allow_plaintext: bool = False) -> bytes:
        """替换真实出站下载, 不发起网络请求"""
        calls.append(url)
        outcome = behaviour[min(len(calls) - 1, len(behaviour) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr("satrap.core.pipeline.media_resolve._download", fake)
    return calls

class TestResolveMedia:
    @pytest.mark.asyncio
    async def test_valid_url_downloads_without_refresh(self, monkeypatch: pytest.MonkeyPatch):
        adapter, event = await _media_resolve_make_event({}, [
            {"type": "image", "data": {"file": "9f2c.image", "url": "https://cdn/a.png"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        calls = _patch_download(monkeypatch, [PNG])
        selection = select_media(event, "none")

        results = await resolve_media(event, selection)

        assert [item.status for item in results] == ["resolved"]
        assert calls == ["https://cdn/a.png"]
        adapter._bot.get_image.assert_not_awaited()
        component = cast(Image, event.get_messages()[0])
        assert component.resolved_path.endswith(".png")
        assert os.path.exists(component.resolved_path)
        # 原始上报字段保持不变, 只多出解析结果
        assert (component.file, component.url) == ("9f2c.image", "https://cdn/a.png")
        event.cleanup_temporary_local_files()

    @pytest.mark.asyncio
    async def test_expired_url_is_refreshed_by_get_image(self, monkeypatch: pytest.MonkeyPatch):
        adapter, event = await _media_resolve_make_event({}, [
            {"type": "image", "data": {"file": "9f2c.image", "url": "https://cdn/expired.png"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        adapter._bot.get_image.return_value = {"file": "fresh.png", "url": "https://cdn/fresh.png"}
        calls = _patch_download(monkeypatch, [RuntimeError("HTTP 400"), PNG])
        selection = select_media(event, "none")

        results = await resolve_media(event, selection)

        assert [item.status for item in results] == ["resolved"]
        assert [item.reason for item in results] == ["image_url_refreshed"]
        assert calls == ["https://cdn/expired.png", "https://cdn/fresh.png"]
        # 必须优先用入站保留的原始标识请求刷新, 旧地址只是靠 URL alias 才可用的兜底
        adapter._bot.get_image.assert_awaited_once_with(file="9f2c.image")
        event.cleanup_temporary_local_files()

    @pytest.mark.asyncio
    async def test_refresh_falls_back_to_reported_url(self, monkeypatch: pytest.MonkeyPatch):
        adapter, event = await _media_resolve_make_event({}, [
            {"type": "image", "data": {"file": "9f2c.image", "url": "https://cdn/expired.png"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])

        async def fake_get_image(**kwargs: object) -> dict[str, str]:
            """只在以上报地址请求时返回刷新后的地址, 模拟不认原始标识的实现"""
            if kwargs.get("file") == "https://cdn/expired.png":
                return {"file": "fresh.png", "url": "https://cdn/fresh.png"}
            return {"file": "", "url": ""}

        adapter._bot.get_image.side_effect = fake_get_image
        calls = _patch_download(monkeypatch, [RuntimeError("HTTP 400"), PNG])
        selection = select_media(event, "none")

        results = await resolve_media(event, selection)

        assert [item.status for item in results] == ["resolved"]
        assert calls == ["https://cdn/expired.png", "https://cdn/fresh.png"]
        # 原始标识未被识别时仍要退回上报地址, 不能直接判定失败
        assert [call.kwargs["file"] for call in adapter._bot.get_image.await_args_list] == [
            "9f2c.image", "https://cdn/expired.png",
        ]
        event.cleanup_temporary_local_files()

    @pytest.mark.asyncio
    async def test_file_only_skips_direct_download(self, monkeypatch: pytest.MonkeyPatch):
        adapter, event = await _media_resolve_make_event({}, [
            {"type": "image", "data": {"file": "9f2c.image"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        adapter._bot.get_image.return_value = {"file": "9f2c.image", "url": "https://cdn/fresh.png"}
        calls = _patch_download(monkeypatch, [PNG])
        selection = select_media(event, "none")

        results = await resolve_media(event, selection)

        assert [item.status for item in results] == ["resolved"]
        # 裸标识不是可下载地址, 只允许经实现动作换回地址后下载一次
        assert calls == ["https://cdn/fresh.png"]
        adapter._bot.get_image.assert_awaited_once_with(file="9f2c.image")
        event.cleanup_temporary_local_files()

    @pytest.mark.asyncio
    async def test_unsupported_get_image_degrades_without_raising(self, monkeypatch: pytest.MonkeyPatch):
        adapter, event = await _media_resolve_make_event({}, [
            {"type": "image", "data": {"file": "9f2c.image", "url": "https://cdn/expired.png"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        adapter._bot.get_image.side_effect = ActionFailed({"retcode": 10002})
        _patch_download(monkeypatch, [RuntimeError("HTTP 403")])
        selection = select_media(event, "none")

        results = await resolve_media(event, selection)

        assert [item.status for item in results] == ["failed"]
        assert [item.reason for item in results] == ["image_unavailable"]
        assert cast(Image, event.get_messages()[0]).resolved_path == ""

    @pytest.mark.asyncio
    async def test_image_absent_from_cache_degrades(self, monkeypatch: pytest.MonkeyPatch):
        adapter, event = await _media_resolve_make_event({}, [
            {"type": "image", "data": {"file": "9f2c.image", "url": "https://cdn/expired.png"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        # 实现返回空地址表示图片已不在其缓存中, 无法恢复
        adapter._bot.get_image.return_value = {"file": "", "url": ""}
        _patch_download(monkeypatch, [RuntimeError("HTTP 403")])
        selection = select_media(event, "none")

        results = await resolve_media(event, selection)

        assert [item.status for item in results] == ["failed"]
        assert [item.reason for item in results] == ["image_unavailable"]

    @pytest.mark.asyncio
    async def test_video_never_calls_refresh_action(self, monkeypatch: pytest.MonkeyPatch):
        adapter, event = await _media_resolve_make_event({}, [
            {"type": "video", "data": {"file": "9f2c.mp4", "url": "https://cdn/v.mp4"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        _patch_download(monkeypatch, [RuntimeError("HTTP 404")])
        selection = select_media(event, "none")

        results = await resolve_media(event, selection)

        # 实现不提供视频回源动作, 不得照搬图片方案
        adapter._bot.get_image.assert_not_awaited()
        assert [item.status for item in results] == ["failed"]
        assert [item.reason for item in results] == ["video_download_failed"]

    @pytest.mark.asyncio
    async def test_budget_dropped_media_is_never_downloaded(self, monkeypatch: pytest.MonkeyPatch):
        adapter, event = await _media_resolve_make_event({"input_media_limit": 1}, [
            {"type": "image", "data": {"url": "https://cdn/a.png"}},
            {"type": "image", "data": {"url": "https://cdn/b.png"}},
            {"type": "image", "data": {"url": "https://cdn/c.png"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        calls = _patch_download(monkeypatch, [PNG])
        selection = select_media(event, "none")

        results = await resolve_media(event, selection)

        assert selection.sources("image") == ["https://cdn/a.png"]
        assert calls == ["https://cdn/a.png"]
        assert [item.status for item in results] == ["resolved"]
        event.cleanup_temporary_local_files()

    @pytest.mark.asyncio
    async def test_shared_source_is_resolved_once_for_all_components(self, monkeypatch: pytest.MonkeyPatch):
        adapter, event = await _media_resolve_make_event({}, [
            {"type": "image", "data": {"url": "https://cdn/same.png"}},
            {"type": "image", "data": {"url": "https://cdn/same.png"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        calls = _patch_download(monkeypatch, [PNG])
        selection = select_media(event, "none")

        results = await resolve_media(event, selection)

        assert calls == ["https://cdn/same.png"]
        images = [c for c in event.get_messages() if isinstance(c, Image)]
        # 同一来源的每个组件都要写回, 否则投影按来源去重会漏掉未写回的那个
        assert len(images) == 2
        assert images[0].resolved_path == images[1].resolved_path != ""
        event.cleanup_temporary_local_files()

    @pytest.mark.asyncio
    async def test_non_onebot_adapter_is_left_alone(self):
        class _ForeignAdapter:
            """非 OneBot 适配器只用于确认解析阶段被跳过"""

        class _ForeignEvent:
            """非 OneBot 事件替身, 只提供解析阶段会访问的属性"""

            adapter: object = _ForeignAdapter()
            policy_settings: dict[str, Any] = {"input_media_limit": 4}

            def get_messages(self) -> list[BaseMessageComponent]:
                """返回一条带地址的图片组件"""
                return [Image(file="a.image", url="https://cdn/a.png")]

        event = cast(MessageEvent, _ForeignEvent())
        selection = select_media(event, "none")

        assert await resolve_media(event, selection) == ()
        assert cast(Image, event.get_messages()[0]).resolved_path == ""

class TestFailedMediaProjection:
    """解析失败的媒体必须显式投影, 不能只丢图留下原占位"""

    @pytest.mark.asyncio
    async def test_mixed_text_keeps_going_with_failure_marker(self):
        adapter, event = await _media_resolve_make_event({"input_media_limit": 4}, [
            {"type": "text", "data": {"text": "这张图是什么 "}},
            {"type": "image", "data": {"url": "https://cdn/a.png"}},
            {"type": "image", "data": {"url": "https://cdn/b.png"}},
        ])
        images = [c for c in event.get_messages() if isinstance(c, Image)]
        images[1].resolved_path = "C:/tmp/ok.png"
        event.set_extra("media_resolution", (
            MediaResult("https://cdn/a.png", "image", "failed", "image_unavailable"),
            MediaResult("https://cdn/b.png", "image", "resolved", "", "C:/tmp/ok.png"),
        ))

        projected = project_input(event, "none")

        # 失败的那个占位被覆盖, 成功的那个保留原占位, 二者一一对齐
        assert "[图片读取失败]" in projected.message
        assert projected.message.replace("[图片读取失败]", "").count("[图片]") == 1
        assert projected.images == ("C:/tmp/ok.png",)
        assert projected.media_only_unavailable is False
        assert "media_failed:image_unavailable" in projected.notes

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind,url,reason,marker", [
        ("image", "https://cdn/a.png", "image_unavailable", "[图片读取失败]"),
        ("video", "https://cdn/v.mp4", "video_download_failed", "[视频读取失败]"),
    ])
    async def test_pure_failed_media_is_flagged_for_deterministic_feedback(self, kind, url, reason, marker):
        adapter, event = await _media_resolve_make_event({"input_media_limit": 4}, [
            {"type": kind, "data": {"url": url}},
        ])
        event.set_extra("media_resolution", (MediaResult(url, kind, "failed", reason),))

        projected = project_input(event, "none")

        assert (projected.images if kind == "image" else projected.videos) == ()
        assert projected.message.strip() == f"[用户 123, 消息 77] {marker}"
        assert projected.media_only_unavailable is True

    @pytest.mark.asyncio
    async def test_quoted_and_forwarded_media_failures_are_marked_in_context_block(self):
        adapter, event = await _media_resolve_make_event({"input_media_limit": 4}, [
            {"type": "reply", "data": {"id": "5"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        adapter._bot.get_msg.return_value = {
            "message_id": 5, "group_id": 456, "message_type": "group", "self_id": 10000,
            "sender": {"user_id": 321, "nickname": "小明"}, "time": 1700000000,
            "message": [{"type": "image", "data": {"url": "https://cdn/q.png"}}],
        }
        quote_status = await resolve_quotes(event)
        event.set_extra("media_resolution", (
            MediaResult("https://cdn/q.png", "image", "failed", "image_url_refreshed"),
        ))
        projected = project_input(event, quote_status)
        assert "[图片读取失败]" in projected.message
        assert projected.media_only_unavailable is False

        adapter, event = await _media_resolve_make_event({"input_media_limit": 4}, [
            {"type": "forward", "data": {"id": "f1"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        adapter._bot.get_forward_msg.return_value = {"messages": [
            {"type": "node", "data": {"nickname": "小红", "user_id": 654, "content": [
                {"type": "image", "data": {"url": "https://cdn/bad.png"}},
            ]}},
            {"type": "node", "data": {"nickname": "小蓝", "user_id": 655, "content": [
                {"type": "image", "data": {"url": "https://cdn/ok.png"}},
            ]}},
        ]}
        forward_status = await resolve_forwards(event)
        event.set_extra("media_resolution", (
            MediaResult("https://cdn/bad.png", "image", "failed", "image_unavailable"),
        ))
        projected = project_input(event, "none", forward_status)
        # 未失败的节点必须保持平台层的原占位, 不能退化成类型名
        assert "- 小红: [图片读取失败]" in projected.message
        assert "- 小蓝: [图片]" in projected.message
        assert "[image]" not in projected.message
        assert projected.media_only_unavailable is False

@pytest.fixture
def media_policy(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(paths, "_configured_media_roots", (str(allowed),))
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path / "data")
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    return allowed

@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["picture#1.png", "picture%20name.png", "中文 图片.png"])
async def test_standard_uri_round_trip(media_policy, filename):
    picture = media_policy / filename
    picture.write_bytes(b"png")
    image = Image.fromFileSystem(str(picture))
    assert image.file == picture.as_uri()
    assert paths.file_uri_to_path(image.file) == str(picture).replace("\\", "/")
    assert await image.convert_to_file_path() == str(picture.resolve())
    assert await image.convert_to_base64() == base64.b64encode(b"png").decode()
    assert _normalize_file_source(str(picture)) == picture.as_uri()

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "file_url", "missing", "video_forward", "uppercase", "single_slash", "invalid_utf8"])
async def test_onebot_denies_before_platform_call(tmp_path, media_policy, kind):
    outside = tmp_path / "outside.png"
    source = outside.as_uri()
    component = File("outside", file=source)
    if kind == "file_url":
        component = File("outside", url=source)
    elif kind == "missing":
        component = File("outside", file=str(outside))
    elif kind == "video_forward":
        component = Nodes([Node(content=[Video(source)])])
    elif kind == "uppercase":
        component = Image(file="FILE:" + source[5:])
    elif kind == "single_slash":
        component = Image(file=source.replace("file:///", "file:/", 1))
    elif kind == "invalid_utf8":
        source = "file:///tmp/%FF.png"
        with pytest.raises(paths.MediaSourcePermissionError) as caught:
            paths.file_uri_to_path(source)
        assert isinstance(caught.value.__cause__, UnicodeDecodeError)
        component = Image(file=source)
    adapter = OneBotAdapter(PlatformConfig(id="test", type="onebot", settings={"host": "127.0.0.1"}))
    adapter._bot = AsyncMock()
    adapter._dispatch_action = AsyncMock()
    receipt = await adapter.send_message("group%456", MessageChain([component]))
    assert receipt.status == "failed" and receipt.reason == "media_source_denied"
    adapter._dispatch_action.assert_not_awaited()
    assert adapter._bot.method_calls == []

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "file", "file_url", "duck"])
async def test_misskey_denies_before_upload(tmp_path, media_policy, kind):
    source = str(tmp_path / "outside.png")
    components = {
        "image": Image(file=source),
        "file": File("outside", file=source),
        "file_url": File("outside", url=Path(source).as_uri()),
        "duck": SimpleNamespace(source=source),
    }
    adapter = MisskeyAdapter(PlatformConfig(id="test", type="misskey", settings={
        "misskey_instance_url": "https://misskey.example", "misskey_token": "token",
    }))
    adapter._client = AsyncMock()
    with pytest.raises(paths.MediaSourcePermissionError):
        await adapter._upload_component(components[kind], asyncio.Semaphore(1))
    assert adapter._client.method_calls == []

@pytest.mark.asyncio
async def test_misskey_keeps_remote_and_converts_inline(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path / "data")
    assert await resolve_component_url_or_path(Image(file="HTTPS://example.com/a.png")) == (
        "https://example.com/a.png", None,
    )
    assert Image.fromURL("HTTPS://example.com/a.png").file == "https://example.com/a.png"
    image = Image.fromBase64(base64.b64encode(b"png").decode())
    remote, local = await resolve_component_url_or_path(image)
    assert remote is None and Path(local).read_bytes() == b"png"

@pytest.mark.parametrize("relative", [
    "platforms/p/sessions/s/uploads/a.png",
    "platforms/p/sessions/s/artifacts/a.png",
])
def test_default_layout_media_only(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path)
    assert paths.ensure_allowed_media_path(str(tmp_path / relative)) == str((tmp_path / relative).resolve())

@pytest.mark.parametrize("relative", [
    "platforms/p/trash/sessions/old/uploads/private.png",
    "platforms/p/users/u/cache/private.png",
    "platforms/p/platform.db",
])
def test_default_layout_denies_named_ancestors(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path)
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(tmp_path / relative))

def test_default_policy_denies_other_app_temp(tmp_path, monkeypatch):
    import tempfile
    other_app_file = Path(tempfile.gettempdir()) / "satrap-policy-probe-other-app" / "private.tmp"
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path / "data")
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    assert paths.get_allowed_media_roots() == [(paths.get_data_dir() / "sandbox").resolve()]
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(other_app_file))
    sandbox = paths.get_data_dir() / "sandbox" / "picture.png"
    assert paths.ensure_allowed_media_path(str(sandbox)) == str(sandbox.resolve())

@pytest.mark.parametrize("mode", ["configured", "environment"])
def test_temp_subdirectory_requires_explicit_authorization(tmp_path, monkeypatch, mode):
    allowed = tmp_path / "chosen-media"
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path / "data")
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    if mode == "configured":
        paths.set_media_allowed_roots([str(allowed)])
    else:
        monkeypatch.setenv("SATRAP_EXTRA_MEDIA_ROOTS", str(allowed))
    assert paths.ensure_allowed_media_path(str(allowed / "a.png")) == str((allowed / "a.png").resolve())
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(tmp_path / "unselected" / "private.tmp"))

def test_explicit_roots_preserve_semicolon_and_replace_temp(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed;media"
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    paths.set_media_allowed_roots([str(allowed)])
    assert paths.get_allowed_media_roots() == [allowed.resolve()]
    assert paths.ensure_allowed_media_path(str(allowed / "a.png")) == str((allowed / "a.png").resolve())
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(tmp_path / "other.png"))

def test_backend_root_controls_generated_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", None)
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    backend = BackendManager(BackendConfig(data_root=str(tmp_path / "custom-data")))
    expected = backend.storage_layout.platform_cache("local") / "temp"
    assert Path(get_satrap_temp_path()) == expected
    assert paths.ensure_allowed_media_path(str(expected / "a.png")) == str(expected / "a.png")
    old = StorageLayout().platform_cache("local") / "temp" / "a.png"
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(old))

@pytest.mark.parametrize("value", ["D:/media", [1], {"path": "media"}, False])
def test_backend_rejects_invalid_media_config(value):
    with pytest.raises(ValueError, match="media_allowed_roots"):
        BackendConfig.from_dict({"media_allowed_roots": value})

@pytest.mark.parametrize("source", ["file:relative", "file:///a#b", "file:///a?b", "file:///a%00b"])
def test_invalid_file_uri_rejected(source):
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.file_uri_to_path(source)

@pytest.mark.asyncio
async def test_default_cache_keeps_download_and_inline_media(tmp_path, monkeypatch):
    from satrap.core.components import message
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path / "data")
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    cache = Path(get_satrap_temp_path())

    async def download(url, target):
        Path(target).write_bytes(b"downloaded")
        return target

    monkeypatch.setattr(message, "download_file", download)
    downloaded = Path(await File("a.png", url="https://example.com/a.png").get_file())
    inline = Path(await Image.fromBase64(base64.b64encode(b"inline").decode()).convert_to_file_path())
    assert downloaded.parent == inline.parent == cache
    assert downloaded.read_bytes() == b"downloaded"
    assert inline.read_bytes() == b"inline"

@pytest.mark.asyncio
async def test_file_preserves_url_priority_and_missing_local_fallback(media_policy, monkeypatch):
    from satrap.core.components import message
    existing = media_policy / "existing.txt"
    existing.write_text("local", encoding="utf-8")
    component = File("file", file=str(existing), url="https://example.com/a.txt")
    assert await component.get_file(True) == "https://example.com/a.txt"
    assert await component.get_file() == str(existing)
    assert component.file == str(existing)
    missing = media_policy / "missing.txt"
    component.file_ = str(missing)
    monkeypatch.setattr(message, "get_satrap_temp_path", lambda: str(media_policy))
    download = AsyncMock(return_value=str(existing))
    monkeypatch.setattr(message, "download_file", download)
    assert await component.get_file() == str(existing)
    assert download.await_args.args[0] == "https://example.com/a.txt"

@pytest.mark.parametrize("denied", [False, True])
def test_sync_file_keeps_download_failure_contract(media_policy, monkeypatch, denied):
    from satrap.core.components import message
    error = paths.MediaSourcePermissionError("denied") if denied else OSError("download failed")
    monkeypatch.setattr(message, "get_satrap_temp_path", lambda: str(media_policy))
    monkeypatch.setattr(message, "download_file", AsyncMock(side_effect=error))
    component = File("file", url="https://example.com/a.txt")
    if denied:
        with pytest.raises(paths.MediaSourcePermissionError):
            component.file
    else:
        assert component.file == ""

@pytest.mark.asyncio
async def test_allowed_onebot_paths_reach_platform_even_if_remote_only(media_policy):
    adapter = OneBotAdapter(PlatformConfig(id="test", type="onebot", settings={"host": "127.0.0.1"}))
    adapter._bot = AsyncMock()
    adapter._dispatch_action = AsyncMock(return_value={"file_id": "f1"})
    local = media_policy / "remote-only.txt"
    assert not local.exists()
    result = await adapter.send_message("group%456", MessageChain([File("file", file=str(local))]))
    assert result.status == "success"
    assert adapter._dispatch_action.await_args.kwargs["file"] == str(local)

@pytest.mark.asyncio
async def test_misskey_duck_get_file_retains_contract(media_policy):
    getter = AsyncMock(return_value="https://example.com/a.png")
    assert await resolve_component_url_or_path(SimpleNamespace(get_file=getter)) == (
        "https://example.com/a.png", None,
    )
    getter.assert_awaited_once_with(True)
    denied = SimpleNamespace(get_file=AsyncMock(side_effect=paths.MediaSourcePermissionError("denied")), url="https://example.com/a.png")
    with pytest.raises(paths.MediaSourcePermissionError):
        await resolve_component_url_or_path(denied)

def test_file_authority_platform_contract(monkeypatch):
    monkeypatch.setattr(paths.os, "name", "posix")
    with pytest.raises(paths.MediaSourcePermissionError):
        _normalize_file_source("file://server/share/a.png")
    assert paths.file_uri_to_path("FILE:/var/tmp/a%20b.png") == "/var/tmp/a b.png"
    monkeypatch.setattr(paths.os, "name", "nt")
    assert paths.file_uri_to_path("file://server/share/a.png") == r"\\server\share\a.png"
    assert paths.file_uri_to_path("file://localhost/C:/a.png") == "C:/a.png"


def quoted(message_id: int = 5, group_id: int = 456, sender: int = 321, text: str = "原文", **extra: object) -> dict[str, object]:
    return {"message_id": message_id, "group_id": group_id, "message_type": "group", "self_id": 10000,
            "sender": {"user_id": sender, "nickname": "小明"}, "time": 1700000000,
            "message": [{"type": "text", "data": {"text": text}}], **extra}

async def _quote_projection_make_event(settings: dict[str, object] | None = None, segments: list[dict[str, object]] | None = None,
                     private: bool = False):
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings or {})))
    adapter._bot = AsyncMock()
    adapter._bot.send_group_msg.return_value = {"message_id": 1}
    segments = segments or [{"type": "reply", "data": {"id": "5"}}, {"type": "at", "data": {"qq": "10000"}}, {"type": "text", "data": {"text": " 这个对吗"}}]
    base: dict[str, object] = {"self_id": 10000, "user_id": 123, "message_id": 77, "message": segments}
    if private:
        await adapter._handle_private_message({**base, "message_type": "private", "sender": {"user_id": 123}})
    else:
        await adapter._handle_group_message({**base, "group_id": 456, "message_type": "group"})
    return adapter, adapter._event_queue.get_nowait()

@pytest.mark.asyncio
async def test_quote_is_resolved_into_reply_fields_and_projected_with_marker():
    adapter, event = await _quote_projection_make_event()
    adapter._bot.get_msg.return_value = quoted()
    status = await resolve_quotes(event)
    assert status == "resolved"
    reply = event.get_messages()[0]
    assert isinstance(reply, Reply) and reply.message_str == "原文" and reply.sender_nickname == "小明" and reply.sender_id == "321"
    projected = project_input(event, status)
    assert projected.message == "[引用 小明 的消息: 原文]\n[用户 123, 消息 77] @10000 这个对吗"
    adapter._bot.get_msg.assert_awaited_once_with(message_id=5)

@pytest.mark.asyncio
async def test_scheduler_passes_quote_to_session_and_quote_alone_reaches_model():
    adapter, event = await _quote_projection_make_event(segments=[{"type": "reply", "data": {"id": "5"}}, {"type": "at", "data": {"qq": "10000"}}])
    adapter._bot.get_msg.return_value = quoted(text="帮我看这个")
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
    manager.handle_call_async.return_value = "好的"
    await PipelineScheduler(manager).execute(event)
    call = manager.handle_call_async.call_args.args[0]
    assert call.message.startswith("[引用 小明 的消息: 帮我看这个]")
    assert event.get_extra("input_projection").quote_status == "resolved"

@pytest.mark.asyncio
async def test_quote_lookup_happens_after_wake_and_not_for_unwoken_messages():
    adapter, event = await _quote_projection_make_event(segments=[{"type": "reply", "data": {"id": "5"}}, {"type": "text", "data": {"text": "没叫机器人"}}])
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
    await PipelineScheduler(manager).execute(event)
    adapter._bot.get_msg.assert_not_awaited()
    manager.handle_call_async.assert_not_awaited()

@pytest.mark.asyncio
async def test_unavailable_quote_keeps_current_message_and_marks_status():
    adapter, event = await _quote_projection_make_event()
    adapter._bot.get_msg.side_effect = RuntimeError("boom")
    status = await resolve_quotes(event)
    projected = project_input(event, status)
    assert status == "unavailable"
    assert projected.message == "[引用了一条无法获取原文的消息]\n[用户 123, 消息 77] @10000 这个对吗"
    assert "quote_unavailable" in projected.notes

@pytest.mark.asyncio
async def test_cross_group_and_foreign_account_quotes_are_rejected():
    adapter, event = await _quote_projection_make_event()
    adapter._bot.get_msg.return_value = quoted(group_id=999)
    assert await resolve_quotes(event) == "unavailable"
    adapter._bot.get_msg.return_value = quoted(self_id=20000)
    assert await resolve_quotes(event) == "unavailable"
    adapter._bot.get_msg.return_value = "not a dict"
    assert await resolve_quotes(event) == "unavailable"

@pytest.mark.asyncio
async def test_private_quote_must_belong_to_the_same_dialogue():
    adapter, event = await _quote_projection_make_event(private=True)
    adapter._bot.get_msg.return_value = {**quoted(sender=123), "group_id": "", "message_type": "private"}
    assert await resolve_quotes(event) == "resolved"
    adapter, event = await _quote_projection_make_event(private=True)
    adapter._bot.get_msg.return_value = {**quoted(sender=555), "group_id": "", "message_type": "private"}
    assert await resolve_quotes(event) == "unavailable"

@pytest.mark.asyncio
async def test_whitelist_tightening_blocks_lookup_and_lookup_disabled_by_policy():
    adapter, event = await _quote_projection_make_event()
    adapter.config.settings["group_whitelist"] = ["789"]
    adapter._bot.get_msg.return_value = quoted()
    assert await resolve_quotes(event) == "unavailable"
    adapter._bot.get_msg.assert_not_awaited()
    adapter, event = await _quote_projection_make_event(settings={"quote_lookup": False})
    assert await resolve_quotes(event) == "disabled"
    adapter._bot.get_msg.assert_not_awaited()
    assert "quote_disabled" in project_input(event, "disabled").notes

@pytest.mark.asyncio
async def test_lookup_timeout_and_oversized_payload_do_not_block():
    adapter, event = await _quote_projection_make_event()

    async def slow(**kwargs: object):
        await asyncio.sleep(10)

    adapter._bot.get_msg.side_effect = slow
    adapter._message_lookup_slots = asyncio.Semaphore(4)
    original = asyncio.wait_for

    async def fast_wait(awaitable: Awaitable[Any], timeout: float) -> Any:
        return await original(awaitable, 0.05)

    import satrap.core.platform.onebot.adapter as module
    module.asyncio.wait_for = fast_wait
    try:
        assert await resolve_quotes(event) == "unavailable"
    finally:
        module.asyncio.wait_for = original
    adapter, event = await _quote_projection_make_event()
    adapter._bot.get_msg.return_value = quoted(text="x" * 70000)
    assert await resolve_quotes(event) == "unavailable"

@pytest.mark.asyncio
async def test_quoted_media_is_merged_with_budget_and_text_truncated():
    adapter, event = await _quote_projection_make_event({"input_media_limit": 4})
    media: list[dict[str, object]] = [{"type": "image", "data": {"url": f"http://x/{i}.png"}} for i in range(6)]
    adapter._bot.get_msg.return_value = {**quoted(text="y" * (QUOTE_TEXT_LIMIT + 10)), "message": media + [{"type": "text", "data": {"text": "y" * (QUOTE_TEXT_LIMIT + 10)}}]}
    status = await resolve_quotes(event)
    projected = project_input(event, status)
    # 引用媒体与顶层共享 input_media_limit 总额度
    assert len(projected.images) == 4
    assert "quote_media_truncated" in projected.notes and "quote_truncated" in projected.notes
    header = projected.message.split(chr(10))[0]
    assert header.endswith("…]") and len(header) < QUOTE_TEXT_LIMIT + 60

@pytest.mark.asyncio
async def test_nested_reply_is_not_recursively_fetched_and_only_first_reply_resolved():
    adapter, event = await _quote_projection_make_event(segments=[{"type": "reply", "data": {"id": "5"}}, {"type": "reply", "data": {"id": "6"}}, {"type": "at", "data": {"qq": "10000"}}])
    adapter._bot.get_msg.return_value = {**quoted(), "message": [{"type": "reply", "data": {"id": "4"}}, {"type": "text", "data": {"text": "二级"}}]}
    assert await resolve_quotes(event) == "resolved"
    assert adapter._bot.get_msg.await_count == 1
    assert await resolve_quotes(event) == "resolved"
    assert adapter._bot.get_msg.await_count == 1

@pytest.mark.asyncio
async def test_quote_of_bot_message_is_labelled_and_not_treated_as_wake():
    adapter, event = await _quote_projection_make_event(segments=[{"type": "reply", "data": {"id": "5"}}, {"type": "text", "data": {"text": "继续"}}])
    adapter._bot.get_msg.return_value = quoted(sender=10000)
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
    await PipelineScheduler(manager).execute(event)
    manager.handle_call_async.assert_not_awaited()
    projected = project_input(event, await resolve_quotes(event))
    assert projected.message.startswith("[引用 机器人自己 的消息: 原文]")

@pytest.mark.parametrize("value", ["false", 0])
def test_quote_lookup_must_be_boolean(value: object):
    with pytest.raises(ValueError, match="quote_lookup"):
        validate_wake_policy({"quote_lookup": value})

@pytest.mark.asyncio
async def test_quote_self_wake_is_opt_in_and_uses_single_lookup():
    segments: list[dict[str, object]] = [{"type": "reply", "data": {"id": "5"}}, {"type": "text", "data": {"text": "继续说"}}]
    adapter, event = await _quote_projection_make_event(settings={"wake_on_quote_self": True}, segments=segments)
    adapter._bot.get_msg.return_value = quoted(sender=10000)
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
    manager.handle_call_async.return_value = ""
    await PipelineScheduler(manager).execute(event)
    assert manager.handle_call_async.await_count == 1
    assert adapter._bot.get_msg.await_count == 1
    assert event.get_extra("wake_decision").rule == "quote_self"
    assert manager.handle_call_async.call_args.args[0].message.startswith("[引用 机器人自己 的消息: 原文]")

@pytest.mark.asyncio
async def test_quote_of_other_user_or_failed_lookup_does_not_wake():
    segments: list[dict[str, object]] = [{"type": "reply", "data": {"id": "5"}}, {"type": "text", "data": {"text": "继续说"}}]
    adapter, event = await _quote_projection_make_event(settings={"wake_on_quote_self": True}, segments=segments)
    adapter._bot.get_msg.return_value = quoted(sender=321)
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
    await PipelineScheduler(manager).execute(event)
    manager.handle_call_async.assert_not_awaited()
    adapter, event = await _quote_projection_make_event(settings={"wake_on_quote_self": True}, segments=segments)
    adapter._bot.get_msg.side_effect = RuntimeError("down")
    await PipelineScheduler(manager).execute(event)
    manager.handle_call_async.assert_not_awaited()
    assert event.get_extra("wake_decision").rule == "no_match"

@pytest.mark.asyncio
async def test_quote_self_wake_respects_whitelist_and_does_not_lookup_when_disabled():
    segments: list[dict[str, object]] = [{"type": "reply", "data": {"id": "5"}}, {"type": "text", "data": {"text": "继续说"}}]
    adapter, event = await _quote_projection_make_event(settings={"wake_on_quote_self": True}, segments=segments)
    adapter.config.settings["group_whitelist"] = ["789"]
    adapter._bot.get_msg.return_value = quoted(sender=10000)
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
    await PipelineScheduler(manager).execute(event)
    adapter._bot.get_msg.assert_not_awaited()
    manager.handle_call_async.assert_not_awaited()

@pytest.mark.asyncio
async def test_quote_with_non_numeric_time_is_still_resolved(monkeypatch: pytest.MonkeyPatch):
    """回源结果的 time 非数字时引用仍解析, time 回退为 0"""
    adapter, event = await _quote_projection_make_event()

    async def fake_fetch(message_id: str, session_id: str) -> dict[str, object]:
        return {"components": [], "message_str": "原文", "sender_id": "321", "sender_nickname": "小明", "time": "abc"}

    monkeypatch.setattr(adapter, "fetch_quoted_message", fake_fetch)
    status = await resolve_quotes(event)
    assert status == "resolved"
    reply = event.get_messages()[0]
    assert isinstance(reply, Reply) and reply.time == 0 and reply.message_str == "原文"

def test_media_budget_checks_videos_after_images_exhausted():
    """图片耗尽预算只跳过图片, 视频仍按预算检查并记录说明"""
    from satrap.core.components import Image, Video
    from satrap.core.pipeline.input_projection import _MediaBudget

    images: list[str] = []
    videos: list[str] = []
    notes: list[str] = []
    budget = _MediaBudget(1, images, videos, notes)
    budget.merge([Image(file="img1"), Image(file="img2"), Video(file="vid1")], "note")
    assert images == ["img1"] and videos == []
    assert notes == ["note", "note"]

async def _attachments_make_event(segments: list[dict[str, object]], settings: dict[str, object] | None = None):
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings or {})))
    adapter._bot = AsyncMock()
    adapter._bot.send_group_msg.return_value = {"message_id": 1}
    # 默认模拟不提供 get_record/fetch_ptt_text 的实现, 需要时由测试覆盖 return_value
    adapter._bot.get_record.side_effect = ActionFailed({"retcode": 1404})
    adapter._bot.fetch_ptt_text.side_effect = ActionFailed({"retcode": 1404})
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
    adapter, event = await _attachments_make_event([record()], {"asr_model": "speech"})
    download, calls = fake_download({"https://media.example.com/voice.amr.wav": b"RIFF"})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    seen: dict[str, Any] = {}

    async def transcribe(data: bytes, filename: str, config: ASRConfig) -> str:
        seen.update(data=data, filename=filename, model=config.model)
        return "你好世界"

    monkeypatch.setattr(_attachments_module, "_transcribe", transcribe)
    results = await resolve_attachments(event, lambda name: asr_config() if name == "speech" else None)
    assert results == (AttachmentResult("record", "voice.amr.wav", "resolved", "你好世界"),)
    assert seen == {"data": b"RIFF", "filename": "voice.wav", "model": "whisper-1"}
    assert calls[0][1] == _attachments_module.AUDIO_MAX_BYTES
    assert event.get_messages()[1].text == "你好世界"
    projected = project_input(event, "none", "none", results)
    assert projected.message == "[语音 转写内容: 你好世界]\n[用户 123, 消息 77] @10000"
    assert projected.attachment_status == "resolved"
    again = await resolve_attachments(event, lambda name: asr_config())
    assert again[0].text == "你好世界" and len(calls) == 1

@pytest.mark.asyncio
async def test_download_verifies_tls_unless_host_is_trusted(monkeypatch: pytest.MonkeyPatch):
    # 默认始终校验
    adapter, event = await _attachments_make_event([file_segment()])
    download, calls = fake_download({"https://files.example.com/notes.txt": b"hello"})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    await resolve_attachments(event, None)
    assert calls[0][2:] == (True, True)
    # media_insecure_tls 对未登记主机不生效, 仍校验
    adapter, event = await _attachments_make_event([file_segment()], {"media_insecure_tls": True})
    await resolve_attachments(event, None)
    assert calls[1][2:] == (True, True)
    # 主机登记进 media_trusted_hosts 后才关闭校验
    adapter, event = await _attachments_make_event(
        [file_segment()], {"media_insecure_tls": True, "media_trusted_hosts": ["files.example.com"]})
    await resolve_attachments(event, None)
    assert calls[2][2:] == (False, True)

@pytest.mark.asyncio
async def test_temp_file_is_cleaned_up_when_write_fails(monkeypatch: pytest.MonkeyPatch):
    """写入在提取线程内完成, 失败时由辅助函数内部清理, 不向事件登记"""
    adapter, event = await _attachments_make_event([file_segment()])
    download, _ = fake_download({"https://files.example.com/notes.txt": b"hello"})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    tracked: list[str] = []
    monkeypatch.setattr(event, "track_temporary_local_file", tracked.append)

    class _Broken:
        name = os.path.join(os.getcwd(), "never-created.txt")

        def write(self, data: bytes) -> None:
            raise OSError("disk full")

        def close(self) -> None:
            pass

    monkeypatch.setattr(_attachments_module.tempfile, "NamedTemporaryFile", lambda **kwargs: _Broken())
    results = await resolve_attachments(event, None)
    assert results[0].status == "failed" and results[0].reason == "OSError"
    assert tracked == []

@pytest.mark.asyncio
async def test_voice_without_asr_binding_is_marked_disabled_and_not_downloaded(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await _attachments_make_event([record()])
    download, calls = fake_download({})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "disabled" and results[0].reason == "asr_not_configured" and not calls
    adapter, event = await _attachments_make_event([record()], {"asr_model": "missing"})
    results = await resolve_attachments(event, lambda name: None)
    assert results[0].reason == "asr_config_missing"
    projected = project_input(event, "none", "none", results)
    assert projected.message.startswith("[语音: 未启用转写]") and "attachment_disabled" in projected.notes

@pytest.mark.asyncio
async def test_voice_download_or_asr_failure_degrades_without_raising(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await _attachments_make_event([record("https://media.example.com/missing.wav")], {"asr_model": "speech"})
    download, _ = fake_download({})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "failed" and results[0].reason == "OutboundHTTPError"
    monkeypatch.undo()
    adapter, event = await _attachments_make_event([record("http://10.0.0.5/voice.wav")], {"asr_model": "speech"})
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "failed" and results[0].reason == "UnsafeOutboundURLError"
    adapter, event = await _attachments_make_event([record("file:///etc/passwd.wav")], {"asr_model": "speech"})
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "failed" and results[0].reason == "UnsafeOutboundURLError"
    adapter, event = await _attachments_make_event([record("https://media.example.com/voice.xyz")], {"asr_model": "speech"})
    download, _ = fake_download({"https://media.example.com/voice.xyz": b"\x00\x01garbage"})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "unsupported" and results[0].reason == "unknown_codec"

@pytest.mark.asyncio
async def test_voice_get_record_transcode_and_download_fallback(monkeypatch: pytest.MonkeyPatch):
    """优先用实现动作平台转码; 该动作失败时回落到下载并按内容识别后缀"""
    # 平台转码可用: 直接使用 get_record 的 base64
    adapter, event = await _attachments_make_event([record()], {"asr_model": "speech"})
    wav = b"RIFF" + bytes(4) + b"WAVEfmt "
    adapter._bot.get_record.side_effect = None
    adapter._bot.get_record.return_value = {"base64": base64.b64encode(wav).decode()}
    download, calls = fake_download({})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    seen: dict[str, Any] = {}

    async def transcribe(data: bytes, filename: str, config: ASRConfig) -> str:
        seen.update(data=data, filename=filename)
        return "平台转码"

    monkeypatch.setattr(_attachments_module, "_transcribe", transcribe)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "resolved" and results[0].text == "平台转码"
    assert seen == {"data": wav, "filename": "voice.wav"} and not calls
    adapter._bot.get_record.assert_awaited_once_with(file="https://media.example.com/voice.amr.wav", out_format="wav")

    # 平台转码失败: 下载原始字节, 文件名按内容识别
    adapter, event = await _attachments_make_event([record()], {"asr_model": "speech"})
    adapter._bot.get_record.side_effect = RuntimeError("network")
    download, calls = fake_download({"https://media.example.com/voice.amr.wav": b"OggS" + bytes(16)})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    seen = {}

    async def transcribe_fallback(data: bytes, filename: str, config: ASRConfig) -> str:
        seen["filename"] = filename
        return "ok"

    monkeypatch.setattr(_attachments_module, "_transcribe", transcribe_fallback)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "resolved" and seen["filename"] == "voice.ogg" and len(calls) == 1

@pytest.mark.asyncio
async def test_silk_voice_without_platform_transcode_is_marked(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await _attachments_make_event([record("https://media.example.com/abc.amr")], {"asr_model": "speech"})
    download, _ = fake_download({"https://media.example.com/abc.amr": b"\x02#!SILK_V3" + bytes(8)})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "unsupported" and results[0].reason == "silk_needs_platform_transcode"
    assert "SILK" in render_attachments(results)

@pytest.mark.asyncio
async def test_amr_voice_is_converted_locally(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await _attachments_make_event([record("https://media.example.com/abc.amr")], {"asr_model": "speech"})
    download, _ = fake_download({"https://media.example.com/abc.amr": b"#!AMR\n" + bytes(32)})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    def _convert(data: bytes, *, max_seconds: float) -> bytes:
        return b"RIFFconverted"

    monkeypatch.setattr(_attachments_module, "convert_to_wav", _convert)
    seen: dict[str, Any] = {}

    async def transcribe(data: bytes, filename: str, config: ASRConfig) -> str:
        seen.update(data=data, filename=filename)
        return "本地转码"

    monkeypatch.setattr(_attachments_module, "_transcribe", transcribe)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].text == "本地转码" and seen == {"data": b"RIFFconverted", "filename": "voice.wav"}
    monkeypatch.setitem(audio_convert._AV_STATE, "module", None)
    adapter, event = await _attachments_make_event([record("https://media.example.com/abc.amr")], {"asr_model": "speech"})
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "unsupported" and results[0].reason == "av_missing"
    assert "av 包" in render_attachments(results)

@pytest.mark.asyncio
async def test_voice_transcribe_modes(monkeypatch: pytest.MonkeyPatch):
    download, calls = fake_download({})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    adapter, event = await _attachments_make_event([record()], {"asr_model": "speech", "voice_transcribe": "off"})
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "disabled" and results[0].reason == "voice_transcribe_off"
    adapter, event = await _attachments_make_event([record()], {"voice_transcribe": "platform"})
    adapter._bot.fetch_ptt_text.side_effect = None
    adapter._bot.fetch_ptt_text.return_value = {"text": "原生转写"}
    results = await resolve_attachments(event, None)
    assert results[0].status == "resolved" and results[0].text == "原生转写" and not calls
    adapter._bot.fetch_ptt_text.assert_awaited_once_with(message_id=77)
    adapter, event = await _attachments_make_event([record()], {"voice_transcribe": "platform"})
    results = await resolve_attachments(event, None)
    assert results[0].status == "unsupported" and results[0].reason == "platform_transcribe_unavailable"
    adapter, event = await _attachments_make_event([record()], {"asr_model": "speech", "voice_transcribe": "asr_then_platform"})
    adapter._bot.fetch_ptt_text.side_effect = None
    adapter._bot.fetch_ptt_text.return_value = {"text": "兜底"}
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "resolved" and results[0].text == "兜底" and len(calls) == 1
    adapter, event = await _attachments_make_event([record()], {"asr_model": "speech", "voice_transcribe": "asr_then_platform"})
    adapter._bot.fetch_ptt_text.side_effect = AdminActionUnconfirmed("timeout")
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "failed" and results[0].reason == "OutboundHTTPError"

@pytest.mark.asyncio
async def test_file_is_downloaded_extracted_and_temp_file_cleaned(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await _attachments_make_event([file_segment()])
    download, calls = fake_download({"https://files.example.com/notes.txt": "第一行\n第二行".encode("utf-8")})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    results = await resolve_attachments(event, None)
    assert results[0].status == "resolved" and "第一行" in results[0].text and "第二行" in results[0].text
    assert calls[0][1] == _attachments_module.FILE_MAX_BYTES
    temp_paths = list(event._temporary_local_files)
    assert len(temp_paths) == 1 and os.path.exists(temp_paths[0])
    event.cleanup_temporary_local_files()
    assert not os.path.exists(temp_paths[0])
    projected = project_input(event, "none", "none", results)
    assert projected.message.startswith("[文件 notes.txt 内容:\n第一行") and projected.message.endswith("@10000")

@pytest.mark.asyncio
async def test_file_unsupported_disabled_and_limit(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await _attachments_make_event([file_segment("archive.zip", "https://files.example.com/archive.zip")])
    download, calls = fake_download({})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    results = await resolve_attachments(event, None)
    assert results[0].status == "unsupported" and not calls
    adapter, event = await _attachments_make_event([file_segment()], {"attachment_extract": False})
    results = await resolve_attachments(event, None)
    assert results[0].status == "disabled" and not calls
    adapter, event = await _attachments_make_event([file_segment(f"n{i}.txt", f"https://files.example.com/n{i}.txt") for i in range(6)])
    download, calls = fake_download({f"https://files.example.com/n{i}.txt": b"x" for i in range(6)})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    results = await resolve_attachments(event, None)
    assert [r.status for r in results] == ["resolved"] * 4 + ["skipped", "skipped"]
    assert len(calls) == 4
    projected = project_input(event, "none", "none", results)
    assert projected.attachment_status == "partial" and projected.message.count("[文件") == 6

@pytest.mark.asyncio
async def test_scheduler_passes_attachment_context_to_session(monkeypatch: pytest.MonkeyPatch):
    adapter, event = await _attachments_make_event([record()], {"asr_model": "speech"})
    download, _ = fake_download({"https://media.example.com/voice.amr.wav": b"RIFF"})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)

    async def transcribe(data: bytes, filename: str, config: ASRConfig) -> str:
        return "请帮我总结"

    monkeypatch.setattr(_attachments_module, "_transcribe", transcribe)
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
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
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
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

    resolver = asr_resolver_from_manager(cast(Any, _Manager()))
    assert resolver is not None and resolver("speech") is not None and resolver("other") is None
    assert asr_resolver_from_manager(None) is None
    assert asr_resolver_from_manager(cast(Any, object())) is None

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
    monkeypatch.setattr(_attachments_module, "build_asr_from_config", _stub_build(_StubASR()))
    assert await _attachments_module._transcribe(b"x", "a.wav", ASRConfig()) == "你好"

@pytest.mark.asyncio
async def test_transcribe_close_failure_preserves_original_error(monkeypatch: pytest.MonkeyPatch):
    """转写与关闭同时失败时抛出原始转写异常"""
    monkeypatch.setattr(_attachments_module, "build_asr_from_config", _stub_build(_StubASR(error=RuntimeError("transcribe boom"))))
    with pytest.raises(RuntimeError, match="transcribe boom"):
        await _attachments_module._transcribe(b"x", "a.wav", ASRConfig())

@pytest.mark.asyncio
async def test_attachment_total_budget_marks_remaining(monkeypatch: pytest.MonkeyPatch):
    """事件级总预算耗尽后, 其余附件标记 attachment_budget_exceeded 且不再下载"""
    now = [0.0]
    monkeypatch.setattr(_attachments_module, "monotonic", lambda: now[0])
    payload = {
        "https://files.example.com/a.txt": b"a",
        "https://files.example.com/b.txt": b"b",
        "https://files.example.com/c.txt": b"c",
    }
    download, calls = fake_download(payload)

    async def slow_download(url: str, **kwargs: Any) -> OutboundHTTPResponse:
        now[0] += 100.0
        return await download(url, **kwargs)

    monkeypatch.setattr(_attachments_module, "safe_async_get", slow_download)
    adapter, event = await _attachments_make_event([
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
    adapter, event = await _attachments_make_event([record()], {"asr_model": "speech"})
    download, _ = fake_download({"https://media.example.com/voice.amr.wav": b"RIFF"})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)

    async def transcribe(data: bytes, filename: str, config: ASRConfig) -> str:
        return "长" * (_attachments_module.TRANSCRIPT_LIMIT + 100)

    monkeypatch.setattr(_attachments_module, "_transcribe", transcribe)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert len(results[0].text) == _attachments_module.TRANSCRIPT_LIMIT
    record_comp = event.get_messages()[1]
    assert len(record_comp.text) == _attachments_module.TRANSCRIPT_LIMIT

@pytest.mark.asyncio
async def test_display_name_is_sanitized(monkeypatch: pytest.MonkeyPatch):
    """平台上报文件名的换行与 ANSI 转义不进入结果与渲染块"""
    evil = "evil\x1b[31m\nname.txt"
    adapter, event = await _attachments_make_event([file_segment(evil, "https://files.example.com/x.txt")])
    download, _ = fake_download({"https://files.example.com/x.txt": b"hello"})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    results = await resolve_attachments(event, None)
    assert results[0].name == "evil [31m name.txt"
    assert "\n" not in results[0].name and "\x1b" not in results[0].name
    rendered = render_attachments(results)
    assert "\x1b" not in rendered
    label_line = rendered.splitlines()[0]
    assert "evil [31m name.txt" in label_line
    assert _attachments_module._safe_display("长" * 100) == "长" * 80

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
        handle.writeframes(bytes(2 * 8000 * int(_attachments_module.AUDIO_MAX_SECONDS + 30)))
    adapter, event = await _attachments_make_event([record()], {"asr_model": "speech"})
    download, _ = fake_download({"https://media.example.com/voice.amr.wav": buffer.getvalue()})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    results = await resolve_attachments(event, lambda name: asr_config())
    assert results[0].status == "unsupported" and results[0].reason == "audio_too_long"

@pytest.mark.asyncio
async def test_plaintext_http_requires_switch_and_flows_through_resolve(monkeypatch: pytest.MonkeyPatch):
    """公网明文 http 默认拒绝, 开启 media_plaintext_http 或登记主机后放行, 逐事件设置经解析链生效"""
    calls: list[str] = []

    class _Resp:
        content = b"x"

        def raise_for_status(self) -> None:
            return None

    async def fake_get(url: str, **kwargs: Any) -> Any:
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(_attachments_module, "safe_async_get", fake_get)
    with pytest.raises(UnsafeOutboundURLError, match="https"):
        await _attachments_module._download("http://public.example.com/a.png", 100, ())
    with pytest.raises(UnsafeOutboundURLError, match="https"):
        await _attachments_module._download("http://nas.local/a.png", 100, ())
    await _attachments_module._download("http://nas.local/a.png", 100, ("nas.local",))
    await _attachments_module._download("http://public.example.com/a.png", 100, (), allow_plaintext=True)
    assert calls == ["http://nas.local/a.png", "http://public.example.com/a.png"]

    adapter, event = await _attachments_make_event(
        [file_segment("notes.txt", "http://files.example.com/notes.txt")])
    download, resolve_calls = fake_download({"http://files.example.com/notes.txt": b"hello"})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    results = await resolve_attachments(event, None)
    assert results[0].status == "failed" and results[0].reason == "UnsafeOutboundURLError"
    adapter, event = await _attachments_make_event(
        [file_segment("notes.txt", "http://files.example.com/notes.txt")], {"media_plaintext_http": True})
    results = await resolve_attachments(event, None)
    assert results[0].status == "resolved"
    assert len(resolve_calls) == 1

@pytest.mark.parametrize("settings", [{"media_plaintext_http": "yes"}, {"media_plaintext_http": 1}])
def test_plaintext_http_policy_must_be_boolean(settings: dict[str, object]):
    with pytest.raises(ValueError, match="media_plaintext_http"):
        validate_wake_policy(settings)

@pytest.mark.asyncio
async def test_qq_http_image_resolves_over_https_without_relaxing_network_policy(monkeypatch: pytest.MonkeyPatch):
    """QQ 图片升级 HTTPS 后进入正常投影, 保留参数和下载边界且不回退明文"""
    original = "http://gchat.qpic.cn:80/gchatpic_new/0/example/0?term=2&key=a%2Fb"
    secure = "https://gchat.qpic.cn/gchatpic_new/0/example/0?term=2&key=a%2Fb"
    download, calls = fake_download({secure: PNG})
    monkeypatch.setattr(_attachments_module, "safe_async_get", download)
    adapter, event = await _media_resolve_make_event({}, [
        {"type": "image", "data": {"file": "native.image", "url": original}},
    ])
    try:
        results = await resolve_media(event, select_media(event, "none"))
        assert results[0].status == "resolved"
        assert Path(cast(Image, event.get_messages()[0]).resolved_path).read_bytes() == PNG
        assert len(calls) == 1 and calls[0][0] == secure and calls[0][2:] == (True, True)
        adapter._bot.get_image.assert_not_awaited()
    finally:
        event.cleanup_temporary_local_files()
    for blocked in ["http://gchat.qpic.cn.attacker.example/a", "http://user@gchat.qpic.cn/a", "http://gchat.qpic.cn:8080/a"]:
        with pytest.raises(UnsafeOutboundURLError):
            await _attachments_module._download(blocked, 100, ())
    denied = AsyncMock(side_effect=UnsafeOutboundURLError("禁止访问非公网地址"))
    monkeypatch.setattr(_attachments_module, "safe_async_get", denied)
    with pytest.raises(UnsafeOutboundURLError):
        await _attachments_module._download(original, 100, (), verify_tls=False)
    assert denied.await_count == 1
    assert denied.call_args.args[0] == secure
    assert denied.call_args.kwargs["trusted_hosts"] == ()
    assert denied.call_args.kwargs["ssl_verify"] is True


def _wav_bytes(seconds: float = 0.5, rate: int = 8000) -> bytes:
    frames = int(seconds * rate)
    samples = array("h", (int(12000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(frames)))
    if sys.byteorder != "little":
        samples.byteswap()
    pcm = samples.tobytes()   # 连续缓冲区避免为长语音创建数百万个临时字节对象
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(pcm)) + pcm

def _amr_bytes(seconds: float = 0.5) -> bytes:
    av: Any = pytest.importorskip("av")
    output = io.BytesIO()
    with av.open(io.BytesIO(_wav_bytes(seconds))) as source, av.open(output, "w", format="amr") as sink:
        stream = sink.add_stream("libopencore_amrnb", rate=8000, layout="mono")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)
        for frame in source.decode(audio=0):
            for resampled in resampler.resample(frame):
                for packet in stream.encode(resampled):
                    sink.mux(packet)
        for packet in stream.encode(None):
            sink.mux(packet)
    return output.getvalue()

@pytest.mark.parametrize("payload, suffix, codec, accepted", [
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", ".amr", "wav", True),
    (b"OggS\x00\x02" + b"\x00" * 10, "", "ogg", True),
    (b"#!AMR\n\x3c\x48", ".amr", "amr", False),
    (b"\x00" * 12, ".mp3", "mp3", True),
])
def test_probe_identifies_codec_by_magic_then_suffix(payload: bytes, suffix: str, codec: str, accepted: bool):
    probe = probe_audio(payload, suffix)
    assert (probe.codec, probe.accepted) == (codec, accepted)

def test_probe_flags_silk_for_platform_transcode():
    # QQ 语音以 .amr 命名但内容是 SILK, 文件头优先于扩展名
    probe = probe_audio(b"\x02#!SILK_V3" + b"\x00" * 6, ".amr")
    assert probe == ("silk", False, False, "silk_needs_platform_transcode")

def test_probe_unknown_and_missing_converter(monkeypatch: pytest.MonkeyPatch):
    assert probe_audio(b"\x00" * 16, ".bin").reason == "unknown_codec"
    monkeypatch.setitem(_audio_convert_module._AV_STATE, "module", None)
    probe = probe_audio(b"#!AMR\n" + b"\x00" * 10, "")
    assert probe.codec == "amr" and not probe.convertible and probe.reason == "av_missing"
    with pytest.raises(AudioConvertError, match="av_missing"):
        convert_to_wav(b"#!AMR\n", max_seconds=10)

def test_amr_converts_to_16k_mono_wav():
    wav = convert_to_wav(_amr_bytes(), max_seconds=10)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    channels, rate = struct.unpack("<HI", wav[22:28])
    assert (channels, rate) == (1, 16000)
    assert probe_audio(wav).accepted

def test_conversion_rejects_overlong_and_garbage():
    with pytest.raises(AudioTooLong):
        convert_to_wav(_amr_bytes(seconds=1.0), max_seconds=0.2)
    with pytest.raises(AudioConvertError):
        convert_to_wav(b"#!AMR\n" + b"\xff" * 64, max_seconds=10)

def test_probe_duration_reads_wav_and_tolerates_garbage():
    short_duration = probe_duration(_wav_bytes(2.0), "wav")
    if short_duration is None:
        raise AssertionError("wav 时长应可探测")
    assert 1.99 < short_duration < 2.01
    long_duration = probe_duration(_wav_bytes(301.0), "wav")
    if long_duration is None:
        raise AssertionError("wav 时长应可探测")
    assert long_duration > 300
    assert probe_duration(b"not-a-wav", "wav") is None
    assert probe_duration(b"OggS" + bytes(64), "ogg") is None
