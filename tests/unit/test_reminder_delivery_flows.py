"""业务链路场景与集中边界契约"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any
import base64
import hashlib
import json
import sqlite3
import threading
import pytest
from satrap.core.config.platform_messages import MessageScope
from satrap.core.group_chat.reminders import ReminderStore, ReminderRecorder, ReminderError
from satrap.core.platform.scheduled import ScheduledTarget
from satrap.core.platform.receipt import SendReceipt
from support.reminder_store import SCOPE, allowed, create, setup_store, target
from dataclasses import replace
from unittest.mock import AsyncMock
import asyncio
from satrap.core.group_chat.reminder_scheduler import ReminderScheduler, ReminderDelivery
from satrap.core.platform.event import MessageChain
from satrap.core.platform.scheduled import execute_scheduled_segments
from satrap.core.components import Plain
from urllib.parse import urlencode
import time
from satrap.core.backend import control_server
from satrap.core.config.platform_messages import ArchiveMessage, PlatformMessageStore
from satrap.core.storage.layout import StorageLayout
from support.http import _request, _json_body, _use_config
from unittest.mock import Mock
from aiocqhttp.exceptions import ApiNotAvailable
from satrap.core.components import At
from satrap.core.platform import PlatformConfig
from satrap.core.platform.onebot.adapter import OneBotAdapter

def reference_list(store: ReminderStore, *, actor: str = "", state: str = "", limit: int = 20, cursor: str = "") -> dict[str, Any]:
    """分页改造前的全量读取实现, 作为签名与切片语义的参照"""
    with closing(store._connect()) as connection:
        rows = connection.execute("SELECT * FROM group_chat_reminders WHERE scope_key=?" + (" AND creator_id=?" if actor else "")
                                  + (" AND state=?" if state else "") + " ORDER BY due_at, reminder_id",
                                  (SCOPE.key, *([actor] if actor else []), *([state] if state else []))).fetchall()
        signature = hashlib.sha256(json.dumps([SCOPE.key, actor, state, limit, [(row["reminder_id"], row["revision"]) for row in rows]]).encode()).hexdigest()
        offset = 0
        if cursor:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if token[0] != signature or type(token[1]) is not int or token[1] < 0:
                raise ReminderError("invalid_cursor", "任务或筛选已变化, 请重新查询")
            offset = token[1]
        more = offset + limit < len(rows)
        return {"items": [store._record(row) for row in rows[offset:offset + limit]], "has_more": more,
                "next_cursor": base64.urlsafe_b64encode(json.dumps([signature, offset + limit]).encode()).decode() if more else None}

def fill_mixed_reminders(store: ReminderStore) -> dict[str, Any]:
    """铺多创建者多状态的提醒, 覆盖排序与筛选维度"""
    first = create(store, "first")
    create(store, "second", "other")
    create(store, "third", "other")
    create(store, "cancel-me")
    store.change(SCOPE, first["reminder_id"], "cancel", first["revision"], actor="member")
    paused = create(store, "pause-me")
    assert store.transition(paused["reminder_id"], ("scheduled",), "paused", "reminders_disabled")
    return first

@pytest.mark.parametrize("filters", [{}, {"actor": "member"}, {"actor": "other"}, {"state": "scheduled"}, {"state": "cancelled"},
                                     {"state": "paused"}, {"actor": "other", "state": "scheduled"}])
def test_list_pagination_matches_full_scan_reference(tmp_path: Path, filters: dict[str, str]) -> None:
    """窄查询分页与改造前的全量读取逐页一致, 含 has_more 与游标字节"""
    store, _ = setup_store(tmp_path)
    fill_mixed_reminders(store)
    for limit in (1, 2, 3):
        collected: list[str] = []
        cursor = ""
        while True:
            page = store.list(SCOPE, limit=limit, cursor=cursor, **filters)
            reference = reference_list(store, limit=limit, cursor=cursor, **filters)
            assert page["items"] == reference["items"]
            assert page["has_more"] == reference["has_more"]
            assert page["next_cursor"] == reference["next_cursor"]
            collected.extend(item["reminder_id"] for item in page["items"])
            cursor = page["next_cursor"] or ""
            if not cursor:
                break
        assert collected == [item["reminder_id"] for item in store.list(SCOPE, limit=50, **filters)["items"]]

def test_cursor_recorded_by_full_scan_implementation_is_still_accepted(tmp_path: Path) -> None:
    """签名公式未变: 旧实现写出的游标仍指向同一页"""
    store, _ = setup_store(tmp_path)
    for index in range(3):
        create(store, f"op-{index}")
    with closing(store._connect()) as connection:
        rows = connection.execute("SELECT * FROM group_chat_reminders WHERE scope_key=? ORDER BY due_at, reminder_id", (SCOPE.key,)).fetchall()
    ordered = [row["reminder_id"] for row in rows]
    signature = hashlib.sha256(json.dumps([SCOPE.key, "", "", 2, [(row["reminder_id"], row["revision"]) for row in rows]]).encode()).hexdigest()
    legacy_cursor = base64.urlsafe_b64encode(json.dumps([signature, 2]).encode()).decode()
    page = store.list(SCOPE, limit=2, cursor=legacy_cursor)
    assert [item["reminder_id"] for item in page["items"]] == ordered[2:]

def test_page_records_keeps_signature_order(tmp_path: Path) -> None:
    """当页按主键回取时保持签名顺序, 与签名出自同一读事务因此不存在缺行"""
    store, _ = setup_store(tmp_path)
    identities = [create(store, f"op-{index}")["reminder_id"] for index in range(3)]
    with closing(store._connect()) as connection:
        records = store._page_records(connection, [identities[2], identities[0]])
        empty = store._page_records(connection, [])
    assert [record["reminder_id"] for record in records] == [identities[2], identities[0]]
    assert empty == []

def test_list_page_and_cursor_come_from_one_read_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """签名与当页正文出自同一读事务: 两次查询之间的并发取消不改变本页, 提交后旧游标失效"""
    store, _ = setup_store(tmp_path)
    reminders = [create(store, f"op-{index}") for index in range(3)]
    before = store.list(SCOPE, limit=2)

    real_offset = ReminderStore._offset
    writer: list[threading.Thread] = []
    errors: list[Exception] = []

    def concurrent_cancel() -> None:
        try:
            store.change(SCOPE, reminders[0]["reminder_id"], "cancel", reminders[0]["revision"], actor="member")
        except Exception as exc:  # 写入线程的异常必须显式带出
            errors.append(exc)

    def hooked_offset(cursor: str, signature: str) -> int:
        offset = real_offset(cursor, signature)
        if not writer:
            thread = threading.Thread(target=concurrent_cancel)
            writer.append(thread)
            thread.start()
            thread.join(0.5)
        return offset

    monkeypatch.setattr(ReminderStore, "_offset", staticmethod(hooked_offset))
    raced = store.list(SCOPE, limit=2)
    writer[0].join()
    assert not errors
    # 有判别力的是条目: 修复前并发取消的修订与状态会进入本页; 游标签名在写入前已算定, 两种实现下一致
    assert json.dumps(raced["items"], ensure_ascii=False, sort_keys=True) == json.dumps(before["items"], ensure_ascii=False, sort_keys=True)
    assert raced["next_cursor"] == before["next_cursor"] and raced["has_more"] == before["has_more"]
    with pytest.raises(ReminderError, match="重新查询"):
        store.list(SCOPE, limit=2, cursor=before["next_cursor"])

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
    assert reloaded.active() == []
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

async def drain(scheduler):
    await asyncio.gather(*list(scheduler._workers.values()))
    await asyncio.sleep(0)

@pytest.mark.asyncio
async def test_disable_pauses_future_tasks_and_enable_does_not_resume(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    enabled = [False]

    async def resolve(record, prepare):
        return ReminderDelivery("ready" if enabled[0] else "paused", "reminders_disabled")

    scheduler = ReminderScheduler(lambda: [store], resolve, clock=lambda: now[0], monotonic=lambda: now[0])
    await scheduler.tick()
    assert store.get(SCOPE, reminder["reminder_id"])["reminder"]["state"] == "paused"
    enabled[0] = True
    now[0] += 10
    await scheduler.tick()
    assert not scheduler._workers and store.get(SCOPE, reminder["reminder_id"])["reminder"]["state"] == "paused"

@pytest.mark.asyncio
async def test_offline_backoff_missed_and_unknown_are_not_blindly_resent(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    sender = AsyncMock(return_value=SendReceipt("unknown", reason="timeout"))
    online = [False]

    async def send(frozen, chain, recorder):
        return await execute_scheduled_segments(frozen, recorder, [{"index": 0}], [sender])

    async def resolve(record, prepare):
        if not online[0]:
            return ReminderDelivery("waiting", "offline", grace=600)
        return ReminderDelivery("ready", target=target(record), chain=MessageChain([Plain("提醒")]), send=send)

    scheduler = ReminderScheduler(lambda: [store], resolve, clock=lambda: now[0], monotonic=lambda: now[0])
    await scheduler.tick()
    now[0] += 10
    await scheduler.tick()
    await drain(scheduler)
    waiting = store.get(SCOPE, reminder["reminder_id"])["reminder"]
    assert waiting["state"] == "waiting_delivery" and waiting["retry_at"] == now[0] + 30
    sender.assert_not_awaited()
    online[0] = True
    now[0] += 30
    await scheduler.tick()
    await drain(scheduler)
    assert store.get(SCOPE, reminder["reminder_id"])["reminder"]["state"] == "unknown"
    now[0] += 300
    await scheduler.tick()
    sender.assert_awaited_once()
    late = create(store, "late")
    online[0] = False
    now[0] += 611
    await scheduler.tick()
    await drain(scheduler)
    assert store.get(SCOPE, late["reminder_id"])["reminder"]["state"] == "missed"

@pytest.mark.asyncio
async def test_clock_jump_skips_claim_until_stable_then_checks_original_deadline(tmp_path):
    store, now = setup_store(tmp_path)
    reminder = create(store)
    monotonic = [0.0]

    async def resolve(record, prepare):
        return ReminderDelivery("waiting", "offline", grace=600)

    scheduler = ReminderScheduler(lambda: [store], resolve, clock=lambda: now[0], monotonic=lambda: monotonic[0])
    await scheduler.tick()
    now[0] += 1000
    monotonic[0] += 5
    await scheduler.tick()
    assert scheduler._clock_unstable and not scheduler._workers
    now[0] += 5
    monotonic[0] += 5
    await scheduler.tick()
    await drain(scheduler)
    assert store.get(SCOPE, reminder["reminder_id"])["reminder"]["state"] == "missed"

@pytest.mark.asyncio
async def test_authenticated_cold_reminder_read_and_cancel_do_not_change_archives(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("future")
    monkeypatch.setattr(control_server, "_configured_storage_layout", lambda: layout)
    archive = PlatformMessageStore(layout.platform_db("future"), "future")
    scope = MessageScope("future", "bot:/a", "group", "群/中文")
    archive.record(scope, ArchiveMessage("source", "member", time.time(), "请提醒我"))
    reminder = ReminderStore(archive.database).create(scope, actor="member", text="检查结果", mentions=[], source_message_id="source", operation_id="create", time_spec={"after_seconds": 30})["reminder"]
    query = urlencode({"self_id": scope.self_id, "chat_id": scope.chat_id, "conversation_kind": "group"})
    root = "/api/platforms/future/group-chat/reminders"
    assert (await _request(f"{root}?{query}", authorized=False)).startswith(b"HTTP/1.1 401")
    listing = _json_body(await _request(f"{root}?{query}"))
    assert listing["items"][0]["source_status"] == "available"
    target = f"{root}/{reminder['reminder_id']}"
    wrong = urlencode({"self_id": "other", "chat_id": scope.chat_id})
    assert (await _request(f"{target}?{wrong}")).startswith(b"HTTP/1.1 404")
    conflict = await _request(f"{target}/cancel?{query}", "POST", json.dumps({"expected_revision": 99}).encode())
    assert conflict.startswith(b"HTTP/1.1 409")
    cancelled = _json_body(await _request(f"{target}/cancel?{query}", "POST", json.dumps({"expected_revision": 1}).encode()))
    assert cancelled["status"] == "cancelled" and archive.get(scope, "source")["text"] == "请提醒我"
    archive.delete(scope, expected_revision=0, message_ids=["source"])
    detail = _json_body(await _request(f"{target}?{query}"))
    assert detail["reminder"]["source_status"] == "unavailable"
    assert detail["reminder"]["text"] == "检查结果" and detail["reminder"]["state"] == "cancelled"

def test_new_same_named_platform_has_a_new_incarnation_and_updates_preserve_it():
    from satrap.core.config.document import upsert_platform, delete_platform, validate_platforms

    legacy = validate_platforms([{"id": "same", "type": "future", "settings": {}}])[0]
    edited = upsert_platform([legacy], {"id": "same", "type": "future", "settings": {"x": 1}}, original_id="same")[0]
    assert edited["instance_id"] == legacy["instance_id"]
    recreated = upsert_platform(delete_platform([edited], "same"), {"id": "same", "type": "future", "settings": {}})[0]
    assert recreated["instance_id"] != legacy["instance_id"]

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
    assert not store.active()

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
async def test_api_unavailable_after_submission_is_unknown_and_cannot_send_again(tmp_path):
    adapter, store, reminder, frozen = setup(tmp_path)
    adapter._bot.send_group_msg.side_effect = ApiNotAvailable()
    chain = MessageChain([Plain('提醒')])
    result = await adapter.group_chat_send_scheduled(frozen, chain, ReminderRecorder(store, reminder, frozen.attempt_id))
    assert result.status == 'unknown' and not result.message_ids
    assert store.get(frozen.scope, reminder['reminder_id'])['reminder']['state'] == 'unknown'
    await adapter.group_chat_send_scheduled(frozen, chain, ReminderRecorder(store, reminder, frozen.attempt_id))
    adapter._bot.send_group_msg.assert_awaited_once()
    adapter._archive_sent_segments.assert_not_called()

@pytest.mark.asyncio
async def test_missing_native_confirmation_is_unknown_not_success(tmp_path):
    adapter, store, reminder, frozen = setup(tmp_path)
    adapter._bot.send_group_msg.side_effect = None
    adapter._bot.send_group_msg.return_value = {}
    result = await adapter.group_chat_send_scheduled(frozen, MessageChain([Plain("提醒")]), ReminderRecorder(store, reminder, frozen.attempt_id))
    assert result.status == "unknown" and not result.message_ids
    assert store.get(frozen.scope, reminder["reminder_id"])["reminder"]["state"] == "unknown"
    adapter._archive_sent_segments.assert_not_called()
