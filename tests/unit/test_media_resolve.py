"""入站图片与视频的 pipeline 解析阶段"""
from unittest.mock import AsyncMock
from typing import Any, cast
import os

from aiocqhttp.exceptions import ActionFailed
import pytest

from satrap.core.pipeline.input_projection import (
    _MediaBudget,
    media_sources,
    resolve_forwards,
    resolve_quotes,
    select_media,
)
from satrap.core.pipeline.media_resolve import MediaResult, resolve_media
from satrap.core.pipeline.input_projection import MEDIA_FAILED_FEEDBACK, project_input
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.components import BaseMessageComponent, Forward, Image, PlatformComponentType, Video
from satrap.core.platform.event import MessageEvent
from satrap.core.type import safe_getattr
from satrap.core.platform import PlatformConfig


def reference_selection(event: MessageEvent, quote_status: str) -> tuple[list[str], list[str], list[str]]:
    """
    按重构前的公开实现重算选中结果, 作为等价性基准

    参数:
    - event: 已完成引用与转发补全的事件
    - quote_status: resolve_quotes 的结果

    返回:
    - tuple[list[str], list[str], list[str]]: images, videos 与预算说明
    """
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


async def make_event(settings: dict[str, object], segments: list[dict[str, object]]):
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
    async def test_top_quote_forward_share_budget_identically(self):
        adapter, event = await make_event({"input_media_limit": 4}, [
            {"type": "reply", "data": {"id": "5"}},
            {"type": "forward", "data": {"id": "f1"}},
            {"type": "image", "data": {"url": "http://x/top.png"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        adapter._bot.get_msg.return_value = {
            "message_id": 5, "group_id": 456, "message_type": "group", "self_id": 10000,
            "sender": {"user_id": 321, "nickname": "小明"}, "time": 1700000000,
            "message": [{"type": "image", "data": {"url": f"http://x/q{i}.png"}} for i in range(3)],
        }
        adapter._bot.get_forward_msg.return_value = {"messages": [
            {"type": "node", "data": {"nickname": "小红", "user_id": 654, "content": [
                {"type": "image", "data": {"url": f"http://x/f{i}.png"}} for i in range(2)
            ]}},
        ]}
        quote_status = await resolve_quotes(event)
        await resolve_forwards(event)

        selection = select_media(event, quote_status)
        images, videos, notes = reference_selection(event, quote_status)

        assert selection.sources("image") == images
        assert selection.sources("video") == videos
        flat_notes = [*selection.top_notes, *selection.quote_notes]
        for group in selection.forward_notes:
            flat_notes.extend(group)
        assert flat_notes == notes

    @pytest.mark.asyncio
    async def test_selection_notes_cover_each_stage(self):
        adapter, event = await make_event({"input_media_limit": 1}, [
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
        adapter, event = await make_event({}, [
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
        adapter, event = await make_event({}, [
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
        adapter, event = await make_event({}, [
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
        adapter, event = await make_event({}, [
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
        adapter, event = await make_event({}, [
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
        adapter, event = await make_event({}, [
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
        adapter, event = await make_event({}, [
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
        adapter, event = await make_event({"input_media_limit": 1}, [
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
        adapter, event = await make_event({}, [
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
        adapter, event = await make_event({"input_media_limit": 4}, [
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
    async def test_pure_failed_image_is_flagged_for_deterministic_feedback(self):
        adapter, event = await make_event({"input_media_limit": 4}, [
            {"type": "image", "data": {"url": "https://cdn/a.png"}},
        ])
        event.set_extra("media_resolution", (
            MediaResult("https://cdn/a.png", "image", "failed", "image_unavailable"),
        ))

        projected = project_input(event, "none")

        assert projected.images == ()
        assert projected.message.strip() == "[用户 123, 消息 77] [图片读取失败]"
        assert projected.media_only_unavailable is True

    @pytest.mark.asyncio
    async def test_pure_failed_video_marker_and_flag(self):
        adapter, event = await make_event({"input_media_limit": 4}, [
            {"type": "video", "data": {"url": "https://cdn/v.mp4"}},
        ])
        event.set_extra("media_resolution", (
            MediaResult("https://cdn/v.mp4", "video", "failed", "video_download_failed"),
        ))

        projected = project_input(event, "none")

        assert projected.videos == ()
        assert projected.message.strip() == "[用户 123, 消息 77] [视频读取失败]"
        assert projected.media_only_unavailable is True

    @pytest.mark.asyncio
    async def test_quoted_media_failure_is_marked_in_context_block(self):
        adapter, event = await make_event({"input_media_limit": 4}, [
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

    @pytest.mark.asyncio
    async def test_forwarded_media_failure_is_marked_in_context_block(self):
        adapter, event = await make_event({"input_media_limit": 4}, [
            {"type": "forward", "data": {"id": "f1"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        adapter._bot.get_forward_msg.return_value = {"messages": [
            {"type": "node", "data": {"nickname": "小红", "user_id": 654, "content": [
                {"type": "image", "data": {"url": "https://cdn/f.png"}},
            ]}},
        ]}
        forward_status = await resolve_forwards(event)
        event.set_extra("media_resolution", (
            MediaResult("https://cdn/f.png", "image", "failed", "image_unavailable"),
        ))

        projected = project_input(event, "none", forward_status)

        assert "- 小红: [图片读取失败]" in projected.message
        assert projected.media_only_unavailable is False

    @pytest.mark.asyncio
    async def test_forwarded_media_placeholder_stays_chinese_when_resolved(self):
        adapter, event = await make_event({"input_media_limit": 4}, [
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
