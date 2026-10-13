from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from satrap.core.group_chat.reminder_validation import validate_reminder_mentions, validate_reminder_text, ReminderError
from satrap.core.group_chat.reminders import ReminderStore
from satrap.core.call_context import bind_call_origin, bind_tool_workflow
from satrap.core.group_chat.reminder_service import execute_reminder_tool
from satrap.core.group_chat.reminder_host import ReminderPolicy
from satrap.core.platform import current_adapter_manager
from .test_group_chat_reply import setup, restore_manager, SCOPE
from .test_reminder_host import setup as setup_host


@pytest.mark.parametrize('mentions', [None, '123', ['all'], ['a', 'a'], [1], [[]], [''], ['a\nb'], ['x' * 257], [str(i) for i in range(11)]])
def test_invalid_mentions_are_rejected_with_stable_error(mentions):
    with pytest.raises(ReminderError) as error:
        validate_reminder_mentions(mentions)
    assert error.value.code == 'invalid_argument'


def test_validation_preserves_order_and_raw_text_without_mutating_input():
    original = ['成员:/甲', '成员:/乙']
    copied = validate_reminder_mentions(original)
    assert copied == original and copied is not original
    assert validate_reminder_text(' 提醒正文 ') == ' 提醒正文 '
    assert validate_reminder_mentions([]) == []


@pytest.mark.parametrize('member', ['x' * 257, 'bad\nmember'])
async def test_invalid_members_never_reach_member_lookup_or_storage_at_any_entry(tmp_path, member):
    adapter, event, _ = setup(tmp_path / 'tool')
    workflow = object()
    manager = current_adapter_manager()
    assert manager is not None
    manager.reminder_host = SimpleNamespace(backend=SimpleNamespace(get_platform_runtime=lambda _: None),
        policy=lambda _: ReminderPolicy('ready', revision='current', config={}))
    adapter._bot.get_group_member_info = AsyncMock()
    with bind_call_origin(event.call_origin), bind_tool_workflow(workflow):
        result = await execute_reminder_tool('group_chat_create_reminder',
            {'text': '提醒', 'after_seconds': 10, 'mention_user_ids': [member]},
            session=SimpleNamespace(_wf=workflow), config={'reminders_enabled': True}, authorize=lambda _: None)
    assert result['error']['code'] == 'invalid_argument'
    adapter._bot.get_group_member_info.assert_not_awaited()
    assert adapter.message_archive is not None
    assert ReminderStore(adapter.message_archive.database).list(SCOPE, actor=event.call_origin.actor_id)['items'] == []

    backend, manual_adapter, _, _, scope, _ = setup_host(tmp_path / 'operator')
    manual_adapter.group_chat_member = AsyncMock()
    with pytest.raises(ReminderError, match='提及成员'):
        await backend.reminder_host.create(scope, {'text': '提醒', 'after_seconds': 10, 'mention_user_ids': [member], 'idempotency_key': 'invalid'})
    manual_adapter.group_chat_member.assert_not_awaited()
    assert manual_adapter.message_archive is not None
    with pytest.raises(ReminderError):
        ReminderStore(manual_adapter.message_archive.database).create(scope, actor='member', text='提醒', mentions=[member],
            source_message_id='source', operation_id='invalid-direct', time_spec={'after_seconds': 10})
