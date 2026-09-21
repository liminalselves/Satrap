import asyncio

import pytest

from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform import PlatformConfig


def payload(message_id=10, self_id=10000, user_id=123, group_id=456):
    return {"self_id": self_id, "user_id": user_id, "group_id": group_id,
            "message_id": message_id, "message_type": "group",
            "message": [{"type": "text", "data": {"text": "测试正文"}}]}


@pytest.mark.asyncio
async def test_account_binding_and_echo_filter_do_not_poison_dedup():
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    await adapter._handle_group_message(payload(self_id=None))
    assert adapter.bot_self_id == ""
    await adapter._handle_group_message(payload())
    await adapter._handle_group_message(payload(11, self_id=20000))
    await adapter._handle_group_message(payload(11, user_id=10000))
    await adapter._handle_group_message(payload(11))
    assert adapter.bot_self_id == adapter.client_self_id == "10000"
    assert adapter._event_queue.qsize() == 2
    assert adapter.get_stats()["ingress"] == {
        "account": 2, "self_echo": 1, "duplicate": 0, "dedup_entries": 2,
        "dedup_capacity": 4096, "dedup_ttl": 120,
    }


@pytest.mark.asyncio
async def test_configured_account_rejects_first_foreign_event():
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={"self_id": "10000"}))
    await adapter._handle_group_message(payload(self_id=20000))
    assert adapter._event_queue.empty()
    assert adapter.client_self_id == "10000"


@pytest.mark.asyncio
async def test_replay_scope_expiry_and_instance_isolation(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("satrap.core.platform.onebot.adapter.monotonic", lambda: clock[0])
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    other = OneBotAdapter(PlatformConfig(id="other", type="onebot", settings={}))
    await adapter._handle_group_message(payload())
    await adapter._handle_group_message(payload(user_id=124))
    await adapter._handle_group_message(payload(group_id=789))
    await other._handle_group_message(payload())
    assert adapter._event_queue.qsize() == 2
    assert other._event_queue.qsize() == 1
    clock[0] = 220.0
    await adapter._handle_group_message(payload())
    assert adapter._event_queue.qsize() == 3
    assert len(adapter._seen_messages) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_and_cancelled_conversion(monkeypatch):
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    entered = asyncio.Event()
    release = asyncio.Event()
    original = adapter.convert_message

    async def convert(raw):
        entered.set()
        await release.wait()
        return await original(raw)

    monkeypatch.setattr(adapter, "convert_message", convert)
    first = asyncio.create_task(adapter._handle_group_message(payload()))
    await entered.wait()
    await adapter._handle_group_message(payload())
    assert adapter._event_queue.empty()
    assert adapter.get_stats()["ingress"]["duplicate"] == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not adapter._seen_messages
    release.set()
    await adapter._handle_group_message(payload())
    assert adapter._event_queue.qsize() == 1


@pytest.mark.asyncio
async def test_full_queue_does_not_mark_message_delivered():
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={"event_queue_capacity": 1}))
    await adapter._handle_group_message(payload())
    await adapter._handle_group_message(payload(11))
    assert len(adapter._seen_messages) == 1
    adapter._event_queue.get_nowait()
    adapter._event_queue.task_done()
    await adapter._handle_group_message(payload(11))
    assert adapter._event_queue.get_nowait().call_origin.source_message_id == "11"


@pytest.mark.asyncio
async def test_dedup_capacity_is_bounded_and_termination_clears_state():
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    for message_id in range(4100):
        await adapter._handle_group_message(payload(message_id))
        adapter._event_queue.get_nowait()
        adapter._event_queue.task_done()
    assert len(adapter._seen_messages) == 4096
    await adapter.terminate()
    assert not adapter._seen_messages


@pytest.mark.asyncio
async def test_missing_message_id_is_not_a_shared_dedup_key():
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    for _ in range(2):
        await adapter._handle_group_message(payload(message_id=None))
    assert adapter._event_queue.qsize() == 2
    assert not adapter._seen_messages
