"""业务链路场景与集中边界契约"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode
import asyncio
import json
import time
import pytest
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore
from satrap.core.config.group_store import GroupConfigStore
from satrap.core.group_chat.reminders import ReminderStore, ReminderError as _reminder_host_ReminderError
from satrap.core.group_chat.reminder_scheduler import ReminderScheduler
from satrap.core.group_chat.types import GroupChatError
from satrap.core.platform import PlatformConfig
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.receipt import SendReceipt
from satrap.core.platform.scheduled import execute_scheduled_segments
from satrap.core.type import SessionConfig
from support.reminder_host import setup as _reminder_host_setup, spec
import jsonschema
from satrap.core.call_context import bind_call_origin, bind_tool_workflow
from satrap.core.group_chat.reminder_host import ReminderPolicy
from satrap.core.group_chat.reminder_service import execute_reminder_tool
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform import current_adapter_manager
from satrap.core.type import LLMCallResponse
from satrap.expend.plugins.group_chat.tools import get_tools
from support.group_reply import setup as _reminder_plugin_setup, restore_manager, SCOPE
from support.group_models import session, Script, Invoker
from satrap.core.group_chat.reminder_validation import validate_reminder_mentions, validate_reminder_text
from satrap.core.group_chat.reminder_validation import ReminderError as _reminder_plugin_ReminderError
from support.reminder_host import setup as setup_host
from datetime import datetime, timezone
from satrap.core.group_chat.reminder_time import resolve_reminder_time

@pytest.mark.parametrize('change,reason', [
    ('platform', 'platform_disabled'), ('removed', 'platform_removed'), ('instance', 'instance_changed'),
    ('account', 'account_changed'), ('offline', 'platform_offline'), ('group', 'group_disabled'),
    ('plugin', 'group_chat_disabled'), ('capability', 'reminder_capability_disabled'),
    ('feature', 'reminders_disabled'), ('allowed', 'group_not_allowed'), ('agent', 'agent_disabled'),
    ('unsupported', 'scheduled_send_unsupported'),
])
def test_current_configuration_revocations_are_effective(tmp_path, change, reason):
    backend, adapter, _, configs, _, reminder = _reminder_host_setup(tmp_path)
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
    backend, adapter, manager, configs, scope, reminder = _reminder_host_setup(tmp_path)
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
    backend, previous, _, _, old_scope, old_reminder = _reminder_host_setup(tmp_path)
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
    backend, adapter, _, _, scope, reminder = _reminder_host_setup(tmp_path)
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
    backend, adapter, _, configs, _, reminder = _reminder_host_setup(tmp_path)
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
    backend, adapter, _, configs, scope, _ = _reminder_host_setup(tmp_path)
    original = adapter.group_chat_member

    async def disable_after_lookup(scope, identity):
        verified = await original(scope, identity)
        configs.update('assistant', {'plugins': [spec(reminders_enabled=False)]})
        return verified

    adapter.group_chat_member = disable_after_lookup
    with pytest.raises(_reminder_host_ReminderError, match='变化'):
        await backend.reminder_host.create(scope, {'text': '新提醒', 'after_seconds': 30, 'mention_user_ids': ['member:/甲'], 'idempotency_key': 'new'})
    assert len(ReminderStore(adapter.message_archive.database).list(scope)['items']) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['deadline', 'stop', 'clock'])
async def test_guard_rechecks_lifecycle_after_awaiting_members(tmp_path, change):
    backend, adapter, _, _, _, reminder = _reminder_host_setup(tmp_path)
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
    backend, adapter, _, configs, scope, _ = _reminder_host_setup(tmp_path)
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

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_model_create_is_persisted_once_without_immediate_native_send(tmp_path, asynchronous):
    adapter, event, _ = _reminder_plugin_setup(tmp_path)
    adapter._bot.get_group_member_info.side_effect = lambda **params: {"group_id": int(SCOPE.chat_id), "user_id": params["user_id"], "nickname": "成员"}
    manager = current_adapter_manager()
    manager.reminder_host = cast(Any, SimpleNamespace(
        backend=SimpleNamespace(get_platform_runtime=lambda platform: None),
        policy=lambda record: ReminderPolicy("ready", revision="same-policy", config={"reminders_enabled": True}),
    ))
    arguments = {"text": " 检查联调结果 ", "after_seconds": 30, "mention_user_ids": ["789", event.call_origin.actor_id]}
    assert validate_reminder_text(arguments["text"]) == arguments["text"]
    validated_mentions = validate_reminder_mentions(arguments["mention_user_ids"])
    assert validated_mentions == arguments["mention_user_ids"] and validated_mentions is not arguments["mention_user_ids"]
    script = Script([
        LLMCallResponse("tools_call", "", tool_calls=[{"id": "first", "name": "group_chat_create_reminder", "arguments": arguments}]),
        LLMCallResponse("tools_call", "", tool_calls=[{"id": "retry", "name": "group_chat_create_reminder", "arguments": arguments}]),
        LLMCallResponse("message", "提醒已安排"),
    ])
    instance = await session(tmp_path, script, asynchronous, False)
    for tool in instance._wf.tools_manager.tools.values():
        if tool.tool_name.startswith("group_chat_"):
            tool.config["reminders_enabled"] = True
    await PipelineScheduler(Invoker(instance, asynchronous)).execute(event)
    reminders = ReminderStore(adapter.message_archive.database).list(SCOPE, actor=event.call_origin.actor_id)["items"]
    assert len(reminders) == 1 and reminders[0]["state"] == "scheduled"
    assert reminders[0]["text"] == "检查联调结果"
    assert reminders[0]["mention_user_ids"] == arguments["mention_user_ids"]
    assert reminders[0]["source_message_id"] == event.call_origin.source_message_id
    assert reminders[0]["source_agent"]["session_id"] == instance.session_id
    repository = ReminderStore(adapter.message_archive.database)
    original_mentions = list(arguments["mention_user_ids"])
    direct = repository.create(SCOPE, actor=event.call_origin.actor_id, text=arguments["text"], mentions=arguments["mention_user_ids"],
                               source_message_id=event.call_origin.source_message_id, operation_id="direct-raw", time_spec={"after_seconds": 30})["reminder"]
    arguments["mention_user_ids"].append("321")
    persisted = repository.get(SCOPE, direct["reminder_id"])["reminder"]
    assert persisted["text"] == "检查联调结果" and persisted["mention_user_ids"] == original_mentions
    adapter._bot.send_group_msg.assert_not_awaited()

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_source_owner_main_workflow_and_unknown_parameters_are_mechanical(tmp_path):
    adapter, event, _ = _reminder_plugin_setup(tmp_path)
    workflow = object()
    instance: Any = SimpleNamespace(_wf=workflow)
    with bind_call_origin(event.call_origin), bind_tool_workflow(workflow):
        with bind_tool_workflow(object()):
            child = await execute_reminder_tool("group_chat_create_reminder", {"text": "提醒", "after_seconds": 10},
                                                session=instance, config={"reminders_enabled": True}, authorize=lambda group: None)
        assert child["error"]["code"] == "read_only_workflow"
        spoof = await execute_reminder_tool("group_chat_create_reminder", {"text": "提醒", "after_seconds": 10, "chat_id": "other"},
                                            session=instance, config={"reminders_enabled": True}, authorize=lambda group: None)
        assert spoof["error"]["code"] == "invalid_argument"
        repository = ReminderStore(adapter.message_archive.database)
        other = repository.create(SCOPE, actor="other", text="私有正文", mentions=[], source_message_id="m", operation_id="other", time_spec={"after_seconds": 30})["reminder"]
        result = await execute_reminder_tool("group_chat_get_reminder", {"reminder_id": other["reminder_id"]},
                                             session=instance, config={}, authorize=lambda group: None)
        assert result["error"]["code"] == "not_found" and "私有正文" not in str(result)

@pytest.mark.usefixtures('restore_manager')
def test_create_schema_enforces_exclusive_time_and_recovery_is_manual():
    instance = SimpleNamespace()
    tool = next(item for item in get_tools(instance, {}) if item.tool_name == "group_chat_create_reminder")
    schema = tool.get_tool_defined()["function"]["parameters"]
    jsonschema.validate({"text": "提醒", "after_seconds": 10}, schema)
    jsonschema.validate({"text": "提醒", "due_at": "2026-10-05T09:00:00"}, schema)
    for values in ({"text": "提醒"}, {"text": "提醒", "due_at": "2026-10-05T09:00:00", "after_seconds": 10}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(values, schema)
    assert tool.recovery_policy == "manual"

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_source_deleted_during_member_lookup_never_creates_reminder(tmp_path):
    adapter, event, _ = _reminder_plugin_setup(tmp_path)
    manager = current_adapter_manager()
    manager.reminder_host = cast(Any, SimpleNamespace(
        backend=SimpleNamespace(get_platform_runtime=lambda platform: None),
        policy=lambda record: ReminderPolicy("ready", revision="same-policy", config={"reminders_enabled": True}),
    ))

    def remove_source(**params):
        adapter.message_archive.delete(SCOPE, expected_revision=0, message_ids=[event.call_origin.source_message_id])
        return {"group_id": int(SCOPE.chat_id), "user_id": params["user_id"], "nickname": "成员"}

    adapter._bot.get_group_member_info.side_effect = remove_source
    workflow = object()
    instance: Any = SimpleNamespace(_wf=workflow, session_id="source-session")
    with bind_call_origin(event.call_origin), bind_tool_workflow(workflow):
        result = await execute_reminder_tool("group_chat_create_reminder", {"text": "提醒", "after_seconds": 30},
                                             session=instance, config={"reminders_enabled": True}, authorize=lambda group: None)
    assert result["error"]["code"] == "invalid_source"
    assert ReminderStore(adapter.message_archive.database).list(SCOPE)["items"] == []
    adapter._bot.send_group_msg.assert_not_awaited()

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.parametrize('mentions', [None, '123', ['all'], ['a', 'a'], [1], [[]], [''], ['a\nb'], ['x' * 257], [str(i) for i in range(11)], ['bad\nmember']])
async def test_invalid_mentions_are_rejected_across_creation_chain_without_network_or_storage(tmp_path, mentions):
    adapter, event, _ = _reminder_plugin_setup(tmp_path / 'tool')
    workflow = object()
    manager = current_adapter_manager()
    assert manager is not None
    manager.reminder_host = cast(Any, SimpleNamespace(backend=SimpleNamespace(get_platform_runtime=lambda _: None),
        policy=lambda _: ReminderPolicy('ready', revision='current', config={})))
    adapter._bot.get_group_member_info = AsyncMock()
    with bind_call_origin(event.call_origin), bind_tool_workflow(workflow):
        result = await execute_reminder_tool('group_chat_create_reminder',
            {'text': '提醒', 'after_seconds': 10, 'mention_user_ids': mentions},
            session=cast(Any, SimpleNamespace(_wf=workflow)), config={'reminders_enabled': True}, authorize=lambda _: None)
    assert result['error']['code'] == 'invalid_argument'
    adapter._bot.get_group_member_info.assert_not_awaited()
    assert adapter.message_archive is not None
    assert ReminderStore(adapter.message_archive.database).list(SCOPE, actor=event.call_origin.actor_id)['items'] == []

    backend, manual_adapter, _, _, scope, _ = setup_host(tmp_path / 'operator')
    manual_adapter.group_chat_member = AsyncMock()
    with pytest.raises(_reminder_plugin_ReminderError):
        await backend.reminder_host.create(scope, {'text': '提醒', 'after_seconds': 10, 'mention_user_ids': mentions, 'idempotency_key': 'invalid'})
    manual_adapter.group_chat_member.assert_not_awaited()
    assert manual_adapter.message_archive is not None
    with pytest.raises(_reminder_plugin_ReminderError):
        ReminderStore(manual_adapter.message_archive.database).create(scope, actor='member', text='提醒', mentions=mentions,
            source_message_id='source', operation_id='invalid-direct', time_spec={'after_seconds': 10})

def test_relative_time_is_based_on_acceptance_not_platform_time():
    assert resolve_reminder_time({"after_seconds": 10}, 1000) == 1010
    assert resolve_reminder_time({"after_seconds": 31536000}, 1000) == 31537000

@pytest.mark.parametrize("values", [{}, {"due_at": "2026-10-04", "after_seconds": 10}, {"after_seconds": True},
                                      {"after_seconds": 9}, {"after_seconds": 31536001}, {"due_at": "2026-10-04"}])
def test_invalid_or_ambiguous_choices_rejected(values):
    with pytest.raises(ValueError):
        resolve_reminder_time(values, 1000)

def test_explicit_offsets_and_local_time_return_utc_deadlines():
    value = "2026-10-04T09:00:00+08:00"
    expected = datetime.fromisoformat(value).timestamp()
    assert resolve_reminder_time({"due_at": value}, expected - 60) == expected
    local = datetime(2026, 10, 4, 9, 0)
    expected_local = local.astimezone().timestamp()
    assert resolve_reminder_time({"due_at": "2026-10-04T09:00:00"}, expected_local - 60) == expected_local

@pytest.mark.parametrize("case", ["fold", "gap"])
def test_local_dst_ambiguity_is_refused(monkeypatch, case):
    """本地时间落入夏令时重叠 (歧义) 或空洞 (不存在) 时均拒绝并给出明确错误"""
    if case == "fold":
        fields = (2026, 11, 1, 1, 30, 0)
        first = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc).timestamp()
        second = first + 3600
        monkeypatch.setattr(time, "mktime", lambda value: first if value[-1] == 1 else second)
        monkeypatch.setattr(time, "localtime", lambda value: time.struct_time((*fields, 0, 0, int(value == first))))
        with pytest.raises(ValueError, match="夏令时歧义"):
            resolve_reminder_time({"due_at": "2026-11-01T01:30:00"}, first - 60)
    else:
        monkeypatch.setattr(time, "mktime", lambda value: 12345)
        monkeypatch.setattr(time, "localtime", lambda value: time.struct_time((2026, 3, 8, 3, 30, 0, 0, 0, 1)))
        with pytest.raises(ValueError, match="不存在"):
            resolve_reminder_time({"due_at": "2026-03-08T02:30:00"}, 1000)
