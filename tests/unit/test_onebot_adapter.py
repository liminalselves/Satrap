from __future__ import annotations

from collections.abc import Sequence
import json

import pytest
from typing import Any, cast

from satrap.core.platform.onebot.onebot_utils import (
    create_platform_message,
    group_session_id,
    message_chain_to_onebot_segments,
    onebot_segments_to_components,
    private_session_id,
)
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.pipeline.wake_policy import evaluate_wake
from satrap.core.platform.event import MessageChain, MessageEvent
from satrap.core.components import At, AtAll, Face, File, Image, Json, Plain, Reply, Unknown
from satrap.core.platform import PlatformAdapterManager, PlatformConfig, registry
from satrap.core.type import PlatformMessage, PlatformMessageType


class FakeOneBotClient:
    def __init__(self):
        self.calls: list[Any] = []
        self.upload_failures: dict[str, BaseException] = {}

    async def send_private_msg(self, **kwargs: Any):
        self.calls.append(("send_private_msg", kwargs))
        return {"message_id": 1}

    async def send_group_msg(self, **kwargs: Any):
        self.calls.append(("send_group_msg", kwargs))
        return {"message_id": 2}

    async def upload_group_file(self, **kwargs: Any):
        self.calls.append(("upload_group_file", kwargs))
        error = self.upload_failures.get(str(kwargs.get("name", "")))
        if error is not None:
            raise error
        return {"file_id": f"F-{kwargs.get('name', '')}"}

    async def upload_private_file(self, **kwargs: Any):
        self.calls.append(("upload_private_file", kwargs))
        error = self.upload_failures.get(str(kwargs.get("name", "")))
        if error is not None:
            raise error
        return {"file_id": f"F-{kwargs.get('name', '')}"}


def make_adapter(settings: dict[str, Any] | None = None) -> OneBotAdapter:
    adapter = OneBotAdapter(
        PlatformConfig(
            id="onebot_main",
            type="onebot",
            settings=settings or {"host": "127.0.0.1", "port": 6700},
        )
    )
    adapter.bot_self_id = "10000"
    adapter.client_self_id = "10000"
    adapter._bot = FakeOneBotClient()
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("segments,rule", [
    ([{"type": "text", "data": {"text": "小助手你好"}}], "alias"),
    ([{"type": "at", "data": {"qq": "all"}}], "no_match"),
    ([{"type": "text", "data": {"text": "@10000"}}], "no_match"),
    ([{"type": "image", "data": {"file": "小助手.png"}}], "no_match"),
    ([{"type": "at", "data": {"qq": "10000"}}], "mention"),
])
async def test_explainable_wake_rules_only_match_top_level_content(segments, rule):
    """名字规则可选启用, 全体提及和单独附件保持静默"""
    adapter = make_adapter({"wake_aliases": ["小助手"]})
    await adapter._handle_group_message({"self_id": 10000, "group_id": 20, "user_id": 30,
                                         "message_type": "group", "message": segments})
    event = adapter._event_queue.get_nowait()
    decision = evaluate_wake(event)
    assert decision.rule == rule
    assert decision.triggered == (rule != "no_match")


@pytest.mark.asyncio
async def test_call_origin_is_frozen_before_message_processing():
    """预处理改写事件路由或消息元信息不能改写工具来源"""
    adapter = make_adapter()
    await adapter._handle_group_message({
        "self_id": 10000, "group_id": 20, "user_id": 30, "message_id": 40,
        "message_type": "group", "message": [{"type": "text", "data": {"text": "hello"}}],
    })
    event = adapter._event_queue.get_nowait()
    trusted = event.call_origin
    assert (trusted.adapter_id, trusted.self_id, trusted.chat_id, trusted.actor_id, trusted.source_message_id) == (
        "onebot_main", "10000", "20", "30", "40",
    )
    event.session_id = "another-session"
    event.platform_message.self_id = "other-bot"
    event.role = "admin"
    assert event.call_origin == trusted
    assert trusted.request_id
    with pytest.raises(AttributeError):
        event.call_origin = trusted


def test_onebot_and_aiocqhttp_aliases_are_registered():
    """onebot 和 aiocqhttp 都能创建同一个适配器类"""
    assert registry.get("onebot") is OneBotAdapter
    assert registry.get("aiocqhttp") is OneBotAdapter

    manager = PlatformAdapterManager(registry=registry)
    first = manager.add_adapter(PlatformConfig(id="ob1", type="onebot", settings={}))
    second = manager.add_adapter(PlatformConfig(id="ob2", type="aiocqhttp", settings={}))

    assert isinstance(first, OneBotAdapter)
    assert isinstance(second, OneBotAdapter)
    assert manager.list_adapters() == ["ob1", "ob2"]


def test_config_defaults_and_aliases():
    adapter = make_adapter({"listen_host": "0.0.0.0", "listen_port": 8081})

    assert adapter.host == "0.0.0.0"
    assert adapter.port == 8081
    assert adapter.enable_private is True
    assert adapter.enable_group is True


def test_committed_event_uses_configured_session_type():
    """OneBot 入站事件应使用平台实例绑定的会话类"""
    adapter = make_adapter()
    adapter.config.session_type = "assistant"
    message = PlatformMessage()
    message.type = PlatformMessageType.FRIEND_MESSAGE
    message.message_str = "hello"
    message.session_id = "private:10001"

    adapter._commit_platform_message(message)
    event = adapter._event_queue.get_nowait()

    assert event.session_type == "assistant"


def test_onebot_segments_to_components():
    components, text = onebot_segments_to_components(
        [
            {"type": "text", "data": {"text": "hello"}},
            {"type": "at", "data": {"qq": "all"}},
            {"type": "at", "data": {"qq": "123"}},
            {"type": "face", "data": {"id": "14"}},
            {"type": "image", "data": {"file": "a.png", "url": "https://cdn/a.png"}},
            {"type": "reply", "data": {"id": "msg-1"}},
            {"type": "json", "data": {"data": '{"ok": true}'}},
            {"type": "custom", "data": {"x": 1}},
        ]
    )

    assert isinstance(components[0], Plain)
    assert isinstance(components[1], AtAll)
    assert isinstance(components[2], At)
    assert isinstance(components[3], Face)
    assert isinstance(components[4], Image)
    assert isinstance(components[5], Reply)
    assert isinstance(components[6], Json)
    assert isinstance(components[7], Unknown)
    assert "hello" in text
    assert "@全体成员" in text
    assert "[图片]" in text


@pytest.mark.asyncio
async def test_message_chain_to_onebot_segments():
    segments = await message_chain_to_onebot_segments(
        [
            Plain("hi"),
            At(qq="123"),
            Image(file="https://cdn/a.png", url="https://cdn/a.png"),
            Reply(id="msg-1"),
            Json({"ok": True}),
        ]
    )

    assert segments[0] == {"type": "text", "data": {"text": "hi"}}
    assert segments[1] == {"type": "at", "data": {"qq": "123"}}
    assert segments[2] == {"type": "image", "data": {"file": "https://cdn/a.png"}}
    assert segments[3] == {"type": "reply", "data": {"id": "msg-1"}}
    assert segments[4] == {"type": "json", "data": {"data": '{"ok": true}'}}


def test_create_platform_message_private_and_group():
    private = create_platform_message(
        {
            "self_id": 10000,
            "message_id": 10,
            "message_type": "private",
            "user_id": 123,
            "sender": {"nickname": "Alice"},
            "message": [{"type": "text", "data": {"text": "hello"}}],
            "time": 1,
        },
        "10000",
    )
    group = create_platform_message(
        {
            "self_id": 10000,
            "message_id": 11,
            "message_type": "group",
            "group_id": 456,
            "user_id": 123,
            "sender": {"card": "AliceCard"},
            "message": [{"type": "text", "data": {"text": "group hi"}}],
            "time": 2,
        },
        "10000",
    )

    assert private.type == PlatformMessageType.FRIEND_MESSAGE
    assert private.session_id == private_session_id(123)
    assert private.sender.nickname == "Alice"
    assert private.message_str == "hello"
    assert group.type == PlatformMessageType.GROUP_MESSAGE
    assert group.session_id == group_session_id(456)
    assert group.group is not None
    assert group.group.group_id == "456"
    assert group.sender.nickname == "AliceCard"


@pytest.mark.asyncio
async def test_convert_message_commits_event():
    adapter = make_adapter()

    await adapter._handle_message_event(
        {
            "self_id": 10000,
            "message_id": 10,
            "message_type": "private",
            "user_id": 123,
            "sender": {"nickname": "Alice"},
            "message": [{"type": "text", "data": {"text": "hello"}}],
        }
    )

    event = cast(MessageEvent, adapter._event_queue.get_nowait())
    assert event.platform_meta.id == "onebot_main"
    assert event.session_id == "private%123"
    assert event.session_type == "onebot"
    assert event.platform_message.type == PlatformMessageType.FRIEND_MESSAGE


@pytest.mark.asyncio
async def test_send_message_routes_private_and_group():
    adapter = make_adapter()

    await adapter.send_message("private%123", MessageChain([Plain("dm")]))
    await adapter.send_message("group%456", MessageChain([Plain("group")]))

    client = adapter._bot
    assert client.calls[0] == (
        "send_private_msg",
        {"user_id": 123, "message": [{"type": "text", "data": {"text": "dm"}}]},
    )
    assert client.calls[1] == (
        "send_group_msg",
        {"group_id": 456, "message": [{"type": "text", "data": {"text": "group"}}]},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("segments, expected", [
    ([{"type": "at", "data": {"qq": "10000"}}], 1),
    ([{"type": "at", "data": {"qq": "99999"}}], 0),
    ([{"type": "at", "data": {"qq": "all"}}], 0),
    ([{"type": "text", "data": {"text": "小助手 帮忙"}}], 1),
    ([{"type": "text", "data": {"text": "@10000"}}], 0),
    ([{"type": "image", "data": {"url": "https://example.com/a.png"}}], 0),
    ([{"type": "reply", "data": {"id": "小助手"}}], 0),
])
async def test_raw_group_wake_reaches_session(segments, expected):
    from unittest.mock import AsyncMock
    from satrap.core.pipeline.scheduler import PipelineScheduler

    adapter = make_adapter({"wake_words": ["小助手"]})
    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    await adapter._handle_group_message({
        "self_id": 10000, "user_id": 123, "group_id": 456,
        "message_id": 789, "message_type": "group", "message": segments,
    })
    event = adapter._event_queue.get_nowait()
    await PipelineScheduler(manager).execute(event)
    assert manager.handle_call_async.await_count == expected


@pytest.mark.asyncio
async def test_unwoken_chatter_preserves_execution_budget():
    from unittest.mock import AsyncMock
    from satrap.core.pipeline.scheduler import PipelineScheduler
    from satrap.core.pipeline.rate_limiter import RateLimiter

    adapter = make_adapter()
    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    scheduler = PipelineScheduler(manager, RateLimiter(rate=0.01, burst=1))
    for message_id, text in enumerate(["普通聊天", "普通聊天", None], start=789):
        segments = [{"type": "text", "data": {"text": text}}] if text else [{"type": "at", "data": {"qq": "10000"}}]
        await adapter._handle_group_message({
            "self_id": 10000, "user_id": 123, "group_id": 456,
            "message_id": message_id, "message_type": "group", "message": segments,
        })
        await scheduler.execute(adapter._event_queue.get_nowait())
    assert manager.handle_call_async.await_count == 1
    assert adapter._bot.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("groups, enabled, accepted", [([], True, True), ([456], True, True), ([789], True, False), ([], False, False)])
async def test_group_scope_guards_receive_and_send(groups, enabled, accepted):
    adapter = make_adapter({"group_whitelist": groups, "enable_group": enabled})
    await adapter._handle_group_message({
        "self_id": 10000, "user_id": 123, "group_id": 456,
        "message_type": "group", "message": [{"type": "text", "data": {"text": "hi"}}],
    })
    assert (not adapter._event_queue.empty()) == accepted
    if accepted:
        await adapter.send_text(group_session_id("456"), "hi")
    else:
        receipt = await adapter.send_text(group_session_id("456"), "hi")
        assert receipt.status == "failed" and receipt.reason == "target_unavailable"
    await adapter.send_text(private_session_id("123"), "hi")
    assert adapter._bot.calls[-1][0] == "send_private_msg"


@pytest.mark.parametrize("value", [None, "123", [True], [0], [-1], ["all"], ["1.2"]])
def test_group_scope_rejects_invalid_config(value):
    from satrap.core.config.document import validate_platforms

    with pytest.raises(ValueError):
        make_adapter({"group_whitelist": value})
    with pytest.raises(ValueError):
        validate_platforms([{"id": "test", "type": "onebot", "settings": {"group_whitelist": value}}])


@pytest.mark.asyncio
async def test_queued_group_cannot_bypass_tightened_scope():
    from unittest.mock import AsyncMock
    from satrap.core.pipeline.scheduler import PipelineScheduler

    adapter = make_adapter()
    await adapter._handle_group_message({
        "self_id": 10000, "user_id": 123, "group_id": 456,
        "message_type": "group", "message": [{"type": "at", "data": {"qq": "10000"}}],
    })
    event = adapter._event_queue.get_nowait()
    event.is_wake = True
    adapter.config.settings["group_whitelist"] = ["789"]
    manager = AsyncMock()
    scheduler = PipelineScheduler(manager)
    processor = AsyncMock(return_value=True)
    scheduler.add_preprocessor(processor)
    await scheduler.execute(event)
    manager.handle_call_async.assert_not_awaited()
    processor.assert_not_awaited()
    assert adapter._bot.calls == []


def test_json_segment_with_non_object_payload_degrades_to_unknown():
    """JSON 段内容不是对象时降级为 Unknown, 不丢整条消息"""
    components, text = onebot_segments_to_components(
        [
            {"type": "text", "data": {"text": "前文"}},
            {"type": "json", "data": {"data": "[1, 2, 3]"}},
            {"type": "text", "data": {"text": "后文"}},
        ]
    )
    assert isinstance(components[0], Plain)
    assert isinstance(components[1], Unknown)
    assert isinstance(components[2], Plain)
    assert "前文" in text and "[JSON]" in text and "后文" in text


def test_create_platform_message_tolerates_non_numeric_time():
    """time 字段非数字时保留默认时间戳, 消息不丢"""
    message = create_platform_message(
        {"self_id": 1, "message_id": 7, "time": "not-a-number", "message_type": "private",
         "user_id": 2, "message": [{"type": "text", "data": {"text": "hi"}}]}, "1")
    assert message.message_str == "hi" and isinstance(message.timestamp, int)


def test_normalize_file_source_tolerates_embedded_nul():
    """含 NUL 的非法路径按原样透传, 不在路径检查处抛出"""
    from satrap.core.platform.onebot.onebot_utils import _normalize_file_source
    assert _normalize_file_source("bad\0path") == "bad\0path"


def test_normalize_file_source_rejects_paths_outside_media_roots(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
):
    """白名单外的本地路径不再包装为 file:// 上传, 直接拒绝"""
    from satrap.core.platform.onebot.onebot_utils import _normalize_file_source
    from satrap.core.utils import paths as paths_mod

    fake_root = tmp_path / "allowed"
    fake_root.mkdir()
    monkeypatch.setattr(paths_mod, "get_allowed_media_roots", lambda: [fake_root.resolve()])
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(PermissionError):
        _normalize_file_source(str(outside))


class TestMediaSourceRejection:
    """媒体白名单拒绝在发送链上保留专属原因码, 不与目标范围拒绝混淆"""

    @pytest.mark.asyncio
    async def test_image_segment_denied_reports_media_reason(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ):
        from satrap.core.utils import paths as paths_mod

        fake_root = tmp_path / "allowed"
        fake_root.mkdir()
        monkeypatch.setattr(paths_mod, "get_allowed_media_roots", lambda: [fake_root.resolve()])
        outside = tmp_path / "secret.png"
        outside.write_bytes(b"png")

        adapter = make_adapter()
        receipt = await adapter.send_message("group%456", MessageChain([Image(file=str(outside))]))
        assert receipt.status == "failed" and receipt.reason == "media_source_denied"
        assert adapter._bot.calls == []

    @pytest.mark.asyncio
    async def test_file_segment_denied_reports_media_reason(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ):
        from satrap.core.utils import paths as paths_mod

        fake_root = tmp_path / "allowed"
        fake_root.mkdir()
        monkeypatch.setattr(paths_mod, "get_allowed_media_roots", lambda: [fake_root.resolve()])
        outside = tmp_path / "secret.bin"
        outside.write_bytes(b"bin")

        adapter = make_adapter()
        receipt = await adapter.send_message("group%456", MessageChain([File(name="secret.bin", file=str(outside))]))
        assert receipt.status == "failed" and receipt.reason == "media_source_denied"
        assert [name for name, _ in adapter._bot.calls] == []


class TestFileOutboundSplit:
    """File 组件按实现能力分流上传, 混合链保持原序, 未验证回落显式标注"""

    @pytest.mark.asyncio
    async def test_mixed_chain_uploads_in_order(self, tmp_path: Any):
        import os

        target = tmp_path / "probe.bin"
        target.write_bytes(b"x")
        adapter = make_adapter()
        receipt = await adapter.send_message("group%456", MessageChain([
            Plain("段一"), File(name="probe.bin", file=str(target)), Plain("段二"),
        ]))
        assert receipt.status == "success"
        assert receipt.message_ids == ("2", "F-probe.bin", "2")
        calls = adapter._bot.calls
        assert [name for name, _ in calls] == ["send_group_msg", "upload_group_file", "send_group_msg"]
        upload = calls[1][1]
        assert upload["group_id"] == 456 and upload["name"] == "probe.bin"
        assert upload["file"] == os.path.abspath(str(target))

    @pytest.mark.asyncio
    async def test_private_file_uses_private_upload(self):
        adapter = make_adapter()
        receipt = await adapter.send_message("private%123", MessageChain([File(name="p.bin", url="https://example.invalid/p.bin")]))
        assert receipt.status == "success" and receipt.message_ids == ("F-p.bin",)
        name, kwargs = adapter._bot.calls[0]
        assert name == "upload_private_file"
        assert kwargs["user_id"] == 123 and kwargs["file"] == "https://example.invalid/p.bin"

    @pytest.mark.asyncio
    async def test_missing_upload_falls_back_once_per_generation(self):
        from aiocqhttp.exceptions import ActionFailed

        adapter = make_adapter()
        adapter._running = True
        adapter._bot.upload_failures["gone.bin"] = ActionFailed({"retcode": 10002})
        receipt = await adapter.send_message("group%456", MessageChain([File(name="gone.bin", url="https://example.invalid/gone.bin")]))
        # 反例: 兼容回落即使拿到 message_id 也不是文件送达的证据
        assert receipt.status == "unknown"
        assert receipt.reason.startswith("file_delivery_unconfirmed")
        assert receipt.message_ids == ("2",)
        assert [name for name, _ in adapter._bot.calls] == ["upload_group_file", "send_group_msg"]
        assert adapter.admin_capabilities()["upload_file"] == "unsupported"
        # 同连接代次内不重复试错: 后续文件直接走兼容路径
        adapter._bot.calls.clear()
        again = await adapter.send_message("group%456", MessageChain([File(name="other.bin", url="https://example.invalid/o.bin")]))
        assert again.status == "unknown" and again.reason.startswith("file_delivery_unconfirmed")
        assert [name for name, _ in adapter._bot.calls] == ["send_group_msg"]

    @pytest.mark.asyncio
    async def test_file_fallback_keeps_confirmed_prefix_and_unsent_suffix(self, tmp_path: Any):
        from aiocqhttp.exceptions import ActionFailed

        adapter = make_adapter()
        store = _make_store(tmp_path)
        adapter.set_send_attempt_recorder(store)
        adapter._bot.upload_failures["mid.bin"] = ActionFailed({"retcode": 1404})
        receipt = await adapter.send_message("group%456", MessageChain([
            Plain("前文"), File(name="mid.bin", url="https://example.invalid/mid.bin"), Plain("后文"),
        ]))
        # 反例: 首段确认保留, 文件段未确认, 末段不发送, 总结果不显示已送达
        assert receipt.status == "unknown" and receipt.reason.startswith("file_delivery_unconfirmed")
        assert receipt.message_ids == ("2", "2")
        assert [name for name, _ in adapter._bot.calls] == ["send_group_msg", "upload_group_file", "send_group_msg"]
        # 持久化与查询也不显示已送达: 文件段未确认, 末段未尝试, 尝试整体是未知
        segments = _segment_states(tmp_path)
        assert segments == ["sent", "unknown", "skipped"]
        assert store.lookup_attempts("", "onebot_main")[0]["status"] == "unknown"
        assert store.request_send_outcome("", "onebot_main")["confirmed"] == 1

    @pytest.mark.asyncio
    async def test_upload_rejection_stops_chain_with_partial(self):
        from aiocqhttp.exceptions import ActionFailed

        adapter = make_adapter()
        adapter._bot.upload_failures["no.bin"] = ActionFailed({"retcode": 1200})
        receipt = await adapter.send_message("group%456", MessageChain([
            Plain("前文"), File(name="no.bin", url="https://example.invalid/no.bin"), Plain("后文"),
        ]))
        assert receipt.status == "partial"
        assert receipt.message_ids == ("2",)
        assert [name for name, _ in adapter._bot.calls] == ["send_group_msg", "upload_group_file"]
        # 上传拒绝不是接口缺失, 不写入能力缓存
        assert "upload_group_file" not in adapter._capability_states

    @pytest.mark.asyncio
    async def test_upload_transport_error_is_unknown(self):
        adapter = make_adapter()
        adapter._bot.upload_failures["x.bin"] = RuntimeError("boom")
        receipt = await adapter.send_message("group%456", MessageChain([File(name="x.bin", url="https://example.invalid/x.bin")]))
        assert receipt.status == "unknown" and receipt.reason == "action_unconfirmed"

    @pytest.mark.asyncio
    async def test_file_without_source_fails(self):
        adapter = make_adapter()
        receipt = await adapter.send_message("group%456", MessageChain([File(name="empty.bin")]))
        assert receipt.status == "failed" and receipt.reason == "empty_file"
        assert adapter._bot.calls == []


def _make_store(tmp_path: Any) -> Any:
    """落盘到指定目录的发送尝试存储"""
    from satrap.core.pipeline.manual_wake_store import ManualWakeStore

    return ManualWakeStore(tmp_path / "manual_wake_store.json")


def _segment_states(tmp_path: Any) -> list[str]:
    """磁盘上单个尝试的段状态, 用于观察 I/O 时刻的真实落盘内容"""
    payload = json.loads((tmp_path / "manual_wake_store.json").read_text(encoding="utf-8"))
    attempt = next(iter(cast(dict[str, Any], payload["attempts"]).values()))
    return [segment.get("status") for segment in attempt["segments"]]


class TestSegmentProgress:
    """B3 反例: 逐段落盘发送证据, 未尝试段不得显示为已提交"""

    @pytest.mark.asyncio
    async def test_segments_advance_one_at_a_time(self, tmp_path: Any):
        from unittest.mock import AsyncMock

        adapter = make_adapter({"message_text_limit": 64})
        store = _make_store(tmp_path)
        adapter.set_send_attempt_recorder(store)
        observed: list[list[str]] = []

        async def observe_send(**kwargs: Any) -> dict[str, Any]:
            observed.append(_segment_states(tmp_path))
            return {"message_id": len(observed)}

        adapter._bot.send_group_msg = AsyncMock(side_effect=observe_send)
        receipt = await adapter.send_message("group%456", MessageChain.from_text("字" * 130))
        assert receipt.status == "success"
        # 每次 I/O 之前只有正在发送的段是 submitted, 后续段仍是 planned
        assert observed[0] == ["submitted", "planned", "planned"]
        assert observed[1] == ["sent", "submitted", "planned"]
        assert observed[2] == ["sent", "sent", "submitted"]

    @pytest.mark.asyncio
    async def test_untracked_io_is_not_reported_as_sent(self, tmp_path: Any):
        from unittest.mock import AsyncMock

        adapter = make_adapter()
        store = _make_store(tmp_path)
        adapter.set_send_attempt_recorder(store)
        adapter._bot = AsyncMock()
        adapter._bot.send_group_msg = AsyncMock(return_value={"message_id": 7})

        def broken(*args: Any, **kwargs: Any) -> bool:
            return False

        # 段状态写入全部失败: 已经发出的段没有任何落盘证据
        store.mark_segment_submitted = broken    # type: ignore[method-assign]
        store.record_segment_result = broken     # type: ignore[method-assign]
        again = await adapter.send_message("group%456", MessageChain.from_text("第二条"))
        assert again.status == "success"
        attempts = store.lookup_attempts("", "onebot_main")
        latest = attempts[0]
        # 已经发出但没有任何落盘证据的段不能被记为已送达
        assert latest["status"] == "unknown" and latest["detail"] == "tracking_incomplete"
        assert [segment["status"] for segment in latest["segments"]] == ["unknown"]

    @pytest.mark.asyncio
    async def test_finalize_timeout_keeps_record_unconfirmed(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
    ):
        """反例: 收尾超时不显示已送达也不重发, 后续可信确认才允许精化"""
        import threading
        import time as time_module
        from unittest.mock import AsyncMock

        from satrap.core.platform.onebot import adapter as adapter_module

        adapter = make_adapter()
        store = _make_store(tmp_path)
        adapter.set_send_attempt_recorder(store)
        adapter._bot = AsyncMock()
        adapter._bot.send_group_msg = AsyncMock(return_value={"message_id": 5})
        release = threading.Event()
        original = store.complete_send_attempt

        def slow(*args: Any, **kwargs: Any) -> bool:
            release.wait(2.0)
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "complete_send_attempt", slow)
        monkeypatch.setattr(adapter_module, "_ATTEMPT_FINALIZE_TIMEOUT", 0.05)
        receipt = await adapter.send_message("group%456", MessageChain.from_text("收尾超时"))
        # 传输层确认照常返回, 但收尾未落盘: 尝试仍是未终结状态, 已确认的段证据保留
        assert receipt.status == "success"
        attempt = store.lookup_attempts("", "onebot_main")[0]
        assert attempt["status"] == "submitted" and attempt["segments"][0]["status"] == "sent"
        # 结论只认段证据: 该段已确认, 不因收尾缺失被压成 unknown 或 failed
        assert store.request_send_outcome("", "onebot_main")["confirmed"] == 1
        # 收尾线程完成后的可信确认才落终态, 期间不产生第二次发送
        release.set()
        for _ in range(50):
            if store.lookup_attempts("", "onebot_main")[0]["status"] == "sent":
                break
            time_module.sleep(0.02)
        assert store.lookup_attempts("", "onebot_main")[0]["status"] == "sent"
        adapter._bot.send_group_msg.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_finalize_result_is_consumed_and_degrade_skips_write(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ):
        """E3 反例: 收尾未落盘必须可见; 降级期不再发起写入也不重复告警"""
        import logging

        adapter = make_adapter()
        store = _make_store(tmp_path)
        adapter.set_send_attempt_recorder(store)

        def _refuse_finalize(turn_id: str, status: str, detail: str = "", untracked: Sequence[int] = ()) -> bool:
            return False

        calls: list[str] = []

        def _record_finalize(turn_id: str, status: str, detail: str = "", untracked: Sequence[int] = ()) -> bool:
            calls.append(turn_id)
            return True

        monkeypatch.setattr(store, "complete_send_attempt", _refuse_finalize)
        with caplog.at_level(logging.WARNING):
            await adapter._finalize_attempt("turn-false", "sent", "ok")
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("发送收尾未落盘 turn=turn-false status=sent" in m for m in warnings)

        caplog.clear()
        monkeypatch.setattr(store, "degraded", True)
        monkeypatch.setattr(store, "complete_send_attempt", _record_finalize)
        with caplog.at_level(logging.WARNING):
            await adapter._finalize_attempt("turn-degraded", "sent", "ok")
        # 降级期存储拒绝一切写入: 不再发起调用, 也不需要逐次告警
        assert calls == []
        assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == []

    @pytest.mark.asyncio
    async def test_finalize_failure_logs_exactly_one_warning(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ):
        """R4 反例: 收尾异常只按异常分支告警一次, 不再叠加"未落盘"; 正常返回 False 才记未落盘"""
        import logging

        adapter = make_adapter()
        store = _make_store(tmp_path)
        adapter.set_send_attempt_recorder(store)

        def _explode(turn_id: str, status: str, detail: str = "", untracked: Sequence[int] = ()) -> bool:
            raise OSError("写入失败")

        monkeypatch.setattr(store, "complete_send_attempt", _explode)
        with caplog.at_level(logging.WARNING):
            await adapter._finalize_attempt("turn-error", "sent", "ok")
        # 只统计一次调用语句 (打印与文件两个 logger 输出同一条消息)
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and not r.name.endswith("_file")]
        assert len(warnings) == 1
        assert "发送收尾失败 turn=turn-error" in warnings[0] and "OSError" in warnings[0]

        caplog.clear()

        def _refuse(turn_id: str, status: str, detail: str = "", untracked: Sequence[int] = ()) -> bool:
            return False

        monkeypatch.setattr(store, "complete_send_attempt", _refuse)
        with caplog.at_level(logging.WARNING):
            await adapter._finalize_attempt("turn-false-only", "sent", "ok")
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and not r.name.endswith("_file")]
        assert len(warnings) == 1
        assert "发送收尾未落盘 turn=turn-false-only status=sent" in warnings[0]
