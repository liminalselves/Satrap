"""
迷你 HTTP/WS 服务器基类 (minihttp) 测试

覆盖 MiniHTTPServer 共享基础设施:
- 真实端口: JSON 响应 / CORS 预检 / query 参数解析 / 未知路由 404
- WebSocket 握手 101 + 文本帧收发
- ChatHTTPServer 路由级 (补齐聊天服务器 HTTP 层覆盖)
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import pytest
from typing import Any, cast
import json

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.utils.minihttp import MiniHTTPServer, query_param
from satrap.core.server_auth import ServerAuth
from satrap.display.recorder import DisplayRecorder, list_conversations
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.service import ChatService
from satrap.display.server import ChatHTTPServer
from satrap.core.storage import StorageLayout


class _EchoServer(MiniHTTPServer):
    """最小测试子类: 一个 echo 路由 + 一个 ws 端点"""

    def __init__(
        self,
        *,
        max_header_bytes: int = 64 * 1024,
        max_body_bytes: int = 16 * 1024 * 1024,
        header_timeout: float = 10.0,
        max_connections: int = 256,
        max_websocket_connections: int = 64,
        websocket_idle_timeout: float = 300.0,
        websocket_ping_interval: float = 30.0,
        hold_websocket: bool = False,
        log_errors: bool = False,
    ) -> None:
        """初始化可调请求限制的测试服务器"""
        auth = ServerAuth.create(
            "127.0.0.1",
            0,
            token="test-token-that-is-at-least-thirty-two-characters",
        )
        super().__init__(
            host="127.0.0.1",
            port=0,
            auth=auth,
            max_header_bytes=max_header_bytes,
            max_body_bytes=max_body_bytes,
            header_timeout=header_timeout,
            max_connections=max_connections,
            max_websocket_connections=max_websocket_connections,
            websocket_idle_timeout=websocket_idle_timeout,
            websocket_ping_interval=websocket_ping_interval,
            log_errors=log_errors,
        )
        self._hold_websocket = hold_websocket
        self._websocket_release = asyncio.Event()

    async def _route(self, method: str, path: str, body: bytes) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path.startswith("/api/echo"):
            return 200, {"ok": True, "q": self._query_param(path, "x")}
        return 404, {"error": f"unknown route: {method} {path}"}

    async def _ws_dispatch(
        self, path: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await self._ws_send(writer, {"type": "subscribed", "path": path})
        if self._hold_websocket:
            await self._websocket_release.wait()
        await self._ws_close(writer, 1000, "bye")


class _FailingDispatchServer(_EchoServer):
    """WebSocket 分发抛指定异常的测试服务器"""

    def __init__(self, error: BaseException) -> None:
        """初始化固定抛出指定异常的分发器"""
        super().__init__(log_errors=True)
        self._dispatch_error = error

    async def _ws_dispatch(
        self, path: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """抛出预置异常"""
        raise self._dispatch_error


class _MemoryWriter:
    """只记录 write 数据的最小流写入器"""

    def __init__(self) -> None:
        """初始化空响应缓冲区"""
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        """追加响应字节"""
        self.data.extend(data)


@pytest.mark.parametrize(
    ("status", "reason"),
    [(201, "Created"), (204, "No Content"), (599, "Unknown Status")],
)
def test_send_json_uses_standard_status_reason(status: int, reason: str):
    """JSON 响应状态行使用标准 reason phrase 并安全处理未知状态码"""
    server = _EchoServer()
    writer = _MemoryWriter()
    server._send_json(cast(asyncio.StreamWriter, writer), status, {"ok": True})
    assert bytes(writer.data).startswith(f"HTTP/1.1 {status} {reason}\r\n".encode())


async def _start_server(server: MiniHTTPServer) -> int:
    """启动服务器并返回实际监听端口 (port=0 系统分配)"""
    await server.start()
    assert server._server is not None
    sockets = server._server.sockets
    assert sockets is not None and len(sockets) > 0
    return int(sockets[0].getsockname()[1])


async def _raw_request(port: int, request: bytes) -> bytes:
    """建立 TCP 连接发送原始 HTTP 请求, 返回完整响应字节"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await reader.read(65536)
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return data


# ==================== 访问密码门禁 ====================


def _request_bytes(method: str, path: str, *, origin: str = "http://127.0.0.1:5173", body: bytes = b"", extra: str = "") -> bytes:
    """构造最小 HTTP 请求字节"""
    head = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: {origin}\r\n"
            f"Content-Length: {len(body)}\r\nContent-Type: application/json\r\nConnection: close\r\n{extra}\r\n")
    return head.encode() + body


def _status_code(response: bytes) -> int:
    """提取响应状态码"""
    return int(response.split(b" ", 2)[1])


async def test_dashboard_password_gate_covers_bootstrap_login_change_and_clear(tmp_path, monkeypatch):
    """免密回环引导, 设密码后按密码或 Bearer 进入, 改密即时生效, 清除后恢复, 非法存储报 500"""
    from satrap.core.auth_password import update_dashboard_password
    config = tmp_path / "config.yaml"
    monkeypatch.setattr("satrap.core.auth_password.dashboard_password_path", lambda: config)
    import threading
    from satrap.core import auth_password
    event_thread = threading.get_ident()
    original_verify = auth_password.verify_dashboard_password
    def verify_in_worker(stored, candidate):
        assert threading.get_ident() != event_thread
        return original_verify(stored, candidate)
    monkeypatch.setattr(auth_password, "verify_dashboard_password", verify_in_worker)
    original_hash = auth_password.dashboard_password_hash
    def hash_in_worker():
        assert threading.get_ident() != event_thread
        return original_hash()
    monkeypatch.setattr(auth_password, "dashboard_password_hash", hash_in_worker)
    server = _EchoServer()
    port = await _start_server(server)
    try:
        echo = "/api/echo?x=1"
        bootstrap = await _raw_request(port, _request_bytes("POST", "/auth/session"))
        assert _status_code(bootstrap) == 200
        free_cookie = "Cookie: " + bootstrap.split(b"Set-Cookie: ", 1)[1].split(b";", 1)[0].decode() + "\r\n"
        assert _status_code(await _raw_request(port, _request_bytes("GET", echo, extra=free_cookie))) == 200
        status_response = await _raw_request(port, _request_bytes("GET", "/auth/password/status"))
        assert _status_code(status_response) == 200 and b'"password_required": false' in status_response

        update_dashboard_password("Secret-123")
        status_response = await _raw_request(port, _request_bytes("GET", "/auth/password/status"))
        assert b'"password_required": true' in status_response
        assert _status_code(await _raw_request(port, _request_bytes("POST", "/auth/session"))) == 401
        assert _status_code(await _raw_request(port, _request_bytes("GET", echo))) == 401
        assert _status_code(await _raw_request(
            port, _request_bytes("POST", "/auth/password/login", body=b'{"password":"wrong-123"}'))) == 401
        login = await _raw_request(
            port, _request_bytes("POST", "/auth/password/login", body=b'{"password":"Secret-123"}'))
        assert _status_code(login) == 200
        cookie = b"Cookie: " + login.split(b"Set-Cookie: ", 1)[1].split(b";", 1)[0] + b"\r\n"
        assert _status_code(await _raw_request(port, _request_bytes("GET", echo, extra=cookie.decode()))) == 200
        assert _status_code(await _raw_request(
            port, _request_bytes("PUT", "/auth/password", extra=cookie.decode(),
                                 body=b'{"current_password":"Secret-123","new_password":"Newer-456"}'))) == 200
        relogin = await _raw_request(port, _request_bytes('POST', '/auth/password/login', body=b'{"password":"Newer-456"}'))
        assert _status_code(relogin) == 200
        assert _status_code(await _raw_request(port, _request_bytes('GET', echo, extra=cookie.decode()))) == 401
        cookie = b'Cookie: ' + relogin.split(b'Set-Cookie: ', 1)[1].split(b';', 1)[0] + b'\r\n'
        assert _status_code(await _raw_request(
            port, _request_bytes("PUT", "/auth/password", extra=cookie.decode(),
                                 body=b'{"current_password":"Secret-123","new_password":"Again-789"}'))) == 403
        assert _status_code(await _raw_request(
            port, _request_bytes("PUT", "/auth/password", extra=cookie.decode(),
                                 body=b'{"current_password":"Newer-456","new_password":null}'))) == 200
        assert b'"password_required": false' in await _raw_request(port, _request_bytes("GET", "/auth/password/status"))
        assert _status_code(await _raw_request(port, _request_bytes("POST", "/auth/session"))) == 200

        update_dashboard_password("Secret-123")
        for _ in range(5):
            await _raw_request(port, _request_bytes("POST", "/auth/password/login", body=b'{"password":"nope-000"}'))
        assert _status_code(await _raw_request(
            port, _request_bytes("POST", "/auth/password/login", body=b'{"password":"Secret-123"}'))) == 429

        config.write_text("api:\n  dashboard_password: not-a-pbkdf2-hash\n", encoding="utf-8")
        assert _status_code(await _raw_request(port, _request_bytes("GET", "/auth/password/status"))) == 500
        assert _status_code(await _raw_request(port, _request_bytes("POST", "/auth/session"))) == 401
    finally:
        await server.stop()


async def test_remembered_login_restores_after_restart_and_rejects_revoked_versions(tmp_path, monkeypatch):
    """真实 HTTP 覆盖持久登录, 重启恢复, 续期, 改密, 退出及服务范围隔离"""
    import sqlite3
    from satrap.core.auth_password import update_dashboard_password
    from satrap.core.browser_sessions import PersistentBrowserSessions

    config = tmp_path / 'config.yaml'
    monkeypatch.setattr('satrap.core.auth_password.dashboard_password_path', lambda: config)
    monkeypatch.setenv('SATRAP_CREDENTIALS_ROOT', str(tmp_path / 'credentials'))
    update_dashboard_password('Secret-123')
    server = _EchoServer()
    port = await _start_server(server)
    login = await _raw_request(port, _request_bytes('POST', '/auth/password/login', body=b'{"password":"Secret-123","remember":true}'))
    assert _status_code(login) == 200 and b'Max-Age=2592000' in login
    credential = login.split(b'Set-Cookie: ', 1)[1].split(b';', 1)[0]
    cookie = 'Cookie: ' + credential.decode() + '\r\n'
    session = credential.decode().split('=', 1)[1]
    database = tmp_path / 'credentials/browser-sessions.db'
    assert session.encode() not in database.read_bytes()
    await server.stop()

    server = _EchoServer()
    port = await _start_server(server)
    try:
        restored = await _raw_request(port, _request_bytes('POST', '/auth/session', extra=cookie))
        assert _status_code(restored) == 200 and b'Set-Cookie' not in restored
        assert _status_code(await _raw_request(port, _request_bytes('GET', '/api/echo', extra=cookie))) == 200
        assert server.auth.remembered_sessions is not None
        store = server.auth.remembered_sessions
        wrong_service = PersistentBrowserSessions(database, 'chat', store.scope)
        assert not wrong_service.validate(session, server.auth._password_version())
        wrong_deployment = PersistentBrowserSessions(database, store.namespace, 'other-deployment')
        assert not wrong_deployment.validate(session, server.auth._password_version())
        peer = ServerAuth.create('127.0.0.1', 0, token=server.auth.token, session_namespace='chat')
        assert not peer.cookie_authorized({'cookie': credential.decode()})
        shared = peer.restore_session('127.0.0.1', 'http://127.0.0.1:5173', {'cookie': credential.decode()})
        assert shared and shared['Set-Cookie'].startswith('satrap_session_chat=p_')
        peer_cookie = shared['Set-Cookie'].split(';', 1)[0]
        assert peer.cookie_authorized({'cookie': peer_cookie})
        with sqlite3.connect(database) as connection:
            connection.execute('UPDATE browser_sessions SET renewed_at=renewed_at-86401')
        renewed = await _raw_request(port, _request_bytes('POST', '/auth/session', extra=cookie))
        assert _status_code(renewed) == 200 and b'Max-Age=2592000' in renewed
        combined = 'Cookie: ' + credential.decode() + '; ' + peer_cookie + '\r\n'
        assert _status_code(await _raw_request(port, _request_bytes('POST', '/auth/logout', extra=combined))) == 200
        assert not peer.cookie_authorized({'cookie': peer_cookie})
        assert _status_code(await _raw_request(port, _request_bytes('POST', '/auth/session', extra=cookie))) == 401

        login = await _raw_request(port, _request_bytes('POST', '/auth/password/login', body=b'{"password":"Secret-123","remember":true}'))
        cookie = 'Cookie: ' + login.split(b'Set-Cookie: ', 1)[1].split(b';', 1)[0].decode() + '\r\n'
        update_dashboard_password('Changed-456')
        assert _status_code(await _raw_request(port, _request_bytes('POST', '/auth/session', extra=cookie))) == 401
    finally:
        await server.stop()


async def test_remembered_cookie_survives_real_browser_reopen(tmp_path, monkeypatch):
    """关闭真实 Chromium 进程再以同一配置启动, 无需重输密码"""
    import shutil
    from satrap.core.auth_password import update_dashboard_password

    node = shutil.which('node')
    ui = Path(__file__).resolve().parents[2] / 'satrap-ui'
    if node is None or not (ui / 'node_modules/playwright').is_dir():
        pytest.skip('未安装 Node 或 Playwright')
    monkeypatch.setattr('satrap.core.auth_password.dashboard_password_path', lambda: tmp_path / 'config.yaml')
    monkeypatch.setenv('SATRAP_CREDENTIALS_ROOT', str(tmp_path / 'credentials'))
    update_dashboard_password('Secret-123')
    script = """
const { chromium } = require('playwright');
(async () => {
  const context = await chromium.launchPersistentContext(process.argv[2], { headless: true });
  try {
    const login = process.argv[3] === 'login';
    const response = await context.request.post('http://127.0.0.1:' + process.argv[1] + (login ? '/auth/password/login' : '/auth/session'), {
      headers: { Origin: 'http://127.0.0.1:5173' }, ...(login ? { data: { password: 'Secret-123', remember: true } } : {})
    });
    console.log(JSON.stringify({ status: response.status(), cookies: (await context.cookies()).length }));
  } finally { await context.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    for phase in ('login', 'restore'):
        server = _EchoServer()
        port = await _start_server(server)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(node, '-e', script, str(port), str(tmp_path / 'browser'), phase,
                                                          cwd=ui, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
            assert process.returncode == 0, stderr.decode('utf-8', errors='replace')
            result = json.loads(stdout)
            assert result['status'] == 200 and result['cookies'] == 1
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            await server.stop()


async def test_password_login_without_origin_and_bearer_still_works(tmp_path, monkeypatch):
    """登录端点拒绝无来源请求, Bearer token 通道不受访问密码影响"""
    from satrap.core.auth_password import update_dashboard_password
    config = tmp_path / "config.yaml"
    monkeypatch.setattr("satrap.core.auth_password.dashboard_password_path", lambda: config)
    server = _EchoServer()
    port = await _start_server(server)
    try:
        update_dashboard_password("Secret-123")
        bearer = b"Authorization: Bearer test-token-that-is-at-least-thirty-two-characters\r\n"
        no_origin = (b"POST /auth/password/login HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                     b"Content-Length: 24\r\nConnection: close\r\n\r\n{\"password\":\"Secret-123\"}")
        assert _status_code(await _raw_request(port, no_origin)) == 403
        assert _status_code(await _raw_request(
            port, _request_bytes("GET", "/api/echo", extra=bearer.decode()))) == 200
        assert _status_code(await _raw_request(
            port, _request_bytes("POST", "/auth/session", extra=bearer.decode()))) == 200
    finally:
        await server.stop()


def _websocket_request(path: str = "/ws/test", key: str = "dGhlIHNhbXBsZSBub25jZQ==") -> bytes:
    """构造带鉴权的 WebSocket 升级请求"""
    return (
        f"GET {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Origin: http://localhost:5173\r\n"
        "Authorization: Bearer test-token-that-is-at-least-thirty-two-characters\r\n"
        f"Sec-WebSocket-Key: {key}\r\n\r\n"
    ).encode()


async def _read_server_websocket_frame(
    reader: asyncio.StreamReader,
) -> tuple[int, bytes]:
    """读取一个未掩码的服务端 WebSocket 帧"""
    first, second = await reader.readexactly(2)
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    return first & 0x0F, await reader.readexactly(length)


async def _wait_for_counter(server: MiniHTTPServer, name: str, value: int) -> None:
    """等待服务器连接计数达到预期值"""
    async def matches() -> None:
        """让出事件循环直至连接计数匹配"""
        while getattr(server, name) != value:
            await asyncio.sleep(0)

    await asyncio.wait_for(matches(), timeout=1.0)


async def _json_get(
    port: int,
    path: str,
    token: str | None = "test-token-that-is-at-least-thirty-two-characters",
) -> tuple[int, dict[str, object]]:
    """发送 GET 请求并解析 JSON 响应"""
    authorization = f"Authorization: Bearer {token}\r\n" if token else ""
    request = (
        f"GET {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"{authorization}"
        "Connection: close\r\n\r\n"
    ).encode()
    resp = await _raw_request(port, request)
    head, _, body = resp.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, json.loads(body)


@pytest.fixture
async def echo_server() -> Any:
    server = _EchoServer()
    port = await _start_server(server)
    yield port
    await server.stop()


@pytest.mark.asyncio
async def test_unknown_route_404(echo_server: int):
    """未知路由返回 404 JSON"""
    port = echo_server
    status, data = await _json_get(port, "/api/nope")
    assert status == 404
    assert "unknown route" in str(data["error"])


@pytest.mark.asyncio
async def test_api_requires_authentication(echo_server: int):
    """普通 API 缺少令牌时返回 401"""
    status, data = await _json_get(echo_server, "/api/echo", token=None)
    assert status == 401
    assert data["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_cors_preflight(echo_server: int):
    """OPTIONS 预检返回 204 + CORS 头"""
    port = echo_server
    request = b"OPTIONS /api/echo HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://localhost:5173\r\n\r\n"
    resp = await _raw_request(port, request)
    assert b"204 No Content" in resp
    assert b"Access-Control-Allow-Origin: http://localhost:5173" in resp
    assert b"Access-Control-Allow-Credentials: true" in resp
    assert b"Access-Control-Allow-Origin: *" not in resp
    assert b"Access-Control-Allow-Methods" in resp


@pytest.mark.asyncio
async def test_untrusted_origin_is_rejected(echo_server: int):
    """非白名单浏览器来源在鉴权前直接拒绝"""
    request = (
        b"GET /api/echo HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Origin: https://evil.example\r\n"
        b"Authorization: Bearer test-token-that-is-at-least-thirty-two-characters\r\n\r\n"
    )
    resp = await _raw_request(echo_server, request)
    assert b"403 Forbidden" in resp
    assert b"origin not allowed" in resp


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred_frame", [False, True])
async def test_websocket_handshake_and_frame(deferred_frame: bool, monkeypatch: pytest.MonkeyPatch):
    """
    握手与帧分批到达时仍完整读取文本帧和正常关闭帧

    参数:
    - deferred_frame: 是否等客户端读完握手后才发送帧
    - monkeypatch: 临时替换测试服务器的帧分发入口
    """
    server = _EchoServer()
    release = asyncio.Event()
    dispatch = server._ws_dispatch

    async def deferred_dispatch(path: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """
        等待客户端读完握手后再发送帧, 确定性覆盖 TCP 分批交付

        参数:
        - path: WebSocket 请求路径
        - reader: 客户端输入流
        - writer: 服务端输出流
        """
        await release.wait()
        await dispatch(path, reader, writer)

    if deferred_frame:
        monkeypatch.setattr(server, "_ws_dispatch", deferred_dispatch)
    port = await _start_server(server)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(_websocket_request())
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            assert b"101 Switching Protocols" in head
            release.set()
            opcode, body = await asyncio.wait_for(_read_server_websocket_frame(reader), timeout=2)
            assert opcode == 0x1
            assert json.loads(body) == {"type": "subscribed", "path": "/ws/test"}
            opcode, body = await asyncio.wait_for(_read_server_websocket_frame(reader), timeout=2)
            assert opcode == 0x8
            assert int.from_bytes(body[:2], "big") == 1000 and body[2:] == b"bye"
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        release.set()
        await server.stop()


@pytest.mark.asyncio
async def test_websocket_rejects_missing_authentication(echo_server: int):
    """WebSocket 握手必须携带有效 Bearer 或会话 Cookie"""
    request = (
        b"GET /ws/test HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Origin: http://localhost:5173\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
    )
    resp = await _raw_request(echo_server, request)
    assert b"401 Unauthorized" in resp
    assert b"101 Switching Protocols" not in resp


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["connection", "websocket"])
async def test_excess_limit_is_rejected_and_counter_released(kind: str):
    """总连接或 WebSocket 达到上限时返回 503 并在断开后释放配额"""
    if kind == "connection":
        server = _EchoServer(max_connections=1)
        counter = "_active_connections"
    else:
        server = _EchoServer(max_websocket_connections=1, hold_websocket=True)
        counter = "_active_websockets"
    port = await _start_server(server)
    first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        first_writer.write(b"GET /api/echo HTTP/1.1\r\nHost: 127.0.0.1\r\n" if kind == "connection" else _websocket_request())
        await first_writer.drain()
        if kind == "websocket":
            first_head = await first_reader.readuntil(b"\r\n\r\n")
            assert b"101 Switching Protocols" in first_head
        await _wait_for_counter(server, counter, 1)

        response = await _raw_request(
            port,
            _websocket_request(key="c2Vjb25kIHNhbXBsZSBub25jZQ=="),
        )
        assert b"503 Service Unavailable" in response
        assert b"service unavailable" in response
        assert getattr(server, counter) == 1
    finally:
        first_writer.close()
        await first_writer.wait_closed()
        await first_reader.read()
        await _wait_for_counter(server, counter, 0)
        await server.stop()


@pytest.mark.asyncio
async def test_websocket_ping_and_idle_timeout_close_inactive_client():
    """WebSocket 应发送 ping 并关闭未回传任何帧的空闲客户端"""
    server = _EchoServer(
        hold_websocket=True,
        websocket_ping_interval=0.01,
        websocket_idle_timeout=0.05,
    )
    port = await _start_server(server)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(_websocket_request())
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        assert b"101 Switching Protocols" in head

        observed_opcodes: list[int] = []
        close_payload = b""
        while 0x8 not in observed_opcodes:
            opcode, payload = await asyncio.wait_for(
                _read_server_websocket_frame(reader),
                timeout=0.5,
            )
            observed_opcodes.append(opcode)
            if opcode == 0x8:
                close_payload = payload

        assert 0x9 in observed_opcodes
        assert int.from_bytes(close_payload[:2], "big") == 1001
        assert close_payload[2:] == b"idle timeout"
        await _wait_for_counter(server, "_active_websockets", 0)
    finally:
        writer.close()
        await writer.wait_closed()
        await server.stop()


@pytest.mark.asyncio
async def test_loopback_browser_can_establish_http_only_session(echo_server: int):
    """白名单本地前端可建立 HttpOnly Cookie 会话"""
    request = (
        b"POST /auth/session HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Origin: http://localhost:5173\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    resp = await _raw_request(echo_server, request)
    assert b"200 OK" in resp
    assert b"Set-Cookie: satrap_session_api=" in resp
    assert b"test-token-that-is-at-least-thirty-two-characters" not in resp
    assert b"HttpOnly" in resp
    assert b"SameSite=Strict" in resp
    assert b"Max-Age=" in resp

    cookie = next(
        line.partition(b": ")[2].split(b";", 1)[0]
        for line in resp.split(b"\r\n")
        if line.startswith(b"Set-Cookie:")
    )
    logout = (
        b"POST /auth/logout HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Origin: http://localhost:5173\r\n"
        b"Cookie: " + cookie + b"\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    logout_resp = await _raw_request(echo_server, logout)
    assert b"200 OK" in logout_resp
    assert b"Max-Age=0" in logout_resp


@pytest.mark.asyncio
async def test_request_body_over_limit_returns_413():
    """请求体声明超过服务上限时应在读取前返回 413"""
    server = _EchoServer(max_body_bytes=8)
    port = await _start_server(server)
    try:
        request = (
            b"POST /api/echo HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer test-token-that-is-at-least-thirty-two-characters\r\n"
            b"Content-Length: 9\r\n\r\n"
        )
        response = await _raw_request(port, request)
        assert response.startswith(b"HTTP/1.1 413 ")
        assert b"request body too large" in response
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_request_headers_over_limit_return_431():
    """请求头超过服务上限时应返回 431 并关闭连接"""
    server = _EchoServer(max_header_bytes=160)
    port = await _start_server(server)
    try:
        request = (
            b"GET /api/echo HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"X-Fill: " + b"x" * 200 + b"\r\n\r\n"
        )
        response = await _raw_request(port, request)
        assert b"431 Request Header Fields Too Large" in response
        assert b"request headers too large" in response
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_slow_request_header_returns_408():
    """请求头在截止时间内未完成时应返回 408"""
    server = _EchoServer(header_timeout=0.05)
    port = await _start_server(server)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/echo HTTP/1.1\r\nHost: 127.0.0.1\r\n")
        await writer.drain()
        response = await reader.read(65536)
        assert b"408 Request Timeout" in response
        assert b"request header timeout" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


def _make_chat_server(tmp_path: Path) -> ChatHTTPServer:
    """构造 ChatHTTPServer (fake 模型配置, 不启动端口)"""
    from satrap.core.type import LLMConfig

    class _FakeModelConfig:
        def list_llm_configs(self, mask_api_key: bool = False) -> dict[str, Any]:
            return {"default": {}}

        def get_llm_config(self, name: str = "default") -> Any:
            return LLMConfig(name=name, model="m", api_key="k")

    reg = ChatPluginRegistry(state_path=tmp_path / "plugins.json")
    svc = ChatService(
        cast(ModelConfigManager, _FakeModelConfig()),
        reg,
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
        storage_layout=StorageLayout(tmp_path / "data"),
        workspace_roots=[tmp_path],
    )
    return ChatHTTPServer(svc)


@pytest.mark.asyncio
async def test_chat_server_health_route(tmp_path: Path):
    """聊天服务器 HTTP 层: /api/chat/health"""
    server = _make_chat_server(tmp_path)
    status, data = await server._route("GET", "/api/chat/health", b"")
    assert status == 200
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_chat_server_manages_history_and_trash(tmp_path: Path):
    """Chat 热管理接口应查询、回收并恢复完整历史, 会话列表读当前平台数据库"""
    server = _make_chat_server(tmp_path)
    recorder = DisplayRecorder(
        db_path=server.service._display_db_path,
        conversation_id="history-1",
    )
    recorder.save_meta("default")
    recorder.start_turn("历史测试")
    recorder.end_turn("完成")
    recorder.close()

    status, conversations = await server._route("GET", "/api/chat/conversations", b"")
    assert status == 200
    assert [item["conversation_id"] for item in conversations["conversations"]] == ["history-1"]

    status, listed = await server._route(
        "GET",
        "/api/chat/history?search=%E5%8E%86%E5%8F%B2&page=1&page_size=10",
        b"",
    )
    assert status == 200
    assert listed["total"] == 1
    assert listed["items"][0]["conversation_id"] == "history-1"

    status, deleted = await server._route(
        "POST",
        "/api/chat/history/delete",
        b'{"mode":"selected","conversation_ids":["history-1"]}',
    )
    assert status == 200
    assert deleted["deleted_count"] == 1

    status, trash = await server._route("GET", "/api/chat/history/trash", b"")
    assert status == 200
    assert trash["items"][0]["title"] == "历史测试"
    archive_id = trash["items"][0]["archive_id"]

    status, restored = await server._route(
        "POST",
        "/api/chat/history/trash/restore",
        json.dumps({"archive_id": archive_id}).encode("utf-8"),
    )
    assert status == 200
    assert restored["session_id"] == "history-1"
    assert list_conversations(str(server.service._display_db_path))[0]["conversation_id"] == "history-1"
    await server.service.close()


@pytest.mark.parametrize(("query", "expected"), [
    ("%252F", "%2F"), ("%25", "%"), ("%2B", "+"), ("+", " "),
    ("%E4%B8%AD%E6%96%87", "中文"), ("", ""), ("first&value=second", "first"),
])
def test_query_values_are_decoded_once(query, expected):
    assert query_param("/api?value=" + query, "value") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [ConnectionResetError("reset by peer"), asyncio.IncompleteReadError(b"partial", 4)],
)
async def test_websocket_normal_disconnect_is_not_error(error: BaseException, caplog: pytest.LogCaptureFixture):
    """客户端正常断连与半包截断只记 DEBUG, 且连接计数照常释放"""
    server = _FailingDispatchServer(error)
    port = await _start_server(server)
    try:
        with caplog.at_level(logging.DEBUG):
            await _raw_request(port, _websocket_request())
            await _wait_for_counter(server, "_active_websockets", 0)
    finally:
        await server.stop()

    messages = [(record.levelno, record.getMessage()) for record in caplog.records]
    assert [message for level, message in messages if "[WebSocket] 连接处理异常" in message] == []
    assert any(
        level == logging.DEBUG and f"[WebSocket] 连接已断开: {type(error).__name__}" in message
        for level, message in messages
    )


@pytest.mark.asyncio
async def test_websocket_unexpected_failure_is_still_error(caplog: pytest.LogCaptureFixture):
    """非断连异常仍记 ERROR, 不被断连分类吞掉"""
    server = _FailingDispatchServer(RuntimeError("dispatch boom"))
    port = await _start_server(server)
    try:
        with caplog.at_level(logging.DEBUG):
            await _raw_request(port, _websocket_request())
            await _wait_for_counter(server, "_active_websockets", 0)
    finally:
        await server.stop()

    messages = [(record.levelno, record.getMessage()) for record in caplog.records]
    assert any(
        level == logging.ERROR and "[WebSocket] 连接处理异常: RuntimeError" in message
        for level, message in messages
    )
    assert [message for level, message in messages if "[WebSocket] 连接已断开" in message] == []


def test_short_cookie_with_persistent_prefix_authenticates_and_revokes(tmp_path, monkeypatch):
    """短令牌碰巧以 p_ 开头时仍走短会话的认证, 恢复和撤销链路"""
    from satrap.core.browser_sessions import PersistentBrowserSessions
    from dataclasses import replace
    monkeypatch.setattr(ServerAuth, "_password_version", staticmethod(lambda: "version"))
    auth = replace(ServerAuth.create("127.0.0.1", 0, token="test-token-that-is-at-least-thirty-two-characters"),
                   remembered_sessions=PersistentBrowserSessions(tmp_path / "sessions.db", "api", "scope"))
    monkeypatch.setattr("satrap.core.server_auth.secrets.token_urlsafe", lambda size: "p_" + "a" * 41)
    cookie = auth.session_cookie().split(";", 1)[0]
    assert auth.cookie_authorized({"cookie": cookie})
    assert auth.restore_session("127.0.0.1", "http://127.0.0.1:5173", {"cookie": cookie}) == {}
    assert auth.revoke_cookie({"cookie": cookie})
    assert not auth.cookie_authorized({"cookie": cookie})
    persistent = auth.session_cookie(remember=True).split(";", 1)[0]
    assert auth.cookie_authorized({"cookie": persistent})
    assert auth.revoke_cookie({"cookie": persistent})
    assert not auth.cookie_authorized({"cookie": persistent})


async def test_no_password_login_race_retains_secure_cookie(tmp_path, monkeypatch):
    """免密竞态登录与正常 HTTPS 来源使用相同的 Secure 策略"""
    from dataclasses import replace
    monkeypatch.setattr("satrap.core.auth_password.dashboard_password_path", lambda: tmp_path / "config.yaml")
    import threading
    from satrap.core import auth_password
    event_thread = threading.get_ident()
    hash_threads = []
    original_hash = auth_password.dashboard_password_hash
    def hash_in_worker():
        hash_threads.append(threading.get_ident())
        assert hash_threads[-1] != event_thread
        return original_hash()
    monkeypatch.setattr(auth_password, "dashboard_password_hash", hash_in_worker)
    server = _EchoServer()
    origin = "https://127.0.0.1:5173"
    server.auth = replace(server.auth, allowed_origins=server.auth.allowed_origins | {origin})
    port = await _start_server(server)
    try:
        response = await _raw_request(port, _request_bytes("POST", "/auth/password/login", origin=origin, body=b'{"password":"no-longer-required"}'))
        assert _status_code(response) == 200
        assert b"; Secure" in response.split(b"\r\n\r\n")[0]
        assert len(hash_threads) >= 2
    finally:
        await server.stop()
