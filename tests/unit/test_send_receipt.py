from unittest.mock import AsyncMock

import pytest

from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.receipt import SendReceipt, combine_receipts
from satrap.core.platform.event import MessageChain
from satrap.core.platform import PlatformConfig


async def make_event():
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    adapter._bot = AsyncMock()
    await adapter._handle_group_message({
        "self_id": 10000, "user_id": 123, "group_id": 456, "message_id": 10,
        "message_type": "group", "message": [{"type": "text", "data": {"text": "你好"}}],
    })
    return adapter, adapter._event_queue.get_nowait()


@pytest.mark.asyncio
@pytest.mark.parametrize("response,status,ids", [
    ({"message_id": 42}, "success", ("42",)),
    ({"message_id": "-12"}, "success", ("-12",)),
    (None, "unknown", ()), ({}, "unknown", ()),
    ({"message_id": True}, "unknown", ()),
])
async def test_onebot_receipt_requires_platform_confirmation(response, status, ids):
    adapter, event = await make_event()
    adapter._bot.send_group_msg.return_value = response
    await event.send(MessageChain.from_text("回复"))
    assert event.last_send_receipt.status == status
    assert event.last_send_receipt.message_ids == ids
    assert event.has_send_operation()


@pytest.mark.asyncio
async def test_unavailable_client_is_not_marked_sent():
    adapter, event = await make_event()
    adapter._bot = None
    await event.send(MessageChain.from_text("回复"))
    assert event.last_send_receipt == SendReceipt("failed", reason="client_unavailable")
    assert not event.has_send_operation()


@pytest.mark.asyncio
async def test_timeout_suppresses_whole_response_retry():
    adapter, event = await make_event()
    adapter._bot.send_group_msg.side_effect = TimeoutError()
    await event.send(MessageChain.from_text("回复"))
    assert event.last_send_receipt.status == "unknown"
    assert event.has_send_operation()
    assert adapter._bot.send_group_msg.await_count == 1


@pytest.mark.asyncio
async def test_stream_stops_on_unknown_and_preserves_confirmed_ids():
    adapter, event = await make_event()
    adapter._bot.send_group_msg.side_effect = [{"message_id": 42}, None, {"message_id": 44}]

    async def chunks():
        for text in ("第一块", "第二块", "第三块"):
            yield MessageChain.from_text(text)

    await event.send_streaming(chunks(), use_fallback=True)
    assert adapter._bot.send_group_msg.await_count == 2
    assert event.last_send_receipt == SendReceipt("unknown", ("42",), 1, "missing_message_id")
    assert event.has_send_operation()


@pytest.mark.asyncio
async def test_stream_producer_failure_preserves_sent_prefix():
    adapter, event = await make_event()
    adapter._bot.send_group_msg.return_value = {"message_id": 42}

    async def chunks():
        yield MessageChain.from_text("第一块")
        raise RuntimeError("producer failed")

    await event.send_streaming(chunks(), use_fallback=True)
    assert event.last_send_receipt == SendReceipt("unknown", ("42",), 1, "stream_interrupted")
    assert event.has_send_operation()


@pytest.mark.asyncio
async def test_later_failure_cannot_clear_previous_send_marker():
    adapter, event = await make_event()
    adapter._bot.send_group_msg.return_value = {"message_id": 42}
    await event.send(MessageChain.from_text("第一块"))
    adapter._bot = None
    await event.send(MessageChain.from_text("第二块"))
    assert event.last_send_receipt.status == "failed"
    assert event.has_send_operation()


def test_partial_receipt_aggregation():
    receipt = combine_receipts([SendReceipt("success", ("1",)), SendReceipt("failed", reason="disabled")])
    assert receipt == SendReceipt("partial", ("1",), 1, "disabled")
    assert receipt.suppress_fallback
    assert not combine_receipts([]).suppress_fallback


@pytest.mark.asyncio
async def test_platform_explicit_rejection_is_distinct_from_timeout():
    from aiocqhttp.exceptions import ActionFailed

    adapter, event = await make_event()
    adapter._bot.send_group_msg.side_effect = ActionFailed({"retcode": 100, "wording": "private details"})
    await event.send(MessageChain.from_text("回复"))
    assert event.last_send_receipt == SendReceipt("failed", reason="action_rejected")
    assert not event.has_send_operation()


@pytest.mark.asyncio
async def test_stream_explicit_failure_returns_partial():
    from aiocqhttp.exceptions import ActionFailed

    adapter, event = await make_event()
    adapter._bot.send_group_msg.side_effect = [{"message_id": 42}, ActionFailed({"retcode": 100})]

    async def chunks():
        for text in ("第一块", "第二块", "第三块"):
            yield MessageChain.from_text(text)

    await event.send_streaming(chunks(), use_fallback=True)
    assert event.last_send_receipt == SendReceipt("partial", ("42",), 1, "action_rejected")
    assert event.has_send_operation()
    assert adapter._bot.send_group_msg.await_count == 2
