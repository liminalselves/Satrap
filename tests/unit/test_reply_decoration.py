from unittest.mock import AsyncMock

import pytest

from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.config.wake_overrides import validate_wake_overrides
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.components import Plain, Reply, At, Image
from satrap.core.platform.event import MessageChain, MessageEvent
from satrap.core.platform import PlatformConfig


async def _event(private: bool = False, **settings: object) -> tuple[OneBotAdapter, MessageEvent]:
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings)))
    adapter._bot = AsyncMock()
    adapter._bot.send_group_msg.return_value = {"message_id": 1}
    adapter._bot.send_private_msg.return_value = {"message_id": 1}
    if private:
        await adapter._handle_private_message({
            "self_id": 10000, "user_id": 123, "message_id": 77, "message_type": "private",
            "message": [{"type": "text", "data": {"text": "你好"}}], "sender": {"user_id": 123},
        })
    else:
        await adapter._handle_group_message({
            "self_id": 10000, "user_id": 123, "group_id": 456, "message_id": 77,
            "message_type": "group", "message": [{"type": "text", "data": {"text": "你好"}}],
        })
    return adapter, adapter._event_queue.get_nowait()


def _sent_segments(adapter: OneBotAdapter, private: bool = False):
    mock = adapter._bot.send_private_msg if private else adapter._bot.send_group_msg
    return [call.kwargs["message"] for call in mock.await_args_list]


@pytest.mark.asyncio
async def test_default_policy_keeps_plain_reply():
    adapter, event = await _event()
    await event.send(MessageChain.from_text("回复"))
    assert _sent_segments(adapter) == [[{"type": "text", "data": {"text": "回复"}}]]


@pytest.mark.asyncio
async def test_quote_and_mention_are_prepended_with_space_after_at():
    adapter, event = await _event(reply_with_quote=True, reply_with_mention=True)
    await event.send(MessageChain.from_text("回复"))
    assert _sent_segments(adapter) == [[
        {"type": "reply", "data": {"id": "77"}},
        {"type": "at", "data": {"qq": "123"}},
        {"type": "text", "data": {"text": " 回复"}},
    ]]


@pytest.mark.asyncio
async def test_existing_reply_and_at_are_not_duplicated():
    adapter, event = await _event(reply_with_quote=True, reply_with_mention=True)
    await event.send(MessageChain([Reply(id="5"), At(qq="123"), Plain("已有")]))
    segments = _sent_segments(adapter)[0]
    assert [s["type"] for s in segments] == ["reply", "at", "text"]
    assert segments[0]["data"]["id"] == "5" and segments[2]["data"]["text"] == "已有"


@pytest.mark.asyncio
async def test_at_other_user_does_not_suppress_sender_mention():
    adapter, event = await _event(reply_with_mention=True)
    await event.send(MessageChain([At(qq="999"), Plain("转告")]))
    segments = _sent_segments(adapter)[0]
    assert [s["data"].get("qq") for s in segments[:2]] == ["123", "999"]


@pytest.mark.asyncio
async def test_private_chat_never_decorated():
    adapter, event = await _event(private=True, reply_with_quote=True, reply_with_mention=True)
    await event.send(MessageChain.from_text("回复"))
    assert _sent_segments(adapter, private=True) == [[{"type": "text", "data": {"text": "回复"}}]]


@pytest.mark.asyncio
async def test_missing_source_message_id_skips_quote_but_keeps_mention():
    adapter, event = await _event(reply_with_quote=True, reply_with_mention=True)
    event.platform_message.message_id = ""
    event._call_origin = event._call_origin.__class__(**{**event._call_origin.__dict__, "source_message_id": ""})
    await event.send(MessageChain.from_text("回复"))
    assert [s["type"] for s in _sent_segments(adapter)[0]] == ["at", "text"]


@pytest.mark.asyncio
async def test_non_text_first_component_gets_no_space_and_original_chain_untouched():
    adapter, event = await _event(reply_with_mention=True)
    image = Image(file="http://x/1.png")
    chain = MessageChain([image, Plain("说明")])
    await event.send(chain)
    assert chain.components == [image, chain.components[1]] and len(chain.components) == 2
    segments = _sent_segments(adapter)[0]
    assert [s["type"] for s in segments] == ["at", "image", "text"]
    assert segments[2]["data"]["text"] == "说明"


@pytest.mark.asyncio
async def test_stream_fallback_decorates_only_first_non_empty_chunk():
    adapter, event = await _event(reply_with_quote=True, reply_with_mention=True)

    async def gen():
        yield MessageChain([])
        yield MessageChain.from_text("第一块")
        yield MessageChain.from_text("第二块")

    await event.send_streaming(gen(), use_fallback=True)
    sent = _sent_segments(adapter)
    assert [s["type"] for s in sent[0]] == ["reply", "at", "text"]
    assert sent[1] == [{"type": "text", "data": {"text": "第二块"}}]


@pytest.mark.asyncio
async def test_stream_merged_decorates_once_and_long_split_only_first_chunk():
    adapter, event = await _event(reply_with_quote=True, message_text_limit=64)
    adapter._bot.send_group_msg.side_effect = [{"message_id": 1}, {"message_id": 2}]

    async def gen():
        yield MessageChain.from_text("甲" * 60)
        yield MessageChain.from_text("乙" * 60)

    await event.send_streaming(gen())
    sent = _sent_segments(adapter)
    assert len(sent) == 2
    assert sent[0][0]["type"] == "reply" and all(s["type"] == "text" for s in sent[1])


@pytest.mark.asyncio
async def test_group_override_can_enable_quote_for_one_group():
    adapter, event = await _event(wake_group_overrides={"456": {"reply_with_quote": True}})
    await event.send(MessageChain.from_text("回复"))
    assert _sent_segments(adapter)[0][0]["type"] == "reply"


@pytest.mark.parametrize("value", ["true", 1, None])
def test_reply_policy_requires_boolean(value: object):
    with pytest.raises(ValueError, match="reply_with_quote"):
        validate_wake_policy({"reply_with_quote": value})


def test_time_rules_cannot_change_reply_policy():
    rules: dict[str, object] = {"wake_time_rules": [{"start": "01:00", "end": "02:00", "settings": {"reply_with_quote": True}}]}
    with pytest.raises(ValueError):
        validate_wake_overrides(rules)
    groups: dict[str, object] = {"wake_group_overrides": {"1": {"reply_with_mention": False}}}
    validate_wake_overrides(groups)
