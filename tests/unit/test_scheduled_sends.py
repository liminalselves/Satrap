"""真实 SQLite 证据与后台分段发送, 任一记录失败禁止后续网络操作"""
from contextlib import closing
from unittest.mock import AsyncMock
import asyncio

import pytest

from satrap.core.platform.scheduled import execute_scheduled_segments
from satrap.core.platform.receipt import SendReceipt
from satrap.core.group_chat.reminders import ReminderRecorder
from .test_reminder_store import setup_store, create, target


@pytest.mark.asyncio
async def test_send_checks_plan_and_each_submission_before_network(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    now[0] += 10
    recorder = ReminderRecorder(store, reminder, "attempt-1")
    frozen = target(reminder)

    async def send():
        with closing(store._connect()) as connection:
            attempt = connection.execute("SELECT status, plan_json FROM group_chat_reminder_attempts").fetchone()
            assert attempt["status"] == "sending" and "submitted" in attempt["plan_json"]
        assert store.get(recorder.scope, recorder.identity)["reminder"]["state"] == "sending"
        return SendReceipt("success", ("message",))

    assert (await execute_scheduled_segments(frozen, recorder, [{"index": 0}], [send])).status == "success"
    assert store.get(recorder.scope, recorder.identity)["reminder"]["state"] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["plan", "submitted"])
async def test_pre_send_ledger_failure_never_sends(tmp_path, monkeypatch, failure):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    now[0] += 10
    recorder = ReminderRecorder(store, reminder, "attempt-1")
    monkeypatch.setattr(recorder, failure, lambda *args: False)
    sender = AsyncMock(return_value=SendReceipt("success", ("id",)))
    outcome = await execute_scheduled_segments(target(reminder), recorder, [{"index": 0}], [sender])
    assert outcome.status == "failed"
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_result_record_failure_stops_next_segment_and_retains_known_ids(tmp_path, monkeypatch):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    now[0] += 10
    recorder = ReminderRecorder(store, reminder, "attempt-1")
    monkeypatch.setattr(recorder, "result", lambda *args: False)
    first = AsyncMock(return_value=SendReceipt("success", ("known",)))
    second = AsyncMock(return_value=SendReceipt("success", ("duplicate",)))
    outcome = await execute_scheduled_segments(target(reminder), recorder, [{"index": 0}, {"index": 1}], [first, second])
    assert outcome.status == "unknown" and outcome.message_ids == ("known",)
    second.assert_not_awaited()
    assert store.get(recorder.scope, recorder.identity)["reminder"]["state"] == "unknown"


@pytest.mark.asyncio
async def test_policy_change_between_segments_keeps_partial_result(tmp_path):
    from dataclasses import replace

    store, now = setup_store(tmp_path)
    reminder = create(store)
    now[0] += 10
    recorder = ReminderRecorder(store, reminder, "attempt-1")
    enabled = [True]

    async def guard():
        return enabled[0]

    async def first():
        enabled[0] = False
        return SendReceipt("success", ("known",))

    second = AsyncMock(return_value=SendReceipt("success", ("not-sent",)))
    outcome = await execute_scheduled_segments(replace(target(reminder), guard=guard), recorder, [{"index": 0}, {"index": 1}], [first, second])
    assert outcome.status == "partial" and outcome.message_ids == ("known",)
    second.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupt_after_submission_is_unknown_and_never_due_again(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    now[0] += 10
    recorder = ReminderRecorder(store, reminder, "attempt-1")

    async def send():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await execute_scheduled_segments(target(reminder), recorder, [{"index": 0}], [send])
    assert store.get(recorder.scope, recorder.identity)["reminder"]["state"] == "unknown"
    assert not store.due()
