"""OneBot 后台提醒使用真实分段队列与独享证据, 不依赖原消息事件"""
from contextlib import closing
from unittest.mock import AsyncMock, Mock
import asyncio
import json

import pytest

from satrap.core.components import Plain, At
from satrap.core.config.platform_messages import MessageScope
from satrap.core.group_chat.reminders import ReminderRecorder
from satrap.core.platform import PlatformConfig
from satrap.core.platform.event import MessageChain
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.scheduled import ScheduledTarget
from .test_reminder_store import setup_store


def setup(tmp_path):
    store, now = setup_store(tmp_path)
    scope = MessageScope("onebot", "10000", "group", "456")
    adapter = OneBotAdapter(PlatformConfig(id="onebot", type="onebot", settings={"message_text_limit": 64}))
    adapter._bot = AsyncMock()
    adapter.bot_self_id = adapter.client_self_id = scope.self_id
    adapter._archive_sent_segments = Mock()
    adapter._bot.send_group_msg.side_effect = [{"message_id": 101}, {"message_id": 102}, {"message_id": 103}]
    reminder = store.create(scope, actor="123", text="a" * 150, mentions=["123"], source_message_id="m1",
                            operation_id="create", time_spec={"after_seconds": 10})["reminder"]
    now[0] += 10

    async def guard():
        return adapter.group_chat_self_id() == scope.self_id

    frozen = ScheduledTarget(scope, adapter.group_chat_connection_token(), "policy", reminder["reminder_id"], "attempt", guard)
    return adapter, store, reminder, frozen


@pytest.mark.asyncio
async def test_native_split_ids_recorded_and_ordinary_recorder_is_untouched(tmp_path):
    adapter, store, reminder, frozen = setup(tmp_path)
    ordinary_recorder = Mock()
    adapter._send_attempt_recorder = ordinary_recorder
    recorder = ReminderRecorder(store, reminder, frozen.attempt_id)
    result = await adapter.group_chat_send_scheduled(frozen, MessageChain([At(qq="123"), Plain("a" * 150)]), recorder)
    assert result.status == "success" and result.message_ids == ("101", "102", "103")
    assert adapter._bot.send_group_msg.await_count == 3
    assert adapter._archive_sent_segments.call_count == 3
    assert adapter._send_attempt_recorder is ordinary_recorder
    ordinary_recorder.record_send_attempt.assert_not_called()
    with closing(store._connect()) as connection:
        attempt = connection.execute("SELECT * FROM group_chat_reminder_attempts").fetchone()
        assert len(json.loads(attempt["plan_json"])) == 3 and attempt["status"] == "sent"


@pytest.mark.asyncio
async def test_queue_wait_revalidates_account_before_claim(tmp_path):
    adapter, store, reminder, frozen = setup(tmp_path)
    lock = asyncio.Lock()
    await lock.acquire()
    adapter._outbound.locks["group%456"] = (lock, 0)
    send = asyncio.create_task(adapter.group_chat_send_scheduled(frozen, MessageChain([Plain("提醒")]), ReminderRecorder(store, reminder, frozen.attempt_id)))
    await asyncio.sleep(0)
    adapter.bot_self_id = adapter.client_self_id = "20000"
    lock.release()
    result = await send
    assert result.status == "failed" and result.reason == "scheduled_target_changed"
    adapter._bot.send_group_msg.assert_not_awaited()
    assert store.get(frozen.scope, reminder["reminder_id"])["reminder"]["state"] == "scheduled"


@pytest.mark.asyncio
async def test_missing_native_confirmation_is_unknown_not_success(tmp_path):
    adapter, store, reminder, frozen = setup(tmp_path)
    adapter._bot.send_group_msg.side_effect = None
    adapter._bot.send_group_msg.return_value = {}
    result = await adapter.group_chat_send_scheduled(frozen, MessageChain([Plain("提醒")]), ReminderRecorder(store, reminder, frozen.attempt_id))
    assert result.status == "unknown" and not result.message_ids
    assert store.get(frozen.scope, reminder["reminder_id"])["reminder"]["state"] == "unknown"
    adapter._archive_sent_segments.assert_not_called()
