"""申请动作发送前拒绝的占用释放与发送后不可重放回归"""
from unittest.mock import AsyncMock
import asyncio

import pytest

from satrap.core.platform.onebot.request_registry import RequestApprovalLedger, RequestFlagRegistry
from satrap.core.config.group_action_origin import bind_group_action_preflight, current_group_action_preflight
from satrap.core.platform.onebot.admin import AdminActionRejected, AdminActionUnconfirmed, UnsupportedAdminAction
from .test_onebot_admin import _adapter


@pytest.mark.parametrize('kind,sub_type,suspicious', [('friend', '', False), ('friend', '', True), ('group', 'add', False), ('group', 'invite', False)])
@pytest.mark.parametrize('failure', ['permission', 'permission_async', 'unsupported', 'disconnected'])
async def test_request_not_sent_keeps_credential_and_can_be_processed_once_after_fix(tmp_path, monkeypatch, kind, sub_type, suspicious, failure):
    adapter = _adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / 'ledger.json'))
    registry = adapter.request_flags
    await registry.register(kind, 'request-flag', self_id=adapter.bot_self_id, user_id='321',
        group_id='456' if kind == 'group' else '', sub_type=sub_type, event_time=100,
        details={'request_category': 'suspicious'} if suspicious else None)
    initial = (await registry.list_requests(kind, self_id=adapter.bot_self_id))['items'][0]
    row = await registry.resolve_request(kind, initial['request_id'], self_id=adapter.bot_self_id)
    bot = adapter._bot
    assert bot is not None
    action = 'set_group_add_request' if kind == 'group' else 'set_doubt_friends_add_request' if suspicious else 'set_friend_add_request'
    rpc = AsyncMock()
    monkeypatch.setattr(bot, action, rpc)

    async def invoke():
        if kind == 'group':
            await adapter.admin.handle_group_request('456', 'request-flag', sub_type, True,
                request_id=initial['request_id'], expected_revision=initial['revision'])
        else:
            await adapter.admin.handle_friend_request('request-flag', True, identity_digest=row['digest'], suspicious=suspicious)

    def denied():
        raise PermissionError('授权已撤销')

    async def denied_async():
        await asyncio.sleep(0)
        raise PermissionError('授权已撤销')

    if failure in {'permission', 'permission_async'}:
        guard = denied if failure == 'permission' else denied_async
        with bind_group_action_preflight(guard):
            with pytest.raises(PermissionError):
                await invoke()
            assert current_group_action_preflight() is guard
    elif failure == 'unsupported':
        monkeypatch.setattr(bot, action, None)
        with pytest.raises(UnsupportedAdminAction):
            await invoke()
        monkeypatch.setattr(bot, action, rpc)
    else:
        monkeypatch.setattr(adapter, '_bot', None)
        with pytest.raises(AdminActionUnconfirmed):
            await invoke()
        monkeypatch.setattr(adapter, '_bot', bot)
    rpc.assert_not_awaited()
    entry = registry.ledger.lookup(adapter.config.id, adapter.bot_self_id, kind, 'request-flag', identity_digest=row['digest'])
    assert entry is not None and entry['state'] == 'available'
    preserved = await registry.resolve_request(kind, initial['request_id'], self_id=adapter.bot_self_id)
    assert preserved['flag'] == 'request-flag' and preserved['execution_state'] == 'not_started'
    assert preserved['revision'] == initial['revision']
    await invoke()
    rpc.assert_awaited_once()
    completed = registry.ledger.lookup(adapter.config.id, adapter.bot_self_id, kind, 'request-flag', identity_digest=row['digest'])
    assert completed is not None and completed['state'] == 'completed'
    with pytest.raises((AdminActionRejected, ValueError)):
        await invoke()
    with pytest.raises(LookupError):
        await registry.occupy(kind, 'request-flag', self_id=adapter.bot_self_id,
            group_id='456' if kind == 'group' else '', sub_type=sub_type, identity_digest=row['digest'])
    assert rpc.await_count == 1


@pytest.mark.parametrize('kind', ['friend', 'group'])
async def test_request_timeout_after_dispatch_cannot_be_released_or_replayed(tmp_path, monkeypatch, kind):
    adapter = _adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / 'ledger.json'))
    registry = adapter.request_flags
    await registry.register(kind, 'flag', self_id=adapter.bot_self_id, user_id='321',
        group_id='456' if kind == 'group' else '', sub_type='invite' if kind == 'group' else '')
    assert adapter._bot is not None
    action = 'set_group_add_request' if kind == 'group' else 'set_friend_add_request'
    rpc = AsyncMock(side_effect=asyncio.TimeoutError())
    monkeypatch.setattr(adapter._bot, action, rpc)
    with pytest.raises(AdminActionUnconfirmed):
        if kind == 'group':
            await adapter.admin.handle_group_request('456', 'flag', 'invite', True)
        else:
            await adapter.admin.handle_friend_request('flag', True)
    entry = registry.ledger.lookup(adapter.config.id, adapter.bot_self_id, kind, 'flag')
    assert entry is not None and entry['state'] == 'unknown'
    assert await registry.ledger.release(adapter.config.id, adapter.bot_self_id, kind, 'flag') is None
    with pytest.raises(LookupError):
        await registry.occupy(kind, 'flag', self_id=adapter.bot_self_id,
            group_id='456' if kind == 'group' else '', sub_type='invite' if kind == 'group' else '')
    rpc.assert_awaited_once()


async def test_release_does_not_revive_superseded_request_or_modify_new_occurrence(tmp_path, monkeypatch):
    monkeypatch.setattr('satrap.core.platform.onebot.request_registry.time', lambda: 1000.0)
    ledger = RequestApprovalLedger(tmp_path / 'ledger.json')
    registry = RequestFlagRegistry('bot', ledger)
    await registry.register('friend', 'reused', self_id='100', user_id='321', event_time=100)
    first = (await registry.list_requests('friend', self_id='100'))['items'][0]
    old = await registry.resolve_request('friend', first['request_id'], self_id='100')
    await registry.occupy('friend', 'reused', self_id='100', identity_digest=old['digest'])
    await registry.register('friend', 'reused', self_id='100', user_id='321', event_time=101)
    current = (await registry.list_requests('friend', self_id='100'))['items'][0]
    latest = await registry.resolve_request('friend', current['request_id'], self_id='100')
    await registry.release('friend', 'reused', self_id='100', identity_digest=old['digest'])
    released = ledger.lookup('bot', '100', 'friend', 'reused', identity_digest=old['digest'])
    assert released is not None and released['state'] == 'superseded'
    new = ledger.lookup('bot', '100', 'friend', 'reused', identity_digest=latest['digest'])
    assert new is not None and new['state'] == 'available'


async def test_release_failure_keeps_occupation_and_never_clears_credential(tmp_path, monkeypatch):
    ledger = RequestApprovalLedger(tmp_path / 'ledger.json')
    await ledger.register('bot', '100', 'friend', 'flag', user_id='321')
    await ledger.occupy('bot', '100', 'friend', 'flag')

    def failed_save():
        raise OSError('不能写入')

    monkeypatch.setattr(ledger, '_save_locked', failed_save)
    with pytest.raises(OSError):
        await ledger.release('bot', '100', 'friend', 'flag')
    entry = ledger.lookup('bot', '100', 'friend', 'flag')
    assert entry is not None and entry['state'] == 'executing'
    with pytest.raises(LookupError):
        await ledger.occupy('bot', '100', 'friend', 'flag')


async def test_release_of_expired_occupation_requires_archived_confirmation(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('satrap.core.platform.onebot.request_registry.time', lambda: clock[0])
    ledger = RequestApprovalLedger(tmp_path / 'ledger.json')
    await ledger.register('bot', '100', 'friend', 'flag', user_id='321')
    await ledger.occupy('bot', '100', 'friend', 'flag')
    clock[0] = 1700.0
    released = await ledger.release('bot', '100', 'friend', 'flag')
    assert released is not None and released['state'] == 'expired'
    with pytest.raises(LookupError):
        await ledger.occupy('bot', '100', 'friend', 'flag')
    assert (await ledger.occupy('bot', '100', 'friend', 'flag', allow_archived=True))['state'] == 'executing'


async def test_cancellation_after_protocol_call_started_remains_unknown(tmp_path, monkeypatch):
    adapter = _adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / 'ledger.json'))
    await adapter.request_flags.register('friend', 'flag', self_id=adapter.bot_self_id, user_id='321')
    started = asyncio.Event()

    async def protocol(**_):
        started.set()
        await asyncio.Event().wait()

    assert adapter._bot is not None
    monkeypatch.setattr(adapter._bot, 'set_friend_add_request', protocol)
    pending = asyncio.create_task(adapter.admin.handle_friend_request('flag', True))
    await asyncio.wait_for(started.wait(), 3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    entry = adapter.request_flags.ledger.lookup(adapter.config.id, adapter.bot_self_id, 'friend', 'flag')
    assert entry is not None and entry['state'] == 'unknown'
