from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import threading
import asyncio

import pytest

from satrap.core.group_chat.types import GroupChatError
from .test_group_chat_service import _setup, _manager_scope


@pytest.mark.parametrize('kind', ['group', 'private'])
@pytest.mark.parametrize('result', ['current', 'changed', 'unavailable'])
async def test_both_origins_use_current_agent_revision_and_keep_group_only_checks(tmp_path, monkeypatch, kind, result):
    _, adapter, original = _setup(tmp_path)
    origin = replace(original, chat_type='GroupMessage' if kind == 'group' else 'FriendMessage',
        conversation_kind=kind, conversation_id=original.chat_id, agent_route_generation=1)
    read = Mock(return_value=1 if result == 'current' else 2)
    if result == 'unavailable':
        read.side_effect = OSError('database unavailable')
    monkeypatch.setattr(adapter, 'agent_route_store', SimpleNamespace(current_revision=read))
    group_route = Mock(return_value=({}, 0))
    monkeypatch.setattr(adapter, 'group_route', group_route)
    call = adapter.group_chat_scope if kind == 'group' else adapter.group_chat_private_scope
    if result == 'current':
        scope = await call(origin)
        assert scope.conversation_kind == kind
        assert group_route.call_count == int(kind == 'group')
    else:
        with pytest.raises(GroupChatError) as error:
            await call(origin)
        assert error.value.code == ('stale_call' if result == 'changed' else 'unavailable')
        assert error.value.retryable == (result == 'unavailable')
        group_route.assert_not_called()
    read.assert_called_once_with(origin.self_id, kind, origin.chat_id)


@pytest.mark.parametrize('kind', ['group', 'private'])
async def test_route_change_while_revision_read_is_pending_denies_old_origin(tmp_path, monkeypatch, kind):
    _, adapter, original = _setup(tmp_path)
    origin = replace(original, chat_type='GroupMessage' if kind == 'group' else 'FriendMessage',
        conversation_kind=kind, conversation_id=original.chat_id, agent_route_generation=1)
    started, released = threading.Event(), threading.Event()
    revision = [1]

    def read(*_):
        started.set()
        assert released.wait(3)
        return revision[0]

    monkeypatch.setattr(adapter, 'agent_route_store', SimpleNamespace(current_revision=read))
    group_route = Mock(return_value=({}, 0))
    monkeypatch.setattr(adapter, 'group_route', group_route)
    call = adapter.group_chat_scope if kind == 'group' else adapter.group_chat_private_scope
    task = asyncio.create_task(call(origin))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        revision[0] = 2
    finally:
        released.set()
    with pytest.raises(GroupChatError) as error:
        await task
    assert error.value.code == 'stale_call'
    group_route.assert_not_called()
