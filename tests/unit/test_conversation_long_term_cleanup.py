"""档案清理与主动选择的长期数据清理共用事务, 保留范围与发送竞争边界"""
from contextlib import closing
import json
import sqlite3
import time

import pytest

from satrap.core.config.platform_message_data import PlatformMessageDataService
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.group_chat.reminders import ReminderStore, ReminderRecorder
from satrap.core.platform.scheduled import ScheduledTarget
from satrap.core.memory.scoped import ScopedMemories, MemoryError


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
        memory = repository.mutate('create', {'kind': 'group_rule', 'key': 'rule', 'title': '群约定', 'content': f'独立记忆正文{index}', 'source_message_ids': ['same']},
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
