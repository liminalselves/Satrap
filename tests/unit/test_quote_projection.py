"""引用回源与模型输入投影"""
from collections.abc import Awaitable
from unittest.mock import AsyncMock
from typing import Any
import asyncio

import pytest

from satrap.core.pipeline.input_projection import QUOTE_TEXT_LIMIT, project_input, resolve_quotes
from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.components import Reply
from satrap.core.platform import PlatformConfig


def quoted(message_id: int = 5, group_id: int = 456, sender: int = 321, text: str = "原文", **extra: object) -> dict[str, object]:
    return {"message_id": message_id, "group_id": group_id, "message_type": "group", "self_id": 10000,
            "sender": {"user_id": sender, "nickname": "小明"}, "time": 1700000000,
            "message": [{"type": "text", "data": {"text": text}}], **extra}


async def make_event(settings: dict[str, object] | None = None, segments: list[dict[str, object]] | None = None,
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
    adapter, event = await make_event()
    adapter._bot.get_msg.return_value = quoted()
    status = await resolve_quotes(event)
    assert status == "resolved"
    reply = event.get_messages()[0]
    assert isinstance(reply, Reply) and reply.message_str == "原文" and reply.sender_nickname == "小明" and reply.sender_id == "321"
    projected = project_input(event, status)
    assert projected.message == "[引用 小明 的消息: 原文]\n@10000 这个对吗"
    adapter._bot.get_msg.assert_awaited_once_with(message_id=5)


@pytest.mark.asyncio
async def test_scheduler_passes_quote_to_session_and_quote_alone_reaches_model():
    adapter, event = await make_event(segments=[{"type": "reply", "data": {"id": "5"}}, {"type": "at", "data": {"qq": "10000"}}])
    adapter._bot.get_msg.return_value = quoted(text="帮我看这个")
    manager = AsyncMock()
    manager.handle_call_async.return_value = "好的"
    await PipelineScheduler(manager).execute(event)
    call = manager.handle_call_async.call_args.args[0]
    assert call.message.startswith("[引用 小明 的消息: 帮我看这个]")
    assert event.get_extra("input_projection").quote_status == "resolved"


@pytest.mark.asyncio
async def test_quote_lookup_happens_after_wake_and_not_for_unwoken_messages():
    adapter, event = await make_event(segments=[{"type": "reply", "data": {"id": "5"}}, {"type": "text", "data": {"text": "没叫机器人"}}])
    manager = AsyncMock()
    await PipelineScheduler(manager).execute(event)
    adapter._bot.get_msg.assert_not_awaited()
    manager.handle_call_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_quote_keeps_current_message_and_marks_status():
    adapter, event = await make_event()
    adapter._bot.get_msg.side_effect = RuntimeError("boom")
    status = await resolve_quotes(event)
    projected = project_input(event, status)
    assert status == "unavailable"
    assert projected.message == "[引用了一条无法获取原文的消息]\n@10000 这个对吗"
    assert "quote_unavailable" in projected.notes


@pytest.mark.asyncio
async def test_cross_group_and_foreign_account_quotes_are_rejected():
    adapter, event = await make_event()
    adapter._bot.get_msg.return_value = quoted(group_id=999)
    assert await resolve_quotes(event) == "unavailable"
    adapter._bot.get_msg.return_value = quoted(self_id=20000)
    assert await resolve_quotes(event) == "unavailable"
    adapter._bot.get_msg.return_value = "not a dict"
    assert await resolve_quotes(event) == "unavailable"


@pytest.mark.asyncio
async def test_private_quote_must_belong_to_the_same_dialogue():
    adapter, event = await make_event(private=True)
    adapter._bot.get_msg.return_value = {**quoted(sender=123), "group_id": "", "message_type": "private"}
    assert await resolve_quotes(event) == "resolved"
    adapter, event = await make_event(private=True)
    adapter._bot.get_msg.return_value = {**quoted(sender=555), "group_id": "", "message_type": "private"}
    assert await resolve_quotes(event) == "unavailable"


@pytest.mark.asyncio
async def test_whitelist_tightening_blocks_lookup_and_lookup_disabled_by_policy():
    adapter, event = await make_event()
    adapter.config.settings["group_whitelist"] = ["789"]
    adapter._bot.get_msg.return_value = quoted()
    assert await resolve_quotes(event) == "unavailable"
    adapter._bot.get_msg.assert_not_awaited()
    adapter, event = await make_event(settings={"quote_lookup": False})
    assert await resolve_quotes(event) == "disabled"
    adapter._bot.get_msg.assert_not_awaited()
    assert "quote_disabled" in project_input(event, "disabled").notes


@pytest.mark.asyncio
async def test_lookup_timeout_and_oversized_payload_do_not_block():
    adapter, event = await make_event()

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
    adapter, event = await make_event()
    adapter._bot.get_msg.return_value = quoted(text="x" * 70000)
    assert await resolve_quotes(event) == "unavailable"


@pytest.mark.asyncio
async def test_quoted_media_is_merged_with_budget_and_text_truncated():
    adapter, event = await make_event()
    media: list[dict[str, object]] = [{"type": "image", "data": {"url": f"http://x/{i}.png"}} for i in range(6)]
    adapter._bot.get_msg.return_value = {**quoted(text="y" * (QUOTE_TEXT_LIMIT + 10)), "message": media + [{"type": "text", "data": {"text": "y" * (QUOTE_TEXT_LIMIT + 10)}}]}
    status = await resolve_quotes(event)
    projected = project_input(event, status)
    assert len(projected.images) == 4
    assert "quote_media_truncated" in projected.notes and "quote_truncated" in projected.notes
    header = projected.message.split(chr(10))[0]
    assert header.endswith("…]") and len(header) < QUOTE_TEXT_LIMIT + 60


@pytest.mark.asyncio
async def test_nested_reply_is_not_recursively_fetched_and_only_first_reply_resolved():
    adapter, event = await make_event(segments=[{"type": "reply", "data": {"id": "5"}}, {"type": "reply", "data": {"id": "6"}}, {"type": "at", "data": {"qq": "10000"}}])
    adapter._bot.get_msg.return_value = {**quoted(), "message": [{"type": "reply", "data": {"id": "4"}}, {"type": "text", "data": {"text": "二级"}}]}
    assert await resolve_quotes(event) == "resolved"
    assert adapter._bot.get_msg.await_count == 1
    assert await resolve_quotes(event) == "resolved"
    assert adapter._bot.get_msg.await_count == 1


@pytest.mark.asyncio
async def test_quote_of_bot_message_is_labelled_and_not_treated_as_wake():
    adapter, event = await make_event(segments=[{"type": "reply", "data": {"id": "5"}}, {"type": "text", "data": {"text": "继续"}}])
    adapter._bot.get_msg.return_value = quoted(sender=10000)
    manager = AsyncMock()
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
    adapter, event = await make_event(settings={"wake_on_quote_self": True}, segments=segments)
    adapter._bot.get_msg.return_value = quoted(sender=10000)
    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    await PipelineScheduler(manager).execute(event)
    assert manager.handle_call_async.await_count == 1
    assert adapter._bot.get_msg.await_count == 1
    assert event.get_extra("wake_decision").rule == "quote_self"
    assert manager.handle_call_async.call_args.args[0].message.startswith("[引用 机器人自己 的消息: 原文]")


@pytest.mark.asyncio
async def test_quote_of_other_user_or_failed_lookup_does_not_wake():
    segments: list[dict[str, object]] = [{"type": "reply", "data": {"id": "5"}}, {"type": "text", "data": {"text": "继续说"}}]
    adapter, event = await make_event(settings={"wake_on_quote_self": True}, segments=segments)
    adapter._bot.get_msg.return_value = quoted(sender=321)
    manager = AsyncMock()
    await PipelineScheduler(manager).execute(event)
    manager.handle_call_async.assert_not_awaited()
    adapter, event = await make_event(settings={"wake_on_quote_self": True}, segments=segments)
    adapter._bot.get_msg.side_effect = RuntimeError("down")
    await PipelineScheduler(manager).execute(event)
    manager.handle_call_async.assert_not_awaited()
    assert event.get_extra("wake_decision").rule == "no_match"


@pytest.mark.asyncio
async def test_quote_self_wake_respects_whitelist_and_does_not_lookup_when_disabled():
    segments: list[dict[str, object]] = [{"type": "reply", "data": {"id": "5"}}, {"type": "text", "data": {"text": "继续说"}}]
    adapter, event = await make_event(settings={"wake_on_quote_self": True}, segments=segments)
    adapter.config.settings["group_whitelist"] = ["789"]
    adapter._bot.get_msg.return_value = quoted(sender=10000)
    manager = AsyncMock()
    await PipelineScheduler(manager).execute(event)
    adapter._bot.get_msg.assert_not_awaited()
    manager.handle_call_async.assert_not_awaited()
    # 白名单收紧后, 已排队事件在 Step.1 来源检查被拒绝, 不消耗回源预算


@pytest.mark.asyncio
async def test_quote_with_non_numeric_time_is_still_resolved(monkeypatch: pytest.MonkeyPatch):
    """回源结果的 time 非数字时引用仍解析, time 回退为 0"""
    adapter, event = await make_event()

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
