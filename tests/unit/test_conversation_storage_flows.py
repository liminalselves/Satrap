"""业务链路场景与集中边界契约"""
from __future__ import annotations

from contextlib import closing
import sqlite3
import json
from satrap.core.config.conversation_catalog import platform_catalog, filter_records, record_facets
from satrap.core.storage.database import snapshot_session_domain, delete_session_domain_rows, restore_session_domain
from satrap.core.config.conversation_data import ConversationDataService
from satrap.core.storage.context_catalog import bind_context
from satrap.core.utils.context import ContextManager
from satrap.core.storage.layout import StorageLayout
from satrap.core.platform import registry
import asyncio
import copy
import threading
from types import SimpleNamespace
import pytest
from satrap.core.config.conversation_data import ConversationDataConflict
from satrap.core.config.conversation_runtime import manage_platform_data
from satrap.core.framework.SessionManager import SessionEntry
from satrap.core.utils.context import AsyncContextManager
from satrap.display.recorder import DisplayRecorder
import time
from satrap.core.config.platform_message_data import PlatformMessageDataService
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.group_chat.reminders import ReminderStore, ReminderRecorder
from satrap.core.platform.scheduled import ScheduledTarget
from satrap.core.memory.scoped import ScopedMemories, MemoryError
from satrap.core.config.user_directory import UserDirectoryService, UserDirectoryConflict, missing_profile_revision
from satrap.core.framework.UserManager import UserInfoStore
from satrap.core.type import UserInfo

def test_exact_owner_beats_prefix_and_archive_preserves_unrelated_context(tmp_path):
    database = tmp_path / "catalog.db"
    context = ContextManager("custom-workflow-id", db_path=str(database))
    context.add_user_message("工作流")
    bind_context(context, "root", "workflow", "检索")
    unrelated = ContextManager("root_other", db_path=str(database))
    unrelated.add_user_message("其它会话")
    bind_context(unrelated, "different", "main")
    service = ConversationDataService(database)
    records = service.catalog_records()
    assert {row["conversation_id"] for row in records} == {"root", "different"}
    root = next(row for row in records if row["conversation_id"] == "root")
    assert root["context_ids"] == ["custom-workflow-id"]
    assert root["contexts"][0]["name"] == "检索"
    archived = snapshot_session_domain(database, "root")
    assert [row["context_id"] for row in archived["context_catalog"]] == ["custom-workflow-id"]
    assert [row["content"] for row in archived["chat_history"]] == ["工作流"]
    delete_session_domain_rows(database, "root")
    assert service.read("root_other", "context")["items"][0]["content"] == "其它会话"
    restore_session_domain(database, "root", archived)
    assert service.read("custom-workflow-id", "context")["items"][0]["content"] == "工作流"
    context.close()
    unrelated.close()

def test_legacy_child_hidden_but_searchable_by_explicit_category(tmp_path):
    database = tmp_path / "catalog.db"
    context = ContextManager("sub_agent_legacy", db_path=str(database))
    context.reset_system_prompt("系统")
    service = ConversationDataService(database)
    records = service.catalog_records()
    assert filter_records(records) == []
    assert filter_records(records, filters={"kind": "legacy_child"})[0]["facets"]["source"] == ["unknown"]
    assert record_facets(records)["kind"] == [{"value": "legacy_child", "label": "旧子代理记录"}]
    context.close()

def test_read_only_legacy_catalog_never_migrates_or_fakes_time(tmp_path):
    database = tmp_path / "old.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE chat_history (conversation_id TEXT, role TEXT, content TEXT)")
        connection.execute("INSERT INTO chat_history VALUES ('unknown', 'user', '旧消息')")
    before = database.read_bytes()
    row = ConversationDataService(database).catalog_records()[0]
    assert row["last_activity_at"] is None
    assert row["facets"]["source"] == ["unknown"]
    assert database.read_bytes() == before

def test_dynamic_platform_and_adapter_labels_without_platform_type_branches(tmp_path, monkeypatch):
    class NewAdapter:
        display_name = "新增平台"

        @classmethod
        def conversation_catalog_metadata(cls, connection, route):
            return {"target": "新增频道", "channel": "自定义频道分类"}

    monkeypatch.setitem(registry._mapping, "future-type", NewAdapter)
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("future-instance")
    platforms = platform_catalog(layout, {"platforms": [{"id": "future-instance", "type": "future-type"}]})
    descriptor = next(item for item in platforms if item["id"] == "future-instance")
    assert descriptor["type_label"] == "新增平台"
    context = ContextManager("root_main", db_path=str(layout.platform_db("future-instance")))
    context.add_user_message("消息")
    bind_context(context, "root", "main")
    with closing(sqlite3.connect(context.db_path)) as connection, connection:
        connection.execute("CREATE TABLE context_sessions (context_key TEXT, session_id TEXT, user_id TEXT, platform TEXT, provider_name TEXT, session_type TEXT)")
        key = "scoped:v1:" + json.dumps(["future-instance", "edictum", "群聊", "group", "bot", "channel-1", ""])
        connection.execute("INSERT INTO context_sessions VALUES (?, 'root', '', 'future-instance', 'edictum', '群聊')", (key,))
    before = layout.platform_db("future-instance").read_bytes()
    row = ConversationDataService(context.db_path).catalog_records(descriptor)[0]
    assert row["title"] == "新增频道"
    assert row["facets"]["target"] == ["future-instance/bot/channel-1"]
    assert row["facets"]["channel"] == ["自定义频道分类"]
    assert row["facets"]["agent"] == ["群聊"]
    assert layout.platform_db("future-instance").read_bytes() == before
    context.close()

def test_legacy_user_route_never_claims_group(tmp_path):
    database = tmp_path / "catalog.db"
    context = ContextManager("root", db_path=str(database))
    context.add_user_message("消息")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE context_sessions (context_key TEXT, session_id TEXT, user_id TEXT, platform TEXT, provider_name TEXT, session_type TEXT)")
        connection.execute("INSERT INTO context_sessions VALUES ('old-key', 'root', 'user', 'adapter', 'edictum', '旧群聊')")
    row = ConversationDataService(database).catalog_records({"id": "adapter", "type": "unregistered"})[0]
    assert row["facets"]["scope"] == ["legacy_user"]
    assert "target" not in row["facets"]
    assert row["facets"]["user"] == ["adapter/user"]
    assert row["facet_labels"]["scope:legacy_user"] == "旧版用户共享"
    context.close()

def test_activity_tracks_appends_and_edits_separately(tmp_path):
    context = ContextManager("main", db_path=str(tmp_path / "catalog.db"))
    context.add_user_message("新消息")
    before = ConversationDataService(context.db_path).catalog_records()[0]["last_activity_at"]
    context.replace_messages([{"role": "user", "content": "编辑"}])
    after = ConversationDataService(context.db_path).catalog_records()[0]["last_activity_at"]
    assert before == after
    context.close()

def test_empty_shared_context_does_not_hide_main_workflow_messages(tmp_path):
    from satrap.core.framework.Base import Session

    session = Session("root", db_path=str(tmp_path / "catalog.db"))
    workflow = ContextManager("root_main", db_path=session.session_ctx.db_path)
    workflow.add_user_message("实际模型上下文")
    session._track_workflow_context("root_main", workflow)
    row = ConversationDataService(workflow.db_path).catalog_records()[0]
    assert row["context_ids"][0] == "root_main"
    assert row["contexts"][0]["kind"] == "main"
    assert next(context for context in row["contexts"] if context["id"] == "root")["kind"] == "shared"
    workflow.close()
    session.session_ctx.close()

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

def setup(tmp_path):
    now = [time.time()]
    archive = PlatformMessageStore(tmp_path / 'platform.db', 'future', clock=lambda: now[0])
    scopes = [MessageScope('future', account, 'group', group) for account, group in [('bot:/a', '群/一'), ('bot:/b', '群/一'), ('bot:/a', '群/二')]]
    memories, reminders = [], []
    store = ReminderStore(archive.database, clock=lambda: now[0])
    for index, scope in enumerate(scopes):
        archive.record(scope, ArchiveMessage('same', 'member', now[0], '请求来源'))
        archive.record(scope, ArchiveMessage('other', 'member', now[0], '另一个来源'))
        repository = ScopedMemories(archive, scope)
        memory = repository.mutate('create', {'kind': 'group_rule', 'key': 'rule', 'title': '群记忆', 'content': f'独立记忆正文{index}', 'source_message_ids': ['same']},
                                   actor='operator', operator=True, operation_id='memory')['memory']
        reminder = store.create(scope, actor='member', text='检查结果', mentions=[], source_message_id='same', operation_id='reminder', time_spec={'after_seconds': 10})['reminder']
        memories.append(memory)
        reminders.append(reminder)
    service = PlatformMessageDataService(archive)
    return archive, store, service, scopes, memories, reminders, now

def payload(scope, action='delete', **values):
    return {'self_id': scope.self_id, 'conversation_kind': scope.conversation_kind, 'chat_id': scope.chat_id,
            'action': action, 'expected_revision': 0, **({'message_ids': ['same']} if action == 'delete' else {}), **values}

def test_default_archive_delete_keeps_memory_and_reminders(tmp_path):
    archive, store, service, scopes, memories, reminders, _ = setup(tmp_path)
    result = service.operate(payload(scopes[0]))
    memory = ScopedMemories(archive, scopes[0]).get(memories[0]['memory_id'])['memory']
    assert memory['source_status'] == 'unavailable' and memory['content'] == '独立记忆正文0'
    assert store.get(scopes[0], reminders[0]['reminder_id'])['reminder']['state'] == 'scheduled'
    assert 'deleted_memory_count' not in result and 'cancelled_reminder_count' not in result

def test_selected_cleanup_is_scoped_permanent_and_clears_pending_body(tmp_path):
    archive, store, service, scopes, memories, reminders, _ = setup(tmp_path)
    repository = ScopedMemories(archive, scopes[0])
    proposal = repository.mutate('create', {'kind': 'group_rule', 'key': 'pending', 'content': '待审秘密正文', 'source_message_ids': ['same']},
                                 actor='member', current_message='same', operation_id='pending')
    unlinked = repository.mutate('create', {'kind': 'group_rule', 'key': 'keep', 'content': '其他来源记忆', 'source_message_ids': ['other']},
                                 actor='operator', operator=True, operation_id='unlinked')['memory']
    result = service.operate(payload(scopes[0], delete_memories=True, cancel_reminders=True))
    assert result['deleted_memory_count'] == result['cleared_memory_proposal_count'] == result['cancelled_reminder_count'] == 1
    assert result['sending_reminder_count'] == 0
    with pytest.raises(MemoryError):
        repository.get(memories[0]['memory_id'])
    assert repository.get(unlinked['memory_id'])['memory']['content'] == '其他来源记忆'
    assert repository.proposals()['items'][0]['state'] == 'conflicted'
    with closing(store._connect()) as connection:
        assert connection.execute('SELECT payload_json FROM memory_proposals WHERE proposal_id=?', (proposal['proposal_id'],)).fetchone()[0] == '{}'
        assert '独立记忆正文0' not in json.dumps([tuple(row) for row in connection.execute('SELECT * FROM memory_audit')], ensure_ascii=False)
    service.operate(payload(scopes[0], 'restore', backup_id=result['backup_id'], expected_revision=1))
    assert archive.get(scopes[0], 'same')['status'] == 'active'
    assert store.get(scopes[0], reminders[0]['reminder_id'])['reminder']['state'] == 'cancelled'
    with pytest.raises(MemoryError):
        repository.get(memories[0]['memory_id'])
    for scope, memory, reminder in zip(scopes[1:], memories[1:], reminders[1:]):
        assert ScopedMemories(archive, scope).get(memory['memory_id'])['memory']['state'] == 'active'
        assert store.get(scope, reminder['reminder_id'])['reminder']['state'] == 'scheduled'

def test_clear_includes_operator_records_and_does_not_cancel_claimed_send(tmp_path):
    archive, store, service, scopes, _, reminders, now = setup(tmp_path)
    scope = scopes[0]
    repository = ScopedMemories(archive, scope)
    repository.mutate('create', {'kind': 'group_rule', 'key': 'manual', 'content': '人工记忆'}, actor='operator', operator=True, operation_id='manual')
    operator = store.create(scope, actor='operator', creator_kind='operator', text='人工提醒', mentions=[], source_message_id='', operation_id='manual', time_spec={'after_seconds': 10})['reminder']
    now[0] += 10

    async def guard():
        return True

    target = ScheduledTarget(scope, None, 'policy', reminders[0]['reminder_id'], 'attempt', guard)
    assert ReminderRecorder(store, reminders[0], 'attempt').plan(target, [{'index': 0, 'kind': 'message'}])
    result = service.operate(payload(scope, 'clear', delete_memories=True, cancel_reminders=True))
    assert result['deleted_memory_count'] == 2 and result['cancelled_reminder_count'] == result['sending_reminder_count'] == 1
    assert repository.list()['items'] == []
    assert store.get(scope, operator['reminder_id'])['reminder']['state'] == 'cancelled'
    assert store.get(scope, reminders[0]['reminder_id'])['reminder']['state'] == 'sending'

def test_storage_failure_rolls_back_archive_and_long_term_cleanup(tmp_path):
    archive, store, service, scopes, memories, reminders, _ = setup(tmp_path)
    with closing(store._connect()) as connection:
        connection.execute("CREATE TRIGGER refuse_cancel BEFORE UPDATE ON group_chat_reminders BEGIN SELECT RAISE(FAIL, 'storage fault'); END")
    with pytest.raises(sqlite3.IntegrityError, match='storage fault'):
        service.operate(payload(scopes[0], delete_memories=True, cancel_reminders=True))
    assert archive.get(scopes[0], 'same')['status'] == 'active'
    assert archive.management_state(scopes[0])['revision'] == 0
    assert ScopedMemories(archive, scopes[0]).get(memories[0]['memory_id'])['memory']['content'] == '独立记忆正文0'
    assert store.get(scopes[0], reminders[0]['reminder_id'])['reminder']['state'] == 'scheduled'

@pytest.mark.parametrize('option', ['delete_memories', 'cancel_reminders'])
def test_cleanup_flags_cannot_coerce_strings_to_destructive_true(tmp_path, option):
    archive, _, service, scopes, _, _, _ = setup(tmp_path)
    with pytest.raises(ValueError, match='布尔值'):
        service.operate(payload(scopes[0], **{option: 'false'}))
    assert archive.get(scopes[0], 'same')['status'] == 'active'

def _service(tmp_path):
    database = tmp_path / "platform.db"
    store = UserInfoStore(database)
    store.upsert(UserInfo(user_id="member", user_nickname="昵称", user_platform="future", user_session=["manual", "missing"]))
    store.upsert(UserInfo(user_id="empty", user_nickname="无对话"))
    for identity in ("manual", "owned", "group"):
        context = ContextManager(identity, db_path=str(database))
        context.add_user_message(identity)
        bind_context(context, identity, "main")
        context.close()
    with closing(sqlite3.connect(database)) as connection, connection:
        for scope, owner, session in (("group_member", "member", "owned"), ("group", "", "group"), ("group_member", "unprofiled", "owned")):
            key = "scoped:v1:" + json.dumps(["future-instance", "edictum", "test", scope, "bot", "group-id", owner])
            connection.execute("INSERT INTO context_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (key, owner, "future", "edictum", "test", session, 1, 1))
    return UserDirectoryService(database, {"id": "future-instance", "type": "future", "label": "未来平台"})

def test_readonly_missing_database_and_missing_tables(tmp_path):
    service = UserDirectoryService(tmp_path / "missing" / "platform.db", {"id": "other"})
    assert service.records() == []
    assert not service.database.parent.exists()
    service.database.parent.mkdir()
    with closing(sqlite3.connect(service.database)) as connection:
        connection.execute("CREATE TABLE other (value TEXT)")
        connection.commit()
    before = service.database.read_bytes()
    assert service.records() == []
    assert service.database.read_bytes() == before

def test_directory_ownership_keeps_empty_users_and_does_not_assign_shared_groups(tmp_path):
    service = _service(tmp_path)
    before = service.database.read_bytes()
    users = {item["user_id"]: item for item in service.records()}
    assert users["empty"]["conversations"] == []
    assert not users["unprofiled"]["has_profile"]
    member = {item["conversation_id"]: item for item in users["member"]["conversations"]}
    assert member["owned"]["routed"] and not member["owned"]["manual"]
    assert member["manual"]["manual"] and not member["manual"]["routed"]
    assert not member["missing"]["exists"]
    assert "group" not in member
    assert service.database.read_bytes() == before

def test_edit_clear_nickname_cas_and_deletion_preserve_messages_routes(tmp_path):
    service = _service(tmp_path)
    user = next(item for item in service.records() if item["user_id"] == "member")
    updated = service.mutate({"action": "update", "user_id": "member", "nickname": "", "expected_revision": user["revision"]})["user"]
    assert updated["user_nickname"] == ""
    with pytest.raises(UserDirectoryConflict):
        service.mutate({"action": "delete", "user_id": "member", "expected_revision": user["revision"]})
    deleted = service.mutate({"action": "delete", "user_id": "member", "expected_revision": updated["revision"]})["user"]
    assert not deleted["has_profile"]
    assert [item["conversation_id"] for item in deleted["conversations"]] == ["owned"]
    with closing(sqlite3.connect(service.database)) as connection:
        assert connection.execute("SELECT count(*) FROM chat_history").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM context_sessions").fetchone()[0] == 3

def test_associations_validate_platform_sessions_and_leave_route_intact(tmp_path):
    service = _service(tmp_path)
    user = next(item for item in service.records() if item["user_id"] == "member")
    with pytest.raises(KeyError):
        service.mutate({"action": "associate", "user_id": "member", "session_id": "ghost", "expected_revision": user["revision"]})
    assert next(item for item in service.records() if item["user_id"] == "member")["revision"] == user["revision"]
    added = service.mutate({"action": "associate", "user_id": "member", "session_id": "owned", "expected_revision": user["revision"]})["user"]
    removed = service.mutate({"action": "dissociate", "user_id": "member", "session_id": "owned", "expected_revision": added["revision"]})["user"]
    assert next(item for item in removed["conversations"] if item["conversation_id"] == "owned")["routed"]
    assert "owned" not in removed["user_session"]

def test_create_profile_and_duplicate_conflict(tmp_path):
    service = UserDirectoryService(tmp_path / "platform.db", {"id": "new", "type": "new-type"})
    data = {"action": "create", "user_id": "id", "nickname": "", "expected_revision": missing_profile_revision()}
    result = service.mutate(data)
    assert result["user"]["user_platform"] == "new-type"
    with pytest.raises(UserDirectoryConflict):
        service.mutate(data)
    with pytest.raises(ValueError):
        service.mutate({"action": "delete", "user_id": "id"})

def test_broken_association_is_visible_and_does_not_hide_other_users(tmp_path):
    service = _service(tmp_path)
    with closing(sqlite3.connect(service.database)) as connection, connection:
        connection.execute("UPDATE user_info SET user_session='invalid-json' WHERE user_id='member'")
    users = {item["user_id"]: item for item in service.records()}
    assert users["member"]["warning"]
    assert users["member"]["conversations"][0]["conversation_id"] == "owned"
    assert users["empty"]["has_profile"]
    with pytest.raises(ValueError):
        service.mutate({"action": "associate", "user_id": "member", "session_id": "manual", "expected_revision": users["member"]["revision"]})
