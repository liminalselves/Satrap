"""平台持久状态恢复的管理 API, CLI, 鉴权与历史保留回归"""
from unittest.mock import AsyncMock
import asyncio
import json

import pytest

from satrap.core.platform.onebot.request_registry import RequestApprovalLedger
from satrap.core.pipeline.manual_wake_store import ManualWakeStore
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.cli import cmd_platform
from satrap.cli.client import DaemonClient
from satrap.main import _build_parser


@pytest.mark.parametrize('name', ['manual_wake', 'request_ledger'])
async def test_management_recovery_requires_restored_files_and_preserves_history(tmp_path, name):
    backend = BackendManager()
    path = tmp_path / f'{name}.json'
    if name == 'manual_wake':
        initial = ManualWakeStore(path)
        initial.accept_request('bot', 'request', 'fingerprint', 'group:20', 'management')
        original = path.read_bytes()
        path.write_text('{broken', encoding='utf-8')
        store = ManualWakeStore(path)
        backend._manual_wake_store = store
    else:
        initial = RequestApprovalLedger(path)
        await initial.register('bot', '100', 'friend', 'used', user_id='321')
        await initial.occupy('bot', '100', 'friend', 'used')
        await initial.settle('bot', '100', 'friend', 'used', 'completed')
        await initial.register('bot', '100', 'friend', 'interrupted', user_id='321')
        await initial.occupy('bot', '100', 'friend', 'interrupted')
        original = path.read_bytes()
        path.write_text('{broken', encoding='utf-8')
        store = RequestApprovalLedger(path)
        backend._request_ledger = store
    assert store.degraded
    server = BackendHTTPServer(backend)
    body = json.dumps({'store': name}).encode()
    status, failed = await server._route('POST', '/api/platforms/recover-store', body)
    assert status == 409 and not failed['ok'] and store.degraded
    path.write_bytes(original)
    status, recovered = await server._route('POST', '/api/platforms/recover-store', body)
    assert status == 200 and recovered['ok'] and not store.degraded
    if name == 'manual_wake':
        assert backend._manual_wake_store is not None
        record = backend._manual_wake_store.lookup_request('request', 'bot')
        assert record is not None and record['fingerprint'] == 'fingerprint'
    else:
        ledger = backend._request_ledger
        assert ledger is not None
        with pytest.raises(LookupError):
            await ledger.occupy('bot', '100', 'friend', 'used')
        interrupted = ledger.lookup('bot', '100', 'friend', 'interrupted')
        assert interrupted is not None and interrupted['state'] == 'unknown'
        await ledger.register('bot', '100', 'friend', 'new', user_id='321')
        await ledger.occupy('bot', '100', 'friend', 'new')
    before_repeat = path.read_bytes()
    assert (await server._route('POST', '/api/platforms/recover-store', body))[0] == 200
    assert path.read_bytes() == before_repeat
    if name == 'request_ledger':
        assert backend._request_ledger is not None
        active = backend._request_ledger.lookup('bot', '100', 'friend', 'new')
        assert active is not None and active['state'] == 'executing'


def test_missing_expected_manual_wake_archive_cannot_be_discarded_by_recovery(tmp_path):
    path = tmp_path / 'wake.json'
    initial = ManualWakeStore(path)
    initial._load_archive_locked()
    initial._save_archive_locked()
    archive = initial._archive_path
    original = archive.read_bytes()
    archive.unlink()
    degraded = ManualWakeStore(path)
    assert degraded.degraded
    assert not degraded.recover()
    assert degraded.degraded
    degraded = ManualWakeStore(path)
    assert not degraded.recover()
    archive.write_bytes(original)
    assert degraded.recover()


@pytest.mark.parametrize('payload', [{}, {'store': []}, {'store': 'other'}, {'store': 'manual_wake', 'path': 'arbitrary'}])
async def test_recovery_route_rejects_unknown_stores_and_extra_fields(payload):
    server = BackendHTTPServer(BackendManager())
    status, result = await server._route('POST', '/api/platforms/recover-store', json.dumps(payload).encode())
    assert status == 400 and result['reason'] == 'invalid_store'


async def test_uninitialized_store_is_reported_unavailable():
    backend = BackendManager()
    server = BackendHTTPServer(backend)
    status, result = await server._route('POST', '/api/platforms/recover-store', b'{"store":"request_ledger"}')
    assert status == 503 and result['reason'] == 'store_unavailable'


async def test_failed_interrupted_state_write_never_clears_ledger_degradation(tmp_path, monkeypatch):
    path = tmp_path / 'ledger.json'
    original = RequestApprovalLedger(path)
    await original.register('bot', '100', 'friend', 'interrupted', user_id='321')
    await original.occupy('bot', '100', 'friend', 'interrupted')
    repaired = path.read_bytes()
    path.write_text('{broken', encoding='utf-8')
    ledger = RequestApprovalLedger(path)
    path.write_bytes(repaired)

    def failed_write():
        raise OSError('磁盘不可写')

    monkeypatch.setattr(ledger, '_save_locked', failed_write)
    backend = BackendManager()
    backend._request_ledger = ledger
    status, result = await BackendHTTPServer(backend)._route('POST', '/api/platforms/recover-store', b'{"store":"request_ledger"}')
    assert status == 409 and not result['ok']
    assert ledger.degraded
    assert json.loads((tmp_path / 'ledger.manifest.json').read_text(encoding='utf-8'))['degraded'] is not None
    with pytest.raises(LookupError):
        await ledger.occupy('bot', '100', 'friend', 'interrupted')


async def test_recovery_endpoint_requires_management_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv('SATRAP_CREDENTIALS_ROOT', str(tmp_path / 'credentials'))
    backend = BackendManager()
    recover = AsyncMock(return_value={'ok': True, 'store': 'manual_wake', 'reason': ''})
    monkeypatch.setattr(backend, 'recover_platform_store', recover)
    server = BackendHTTPServer(backend, port=0)
    await server.start()
    try:
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        body = b'{"store":"manual_wake"}'
        writer.write(f'POST /api/platforms/recover-store HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 3)
        writer.close()
        await writer.wait_closed()
        assert response.startswith(b'HTTP/1.1 401')
        recover.assert_not_awaited()
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(f'POST /api/platforms/recover-store HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {server.auth.token}\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 3)
        writer.close()
        await writer.wait_closed()
        assert response.startswith(b'HTTP/1.1 200')
        recover.assert_awaited_once_with('manual_wake')
    finally:
        await server.stop()


@pytest.mark.parametrize('name', ['manual-wake', 'request-ledger'])
def test_cli_recovery_command_uses_authenticated_daemon_route(monkeypatch, name):
    args = _build_parser().parse_args(['platform', 'recover-store', name])
    calls = []
    client = DaemonClient()
    monkeypatch.setattr(client, 'require_alive', lambda: None)

    def request(method, path, body):
        calls.append((method, path, body))
        return {'ok': True, 'store': body['store']}

    monkeypatch.setattr(client, '_request', request)
    monkeypatch.setattr(cmd_platform, 'daemon_client_from_args', lambda *_, **__: client)
    monkeypatch.setattr(cmd_platform, 'render_data', lambda *_: None)
    cmd_platform.dispatch(args)
    assert calls == [('POST', '/api/platforms/recover-store', {'store': name.replace('-', '_')})]
