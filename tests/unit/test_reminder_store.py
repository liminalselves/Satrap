"""提醒持久化, 所有权, 发送竞争与恢复证据"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any
import base64
import hashlib
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


def setup_store(tmp_path: Path) -> tuple[ReminderStore, list[float]]:
    now = [1791040000.0]
    store = ReminderStore(tmp_path / "platform.db", clock=lambda: now[0])
    return store, now


def create(store: ReminderStore, operation: str = "create", actor: str = "member", **kwargs: Any) -> dict[str, Any]:
    return store.create(SCOPE, actor=actor, text="检查联调结果", mentions=["member"], source_message_id="m1", operation_id=operation,
                        time_spec={"after_seconds": 10}, **kwargs)["reminder"]


def target(reminder: dict[str, Any], attempt: str = "attempt-1") -> ScheduledTarget:
    return ScheduledTarget(SCOPE, (object(), 1), "policy", reminder["reminder_id"], attempt, allowed)


def reference_list(store: ReminderStore, *, actor: str = "", state: str = "", limit: int = 20, cursor: str = "") -> dict[str, Any]:
    """
    分页改造前的全量读取实现, 作为签名与切片语义的参照

    参数:
    - store: 当前任务存储
    - actor: 可管理的创建者
    - state: 可选状态筛选
    - limit: 每页条数
    - cursor: 分页游标

    返回:
    - 旧实现的 items, has_more 与 next_cursor
    """
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


def test_page_records_keeps_signature_order_and_skips_deleted_rows(tmp_path: Path) -> None:
    """当页按主键回取时保持签名顺序, 并在签名扫描后删除的行被跳过"""
    store, _ = setup_store(tmp_path)
    identities = [create(store, f"op-{index}")["reminder_id"] for index in range(3)]
    with closing(store._connect()) as connection:
        records = store._page_records(connection, [identities[2], "rem_缺失", identities[0]])
    assert [record["reminder_id"] for record in records] == [identities[2], identities[0]]
    assert store._page_records(connection, []) == []


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
