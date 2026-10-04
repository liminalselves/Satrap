"""提醒持久化, 所有权, 发送竞争与恢复证据"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3

import pytest

from satrap.core.config.platform_messages import MessageScope
from satrap.core.group_chat.reminders import ReminderStore, ReminderRecorder, ReminderError
from satrap.core.platform.scheduled import ScheduledTarget
from satrap.core.platform.receipt import SendReceipt


SCOPE = MessageScope("future-platform", "bot:/账号", "group", "group:/群")


async def allowed():
    return True


def setup_store(tmp_path):
    now = [1791040000.0]
    store = ReminderStore(tmp_path / "platform.db", clock=lambda: now[0])
    return store, now


def create(store, operation="create", actor="member", **kwargs):
    return store.create(SCOPE, actor=actor, text="检查联调结果", mentions=["member"], source_message_id="m1", operation_id=operation,
                        time_spec={"after_seconds": 10}, **kwargs)["reminder"]


def target(reminder, attempt="attempt-1"):
    return ScheduledTarget(SCOPE, (object(), 1), "policy", reminder["reminder_id"], attempt, allowed)


def test_relative_retry_freezes_deadline_and_cannot_change_payload(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    now[0] += 300
    assert create(store)["due_timestamp"] == reminder["due_timestamp"]
    with pytest.raises(ReminderError, match="不同内容"):
        store.create(SCOPE, actor="member", text="不同内容", mentions=["member"], source_message_id="m1", operation_id="create", time_spec={"after_seconds": 10})
    assert len(store.list(SCOPE)["items"]) == 1


def test_member_scope_cursor_revision_and_quota_are_enforced(tmp_path):
    store, _ = setup_store(tmp_path)
    first = create(store)
    second = create(store, "second", "other")
    assert [item["reminder_id"] for item in store.list(SCOPE, actor="member")["items"]] == [first["reminder_id"]]
    with pytest.raises(ReminderError, match="不存在"):
        store.get(SCOPE, second["reminder_id"], actor="member")
    other_scope = MessageScope(SCOPE.adapter_id, "another-account", "group", SCOPE.chat_id)
    with pytest.raises(ReminderError, match="不存在"):
        store.get(other_scope, first["reminder_id"])
    cursor = store.list(SCOPE, limit=1)["next_cursor"]
    with pytest.raises(ReminderError, match="重新查询"):
        store.list(other_scope, limit=1, cursor=cursor)
    with pytest.raises(ReminderError, match="上限"):
        create(store, "quota", member_limit=1)
    with pytest.raises(ReminderError, match="重新查询"):
        store.change(SCOPE, first["reminder_id"], "cancel", 99, actor="member")


def test_cancel_and_plan_compete_in_same_transaction(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    now[0] += 10
    recorder = ReminderRecorder(store, reminder, "attempt-1")
    frozen = target(reminder)

    def cancel():
        try:
            return store.change(SCOPE, reminder["reminder_id"], "cancel", 1, actor="member")["status"]
        except ReminderError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(cancel)
        planned = recorder.plan(frozen, [{"index": 0}])
        result = future.result()
    current = store.get(SCOPE, reminder["reminder_id"])["reminder"]
    if planned:
        assert current["state"] == "sending" and result == "revision_conflict"
        assert store.change(SCOPE, reminder["reminder_id"], "cancel", current["revision"])["status"] == "too_late_to_cancel"
    else:
        assert current["state"] == result == "cancelled"


def test_plan_failure_rolls_back_claim(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    now[0] += 10
    recorder = ReminderRecorder(store, reminder, "attempt-1")
    with closing(store._connect()) as connection, connection:
        connection.execute("CREATE TRIGGER reject_attempt BEFORE INSERT ON group_chat_reminder_attempts BEGIN SELECT RAISE(ABORT, 'disk rejected plan'); END")
    with pytest.raises(sqlite3.IntegrityError, match="disk rejected"):
        recorder.plan(target(reminder), [{"index": 0}])
    assert store.get(SCOPE, reminder["reminder_id"])["reminder"]["state"] == "scheduled"


def test_recovery_keeps_confirmed_ids_without_requeueing(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    now[0] += 10
    recorder = ReminderRecorder(store, reminder, "attempt-1")
    assert recorder.plan(target(reminder), [{"index": 0}, {"index": 1}])
    assert recorder.submitted(0)
    assert recorder.result(0, SendReceipt("success", ("confirmed-id",)))
    assert recorder.submitted(1)
    reloaded = ReminderStore(store.database, clock=lambda: now[0])
    assert reloaded.recover() == 1
    current = reloaded.get(SCOPE, reminder["reminder_id"])["reminder"]
    assert current["state"] == "unknown"
    assert current["delivery"]["message_ids"] == ["confirmed-id"]
    assert reloaded.due() == []
    assert reloaded.recover() == 0


def test_pause_requires_explicit_resume_and_cleanup_keeps_active_tasks(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    identity = reminder["reminder_id"]
    assert store.transition(identity, ("scheduled",), "paused", "reminders_disabled")
    paused = store.get(SCOPE, identity)["reminder"]
    assert paused["paused_at"] == now[0] and not store.active()
    assert store.change(SCOPE, identity, "resume", paused["revision"])["status"] == "scheduled"
    cancelled = store.change(SCOPE, identity, "cancel", 3)
    now[0] += 31 * 86400
    active = create(store, "new")
    assert store.cleanup() == 1
    assert store.get(SCOPE, active["reminder_id"])["reminder"]["state"] == "scheduled"
    with closing(store._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reminder_operations").fetchone()[0] == 2
    with pytest.raises(ReminderError, match="不存在"):
        create(store)
    assert cancelled["status"] == "cancelled"


def test_attempt_and_terminal_receipt_commit_together(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    now[0] += 10
    recorder = ReminderRecorder(store, reminder, "attempt-1")
    assert recorder.plan(target(reminder), [{"index": 0}, {"index": 1}])
    assert recorder.submitted(0)
    assert recorder.result(0, SendReceipt("success", ("id",)))
    assert recorder.complete(SendReceipt("partial", ("id",), 1, "rejected"))
    current = store.get(SCOPE, reminder["reminder_id"])["reminder"]
    assert current["state"] == "partial" and current["delivery"]["message_ids"] == ["id"]
    with closing(store._connect()) as connection:
        attempt = connection.execute("SELECT * FROM group_chat_reminder_attempts").fetchone()
        assert attempt["status"] == "partial" and json.loads(attempt["plan_json"])[1]["state"] == "skipped"
    assert store.recover() == 0


def test_success_requires_confirmed_ids_for_every_planned_segment(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    now[0] += 10
    recorder = ReminderRecorder(store, reminder, "attempt-1")
    assert recorder.plan(target(reminder), [{"index": 0}, {"index": 1}])
    assert not recorder.complete(SendReceipt("success", ("invented",)))
    assert recorder.submitted(0)
    assert not recorder.result(0, SendReceipt("success"))
    assert recorder.result(0, SendReceipt("success", ("id-1",)))
    assert not recorder.complete(SendReceipt("success", ("id-1",)))
    assert recorder.submitted(1)
    assert recorder.result(1, SendReceipt("success", ("id-2",)))
    assert not recorder.complete(SendReceipt("success", ("wrong-id",)))
    assert recorder.complete(SendReceipt("success", ("id-1", "id-2")))
    assert store.get(SCOPE, reminder["reminder_id"])["reminder"]["state"] == "sent"
