import asyncio
import copy
import json
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest

from satrap.core.config.conversation_data import ConversationDataService, ConversationDataConflict
from satrap.core.config.conversation_runtime import manage_platform_data
from satrap.core.framework.SessionManager import SessionEntry
from satrap.core.storage.database import snapshot_session_domain, delete_session_domain_rows, restore_session_domain
from satrap.core.utils.context import ContextManager, AsyncContextManager
from satrap.display.recorder import DisplayRecorder


@pytest.fixture
def saved(tmp_path):
    database = tmp_path / "platform.db"
    context = ContextManager("conversation_main", db_path=str(database), enable_checkpoint=True)
    messages = [{"role": "system", "content": "原始提示词"}, {"role": "user", "content": [{"type": "text", "text": "图片提问"}, {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}]}, {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}, {"id": "call-2", "type": "function", "function": {"name": "probe2", "arguments": "{}"}}]}, {"role": "tool", "content": "结果一", "tool_call_id": "call-1"}, {"role": "tool", "content": "结果二", "tool_call_id": "call-2"}, {"role": "assistant", "content": "原始回复", "reasoning_content": "原始思考"}]
    context.replace_messages(messages)
    context.load_context()
    messages = copy.deepcopy(context.get_context())
    recorder = DisplayRecorder(str(database), "conversation")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("INSERT INTO conversation_meta (conversation_id, model, think, created_at) VALUES ('conversation', 'default', 'off', 1)")
        connection.execute("INSERT INTO display_turns (conversation_id, turn_index, user_input, answer, segments, created_at, active_variant) VALUES ('conversation', 0, '历史输入', '原始回复', '旧片段', 1, 1)")
        turn = connection.execute("SELECT id FROM display_turns").fetchone()[0]
        for variant in range(2):
            connection.execute("INSERT INTO display_turn_variants (turn_id, variant_index, answer, segments, context_messages, created_at) VALUES (?, ?, ?, '旧片段', '保留的模型快照', 1)", (turn, variant, f"版本{variant}"))
        connection.execute("INSERT INTO display_tool_calls (turn_id, seq, name, created_at, variant_index) VALUES (?, 0, 'probe', 1, 1)", (turn,))
    yield database, context, messages, ConversationDataService(database)
    context.close()
    recorder.close()


def test_read_only_catalog_groups_workflow_and_preserves_content(saved, tmp_path):
    database, _, messages, service = saved
    assert ConversationDataService(tmp_path / "missing.db").list_conversations() == {"items": [], "total": 0}
    assert not (tmp_path / "missing.db").exists()
    catalog = service.list_conversations()
    assert catalog["total"] == 1
    assert catalog["items"][0]["context_ids"] == ["conversation_main"]
    assert catalog["items"][0]["message_count"] == len(messages)
    assert service.list_conversations("conversation_main")["total"] == 1
    snapshot = service.read("conversation_main", "context")
    assert [{key: value for key, value in item.items() if key != "index"} for item in snapshot["items"]] == messages
    assert service.read("conversation", "history")["items"][0]["tool_call_records"][0]["name"] == "probe"
    with closing(sqlite3.connect(database)) as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_data_backups'").fetchone()
    with pytest.raises(KeyError):
        service.read("missing", "context")


def test_context_edit_preserves_tools_multimodal_and_history_and_rejects_stale(saved):
    database, _, messages, service = saved
    history = service.read("conversation", "history")
    snapshot = service.read("conversation_main", "context")
    result = service.mutate("conversation_main", "context", {"action": "edit", "index": 5, "content": "修改回复", "reasoning_content": "修改思考", "expected_revision": snapshot["revision"]})
    assert result["messages"][1:5] == messages[1:5]
    assert result["messages"][5]["reasoning_content"] == "修改思考"
    assert service.read("conversation", "history")["revision"] == history["revision"]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM context_runtime_state WHERE conversation_id='conversation_main'").fetchone()[0] == 0
    with pytest.raises(ConversationDataConflict):
        service.mutate("conversation_main", "context", {"action": "clear", "expected_revision": snapshot["revision"]})
    assert len(service.read("conversation_main", "context")["backups"]) == 1


def test_tool_result_deletion_removes_complete_call_group_and_restores(saved):
    _, _, messages, service = saved
    snapshot = service.read("conversation_main", "context")
    result = service.mutate("conversation_main", "context", {"action": "delete", "index": 3, "expected_revision": snapshot["revision"]})
    assert result["messages"] == [messages[0], messages[1], messages[5]]
    current = service.read("conversation_main", "context")
    restored = service.mutate("conversation_main", "context", {"action": "restore", "backup_id": result["backup_id"], "expected_revision": current["revision"]})
    assert restored["messages"] == messages
    assert len(service.read("conversation_main", "context")["backups"]) == 2


def test_clear_preserves_prompt_and_backup_keeps_empty_scope_discoverable(saved):
    _, _, messages, service = saved
    snapshot = service.read("conversation_main", "context")
    result = service.mutate("conversation_main", "context", {"action": "clear", "expected_revision": snapshot["revision"]})
    assert result["messages"] == [messages[0]]
    current = service.read("conversation_main", "context")
    service.mutate("conversation_main", "context", {"action": "clear", "keep_system": False, "expected_revision": current["revision"]})
    assert service.read("conversation_main", "context")["total"] == 0
    assert service.list_conversations()["items"][0]["context_ids"] == ["conversation_main"]


def test_explicit_null_content_remains_null_after_save(saved):
    _, _, _, service = saved
    snapshot = service.read("conversation_main", "context")
    service.mutate("conversation_main", "context", {"action": "edit", "index": 2, "content": None, "expected_revision": snapshot["revision"]})
    assert service.read("conversation_main", "context")["items"][2]["content"] is None


def test_failed_write_rolls_back_backup_and_all_data(saved, monkeypatch):
    _, _, _, service = saved
    snapshot = service.read("conversation_main", "context")
    def fail(connection, conversation, messages):
        connection.execute("DELETE FROM chat_history WHERE conversation_id=?", (conversation,))
        raise sqlite3.OperationalError("模拟磁盘写入失败")
    monkeypatch.setattr(service, "_write_context", fail)
    with pytest.raises(sqlite3.OperationalError):
        service.mutate("conversation_main", "context", {"action": "clear", "expected_revision": snapshot["revision"]})
    after = service.read("conversation_main", "context")
    assert after["revision"] == snapshot["revision"]
    assert after["backups"] == []


def test_display_edit_delete_restore_do_not_change_context_or_other_variants(saved):
    _, _, _, service = saved
    context = service.read("conversation_main", "context")
    history = service.read("conversation", "history")
    service.mutate("conversation", "history", {"action": "edit", "index": 0, "user_input": "展示输入", "answer": "展示回复", "thinking": "展示思考", "expected_revision": history["revision"]})
    changed = service.read("conversation", "history")
    item = changed["items"][0]
    assert item["variants"][0]["answer"] == "版本0"
    assert item["variants"][1]["answer"] == "展示回复"
    assert item["variants"][1]["segments"] is None
    assert item["variants"][1]["context_messages"] == "保留的模型快照"
    assert item["tool_call_records"][0]["name"] == "probe"
    deleted = service.mutate("conversation", "history", {"action": "delete", "index": 0, "expected_revision": changed["revision"]})
    empty = service.read("conversation", "history")
    assert empty["total"] == 0
    service.mutate("conversation", "history", {"action": "restore", "backup_id": deleted["backup_id"], "expected_revision": empty["revision"]})
    assert service.read("conversation", "history")["items"] == changed["items"]
    assert service.read("conversation_main", "context")["revision"] == context["revision"]


def test_backup_scope_isolation_and_archive_round_trip(saved):
    database, _, _, service = saved
    current = service.read("conversation_main", "context")
    result = service.mutate("conversation_main", "context", {"action": "clear", "expected_revision": current["revision"]})
    history = service.read("conversation", "history")
    with pytest.raises(KeyError):
        service.mutate("conversation", "history", {"action": "restore", "backup_id": result["backup_id"], "expected_revision": history["revision"]})
    records = snapshot_session_domain(database, "conversation")
    assert len(records["conversation_data_backups"]) == 1
    delete_session_domain_rows(database, "conversation")
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_data_backups").fetchone()[0] == 0
    restore_session_domain(database, "conversation", records)
    assert service.read("conversation_main", "context")["backups"][0]["id"] == result["backup_id"]


def test_materialized_checkpoint_restoration_and_foreign_scope_rejection(saved):
    _, context, messages, service = saved
    checkpoint = context.state_store.materialize_edit_protection(context._scope())
    snapshot = service.read("conversation_main", "context")
    service.mutate("conversation_main", "context", {"action": "clear", "expected_revision": snapshot["revision"]})
    current = service.read("conversation_main", "context")
    restored = service.mutate("conversation_main", "context", {"action": "checkpoint", "checkpoint_id": checkpoint.checkpoint_id, "expected_revision": current["revision"]})
    assert restored["messages"] == messages
    with pytest.raises(KeyError):
        service.mutate("conversation", "context", {"action": "checkpoint", "checkpoint_id": checkpoint.checkpoint_id, "expected_revision": service.read("conversation", "context")["revision"]})


@pytest.mark.parametrize("content", [[1], [{"text": "缺少类型"}], {"type": "text"}])
def test_invalid_multimodal_edit_does_not_write(saved, content):
    _, _, _, service = saved
    before = service.read("conversation_main", "context")
    with pytest.raises(ValueError):
        service.mutate("conversation_main", "context", {"action": "edit", "index": 1, "content": content, "expected_revision": before["revision"]})
    assert service.read("conversation_main", "context")["revision"] == before["revision"]


@pytest.mark.asyncio
async def test_live_async_context_is_updated_and_busy_round_is_rejected(saved):
    database, _, _, service = saved
    context = AsyncContextManager("conversation_main", db_path=str(database))
    await context.initialize()
    entry = SessionEntry(session=SimpleNamespace(_all_contexts=lambda: {"main": context}), session_type="probe", created_at=1, last_used=1)
    manager = SimpleNamespace(pool=SimpleNamespace(list_entries=lambda: {"conversation": entry}), _entry_creation_lock=threading.RLock())
    snapshot = await manage_platform_data(str(database), manager, "conversation_main", "context", {})
    assert snapshot["source"] == "memory"
    await manage_platform_data(str(database), manager, "conversation_main", "context", {"action": "edit", "index": 5, "content": "活动实例修改", "expected_revision": snapshot["revision"]})
    assert context.get_context()[-1]["content"] == "活动实例修改"
    await context.add_user_message("后续输入")
    assert service.read("conversation_main", "context")["items"][-2]["content"] == "活动实例修改"
    entry.active_calls = 1
    before = service.read("conversation_main", "context")
    with pytest.raises(ConversationDataConflict):
        await manage_platform_data(str(database), manager, "conversation_main", "context", {"action": "clear", "expected_revision": before["revision"]})
    assert service.read("conversation_main", "context")["revision"] == before["revision"]


@pytest.mark.asyncio
async def test_cancelled_edit_finishes_disk_and_memory_before_releasing_instance(saved, monkeypatch):
    database, _, _, service = saved
    context = AsyncContextManager("conversation_main", db_path=str(database))
    await context.initialize()
    entry = SessionEntry(session=SimpleNamespace(_all_contexts=lambda: {"main": context}), session_type="probe", created_at=1, last_used=1)
    manager = SimpleNamespace(pool=SimpleNamespace(list_entries=lambda: {"conversation": entry}), _entry_creation_lock=threading.RLock())
    entered = threading.Event()
    proceed = threading.Event()
    original = ConversationDataService._write_context
    def slow_write(connection, conversation, messages):
        entered.set()
        assert proceed.wait(3)
        original(connection, conversation, messages)
    monkeypatch.setattr(ConversationDataService, "_write_context", staticmethod(slow_write))
    before = service.read("conversation_main", "context")
    task = asyncio.create_task(manage_platform_data(str(database), manager, "conversation_main", "context", {"action": "clear", "expected_revision": before["revision"]}))
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()
    task.cancel()
    await asyncio.sleep(0.02)
    assert entry.async_operation_lock.locked()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not entry.async_operation_lock.locked()
    assert context.get_context() == [{"role": "system", "content": "原始提示词"}]
    assert service.read("conversation_main", "context")["items"][0]["content"] == "原始提示词"


def test_missing_backup_and_invalid_clear_option_do_not_write(saved):
    database, _, _, service = saved
    snapshot = service.read("conversation_main", "context")
    for operation, failure in (({"action": "restore", "backup_id": "missing"}, KeyError), ({"action": "clear", "keep_system": "false"}, ValueError)):
        with pytest.raises(failure):
            service.mutate("conversation_main", "context", {**operation, "expected_revision": snapshot["revision"]})
        assert service.read("conversation_main", "context")["revision"] == snapshot["revision"]
    with closing(sqlite3.connect(database)) as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_data_backups'").fetchone()
