"""通过真实命名 Agent 与平台路由解析提醒授权, 不复用旧会话权限"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode
import asyncio
import json
import time

import pytest

from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.config.platform_identity import platform_instance_id
from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore, ArchiveMessage
from satrap.core.config.group_store import GroupConfigStore
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.providers import EdictumProvider
from satrap.core.group_chat.reminders import ReminderStore, ReminderError
from satrap.core.group_chat.reminder_scheduler import ReminderScheduler
from satrap.core.group_chat.types import GroupChatError
from satrap.core.platform import PlatformAdapterManager, PlatformConfig
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.receipt import SendReceipt
from satrap.core.platform.scheduled import execute_scheduled_segments
from satrap.core.type import SessionConfig
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.registry import create_default_edictum_type_registry
from .test_group_chat_service import _FutureAdapter


class ReminderAdapter(_FutureAdapter):
    """支持非数字账号和成员 ID 的虚拟后台平台"""

    supports_scheduled_group_send = True

    def get_client(self):
        return self.client

    def group_chat_group_visible(self, group):
        return self.visible


def spec(enabled=True, **config):
    return {'name': 'group_chat', 'enabled': enabled, 'config': {'reminders_enabled': True, **config},
            'capabilities': {'tools': {'group_chat_create_reminder': True}}}


def setup(tmp_path):
    platform = {'id': 'future', 'type': 'future', 'session_provider': 'edictum', 'session_type': 'assistant'}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path / 'data'), platforms=[platform]))
    adapter = ReminderAdapter(PlatformConfig(**platform, instance_id=platform_instance_id(platform)))
    adapter.started = True
    adapter.client_self_id = 'robot:/一'
    adapter.client = object()
    adapter.visible = True
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters['future'] = adapter
    backend._running = True
    backend._reminder_scheduler = SimpleNamespace(_stopping=False, _clock_unstable=False)
    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / 'edictum.json')
    configs.create('assistant', {'edictum_type': 'async_simple', 'model_name': 'base', 'plugins': [spec()]})
    configs.create('without-reminders', {'edictum_type': 'async_simple', 'model_name': 'base', 'plugins': []})
    provider = EdictumProvider(configs, registry, default_checkpoint_db=str(backend.platform_db_path('future')))
    manager = SessionManager(db_path=backend.platform_db_path('future'), platform_id='future')
    manager.register_provider(provider)
    backend._platform_runtimes['future'] = (manager, None)
    scope = MessageScope('future', adapter.client_self_id, 'group', 'room:/群')
    adapter.message_archive = PlatformMessageStore(backend.platform_db_path('future'), 'future')
    adapter.message_archive.record(scope, ArchiveMessage('source', 'member:/甲', time.time(), '提醒请求'))
    reminder = ReminderStore(adapter.message_archive.database).create(scope, actor='member:/甲', text='检查结果', mentions=['member:/甲'],
        source_message_id='source', operation_id='create', time_spec={'after_seconds': 10},
        source_agent={'instance_id': adapter.config.instance_id})['reminder']
    return backend, adapter, manager, configs, scope, reminder


@pytest.mark.parametrize('change,reason', [
    ('platform', 'platform_disabled'), ('removed', 'platform_removed'), ('instance', 'instance_changed'),
    ('account', 'account_changed'), ('offline', 'platform_offline'), ('group', 'group_disabled'),
    ('plugin', 'group_chat_disabled'), ('capability', 'reminder_capability_disabled'),
    ('feature', 'reminders_disabled'), ('allowed', 'group_not_allowed'), ('agent', 'agent_disabled'),
    ('unsupported', 'scheduled_send_unsupported'),
])
def test_current_configuration_revocations_are_effective(tmp_path, change, reason):
    backend, adapter, _, configs, _, reminder = setup(tmp_path)
    host = backend.reminder_host
    assert host.policy(reminder).state == 'ready'
    if change == 'platform':
        adapter.config.enable = False
    elif change == 'removed':
        backend.config.platforms = []
    elif change == 'instance':
        adapter.config.instance_id = 'f' * 32
    elif change == 'account':
        adapter.client_self_id = 'robot:/二'
    elif change == 'offline':
        adapter.client = None
    elif change == 'group':
        adapter.visible = False
    elif change == 'unsupported':
        adapter.supports_scheduled_group_send = False
    elif change == 'agent':
        configs.set_enabled('assistant', False)
    else:
        plugin = spec(change != 'plugin', **({'reminders_enabled': False} if change == 'feature' else {'allowed_groups': 'another-room'} if change == 'allowed' else {}))
        if change == 'capability':
            plugin['capabilities']['tools']['group_chat_create_reminder'] = False
        configs.update('assistant', {'plugins': [plugin]})
    policy = host.policy(reminder)
    assert policy.reason == reason
    assert policy.state == ('waiting' if change == 'offline' else 'paused')


def test_source_overrides_expire_on_route_change_and_named_config_is_reloaded(tmp_path):
    backend, adapter, manager, configs, scope, reminder = setup(tmp_path)
    manager.store.upsert(SessionConfig(session_id='source-session', provider_name='edictum', session_type_name='assistant', session_config={'plugins': [spec(False)]}))
    reminder['source_agent'].update(session_id='source-session', config_name='assistant', route_generation=0)
    assert backend.reminder_host.policy(reminder).reason == 'group_chat_disabled'
    adapter._agent_route_memory[(scope.self_id, 'group', scope.chat_id)] = ((), 1)
    assert backend.reminder_host.policy(reminder).state == 'ready'
    adapter.config.session_bindings = {'group': {'mode': 'value', 'provider': 'edictum', 'config_name': 'without-reminders'}}
    assert backend.reminder_host.policy(reminder).reason == 'group_chat_disabled'
    configs.update('without-reminders', {'plugins': [spec()]})
    assert backend.reminder_host.policy(reminder).state == 'ready'


@pytest.mark.asyncio
@pytest.mark.parametrize('group_enabled', [True, False])
async def test_onebot_restart_waits_for_snapshot_then_honors_actual_group_policy(tmp_path, group_enabled):
    backend, previous, _, _, old_scope, old_reminder = setup(tmp_path)
    database = backend.platform_db_path('future')
    store = ReminderStore(database)
    store.change(old_scope, old_reminder['reminder_id'], 'cancel', 1, actor=old_reminder['creator_user_id'])
    scope = MessageScope('future', '10000', 'group', '456')
    adapter = OneBotAdapter(PlatformConfig(
        id='future', type='onebot', instance_id=previous.config.instance_id,
        session_provider='edictum', session_type='assistant',
        settings={'group_whitelist': ['456' if group_enabled else '999']},
    ))
    adapter._bot = AsyncMock()
    adapter._bot.send_group_msg.return_value = {'message_id': 9876}
    adapter._running = adapter.started = True
    adapter._meta_hooked = True
    adapter.bot_self_id = adapter.client_self_id = scope.self_id
    adapter._archive_sent_segments = Mock()
    adapter.message_archive = PlatformMessageStore(database, 'future')
    adapter.set_group_access_store(GroupConfigStore(database))
    backend._adapter_mgr._adapters['future'] = adapter
    reminder = store.create(
        scope, actor='authenticated_operator', creator_kind='operator', text='重启提醒', mentions=[],
        source_message_id='', operation_id='restart', time_spec={'after_seconds': 30},
        source_agent={'instance_id': adapter.config.instance_id},
    )['reminder']
    scheduler = ReminderScheduler(lambda: [store], backend.reminder_host.resolve)
    backend._reminder_scheduler = scheduler
    try:
        assert adapter.group_route(scope.chat_id)[1] == -1
        assert not adapter.group_chat_group_visible(scope.chat_id)
        policy = backend.reminder_host.policy(reminder)
        assert (policy.state, policy.reason) == ('waiting', 'group_state_pending')
        await scheduler.tick()
        current = store.get(scope, reminder['reminder_id'])['reminder']
        assert current['state'] == 'scheduled' and current['revision'] == 1
        adapter._bot.send_group_msg.assert_not_awaited()

        await adapter._ensure_group_access(scope.self_id)
        policy = backend.reminder_host.policy(reminder)
        assert (policy.state, policy.reason) == ('waiting', 'platform_offline')
        await adapter._handle_meta({'self_id': 10000, 'meta_event_type': 'lifecycle', 'sub_type': 'connect'})
        if not group_enabled:
            policy = backend.reminder_host.policy(reminder)
            assert (policy.state, policy.reason) == ('paused', 'group_disabled')
            await scheduler.tick()
            assert store.get(scope, reminder['reminder_id'])['reminder']['state'] == 'paused'
            adapter._bot.send_group_msg.assert_not_awaited()
            return
        assert backend.reminder_host.policy(reminder).state == 'ready'
        with store._connect() as connection:
            connection.execute('UPDATE group_chat_reminders SET due_at=? WHERE reminder_id=?',
                               (time.time() - 1, reminder['reminder_id']))
        await scheduler.tick()
        await asyncio.gather(*list(scheduler._workers.values()))
        await asyncio.sleep(0)
        current = store.get(scope, reminder['reminder_id'])['reminder']
        assert current['state'] == 'sent' and current['delivery']['message_ids'] == ['9876']
        await scheduler.tick()
        adapter._bot.send_group_msg.assert_awaited_once()
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('retryable', [True, False])
async def test_temporary_member_lookup_failure_waits_and_revalidates_before_delivery(tmp_path, monkeypatch, retryable):
    backend, adapter, _, _, scope, reminder = setup(tmp_path)
    now = [reminder['due_timestamp'] + 1]
    monkeypatch.setattr('satrap.core.group_chat.reminder_host.time', SimpleNamespace(time=lambda: now[0]))
    store = ReminderStore(adapter.message_archive.database, clock=lambda: now[0])
    original = adapter.group_chat_member
    adapter.group_chat_member = AsyncMock(side_effect=GroupChatError('unavailable', '成员查询不可用', retryable=retryable))
    sender = AsyncMock(return_value=SendReceipt('success', ('confirmed-message',)))

    async def send(target, chain, recorder):
        return await execute_scheduled_segments(target, recorder, [{'index': 0}], [sender])

    adapter.group_chat_send_scheduled = send
    scheduler = ReminderScheduler(lambda: [store], backend.reminder_host.resolve, clock=lambda: now[0], monotonic=lambda: now[0])
    backend._reminder_scheduler = scheduler
    try:
        await scheduler.tick()
        await asyncio.gather(*list(scheduler._workers.values()))
        await asyncio.sleep(0)
        current = store.get(scope, reminder['reminder_id'])['reminder']
        assert current['state'] == ('waiting_delivery' if retryable else 'paused')
        assert current['reason'] == ('member_unavailable' if retryable else 'member_unverified')
        sender.assert_not_awaited()
        with store._connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM group_chat_reminder_attempts').fetchone()[0] == 0
        adapter.group_chat_member = original
        now[0] += 30
        await scheduler.tick()
        await asyncio.gather(*list(scheduler._workers.values()))
        await asyncio.sleep(0)
        current = store.get(scope, reminder['reminder_id'])['reminder']
        if retryable:
            assert current['state'] == 'sent' and current['delivery']['message_ids'] == ['confirmed-message']
            await scheduler.tick()
            sender.assert_awaited_once()
        else:
            assert current['state'] == 'paused'
            sender.assert_not_awaited()
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_guard_rebuilds_members_config_connection_and_deadline(tmp_path):
    backend, adapter, _, configs, _, reminder = setup(tmp_path)
    reminder['due_timestamp'] = time.time() - 1
    delivery = await backend.reminder_host.resolve(reminder, True)
    assert delivery.state == 'ready' and await delivery.target.guard()
    frozen_client = adapter.client
    adapter.client = object()
    assert not await delivery.target.guard()
    adapter.client = frozen_client
    configs.update('assistant', {'plugins': [spec(reminders_enabled=False)]})
    assert not await delivery.target.guard()
    configs.update('assistant', {'plugins': [spec()]})
    reminder['due_timestamp'] = time.time() - 601
    assert not await delivery.target.guard()


@pytest.mark.asyncio
async def test_creation_rechecks_config_after_member_lookup(tmp_path):
    backend, adapter, _, configs, scope, _ = setup(tmp_path)
    original = adapter.group_chat_member

    async def disable_after_lookup(scope, identity):
        verified = await original(scope, identity)
        configs.update('assistant', {'plugins': [spec(reminders_enabled=False)]})
        return verified

    adapter.group_chat_member = disable_after_lookup
    with pytest.raises(ReminderError, match='变化'):
        await backend.reminder_host.create(scope, {'text': '新提醒', 'after_seconds': 30, 'mention_user_ids': ['member:/甲'], 'idempotency_key': 'new'})
    assert len(ReminderStore(adapter.message_archive.database).list(scope)['items']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['deadline', 'stop', 'clock'])
async def test_guard_rechecks_lifecycle_after_awaiting_members(tmp_path, change):
    backend, adapter, _, _, _, reminder = setup(tmp_path)
    reminder['due_timestamp'] = time.time() - 1
    delivery = await backend.reminder_host.resolve(reminder, True)
    original = adapter.group_chat_member

    async def changed_during_lookup(scope, identity):
        verified = await original(scope, identity)
        if change == 'deadline':
            reminder['due_timestamp'] = time.time() - 601
        elif change == 'stop':
            backend._running = False
        else:
            backend._reminder_scheduler._clock_unstable = True
        return verified

    adapter.group_chat_member = changed_during_lookup
    assert not await delivery.target.guard()


@pytest.mark.asyncio
async def test_live_http_creation_and_resume_pass_through_auth_and_real_policy(tmp_path):
    backend, adapter, _, configs, scope, _ = setup(tmp_path)
    server = BackendHTTPServer(backend, port=0)
    query = urlencode({'self_id': scope.self_id, 'chat_id': scope.chat_id, 'conversation_kind': 'group'})
    root = '/api/platforms/future/group-chat/reminders?' + query

    async def request(path, payload=None, authorized=True):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else b''
        method = 'POST' if payload is not None else 'GET'
        headers = (f'{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                   f'Authorization: Bearer {server.auth.token if authorized else "invalid"}\r\n'
                   f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode('utf-8')
        reader = asyncio.StreamReader()
        reader.feed_data(headers + body)
        reader.feed_eof()
        output = bytearray()
        writer = SimpleNamespace(write=output.extend, drain=AsyncMock(), close=Mock())
        await server._handle_connection(reader, writer)
        header, content = bytes(output).split(b'\r\n\r\n', 1)
        return int(header.split()[1]), json.loads(content.decode('utf-8'))

    values = {'text': '人工安排的提醒', 'after_seconds': 30, 'mention_user_ids': ['member:/甲'], 'idempotency_key': 'http-create'}
    assert (await request(root, values, authorized=False))[0] == 401
    status, created = await request(root, values)
    assert status == 200 and created['status'] == 'created'
    identity = created['reminder']['reminder_id']
    assert (await request(root, values))[1]['reminder']['reminder_id'] == identity
    repository = ReminderStore(adapter.message_archive.database)
    assert repository.transition(identity, ('scheduled',), 'paused', 'reminders_disabled')
    path = f'/api/platforms/future/group-chat/reminders/{identity}/resume?{query}'
    assert (await request(path, {'expected_revision': 99}))[0] == 409
    paused = repository.get(scope, identity)['reminder']
    configs.update('assistant', {'plugins': [spec(reminders_enabled=False)]})
    assert (await request(path, {'expected_revision': paused['revision']}))[0] == 409
    assert repository.get(scope, identity)['reminder']['state'] == 'paused'
    configs.update('assistant', {'plugins': [spec()]})
    status, resumed = await request(path, {'expected_revision': paused['revision']})
    assert status == 200 and resumed['reminder']['state'] == 'scheduled'
    assert resumed['reminder']['due_timestamp'] == created['reminder']['due_timestamp']
    assert (await request(root))[0] == 200
