"""
迷你 HTTP/WS 服务器基类 (minihttp) 测试

覆盖 MiniHTTPServer 共享基础设施:
- 真实端口: JSON 响应 / CORS 预检 / query 参数解析 / 未知路由 404
- WebSocket 握手 101 + 文本帧收发
- ChatHTTPServer 路由级 (补齐聊天服务器 HTTP 层覆盖)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.utils.minihttp import MiniHTTPServer, query_param
from satrap.core.server_auth import ServerAuth
from satrap.core.storage import StorageLayout
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.recorder import DisplayRecorder, list_conversations
from satrap.display.server import ChatHTTPServer
from satrap.display.service import ChatService


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
    ) -> None:
        """
        初始化可调请求限制的测试服务器

        参数:
        - max_header_bytes: 请求头最大字节数
        - max_body_bytes: 请求体最大字节数
        - header_timeout: 请求头读取超时秒数
        - max_connections: 最大并发连接数
        - max_websocket_connections: 最大 WebSocket 并发连接数
        - websocket_idle_timeout: WebSocket 客户端空闲超时秒数
        - websocket_ping_interval: WebSocket 服务端 ping 间隔秒数
        - hold_websocket: 是否保持测试 WebSocket 不主动关闭
        """
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


class _MemoryWriter:
    """只记录 write 数据的最小流写入器"""

    def __init__(self) -> None:
        """初始化空响应缓冲区"""
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        """
        追加响应字节

        参数:
        - data: 待记录的响应字节
        """
        self.data.extend(data)


@pytest.mark.parametrize(
    ("status", "reason"),
    [(201, "Created"), (204, "No Content"), (599, "Unknown Status")],
)
def test_send_json_uses_standard_status_reason(status: int, reason: str):
    """
    JSON 响应状态行使用标准 reason phrase 并安全处理未知状态码

    参数:
    - status: 参数化 HTTP 状态码
    - reason: 预期 reason phrase
    """
    server = _EchoServer()
    writer = _MemoryWriter()
    server._send_json(cast(asyncio.StreamWriter, writer), status, {"ok": True})
    assert bytes(writer.data).startswith(f"HTTP/1.1 {status} {reason}\r\n".encode())


async def _start_server(server: MiniHTTPServer) -> int:
    """
    启动服务器并返回实际监听端口 (port=0 系统分配)

    参数:
    - server: 服务器

    返回:
    - int: 实际监听端口 (port=0 系统分配)
    """
    await server.start()
    assert server._server is not None
    sockets = server._server.sockets
    assert sockets is not None and len(sockets) > 0
    return int(sockets[0].getsockname()[1])


async def _raw_request(port: int, request: bytes) -> bytes:
    """
    建立 TCP 连接发送原始 HTTP 请求, 返回完整响应字节

    参数:
    - port: 端口
    - request: 请求对象

    返回:
    - bytes: 完整响应字节
    """
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


def _websocket_request(path: str = "/ws/test", key: str = "dGhlIHNhbXBsZSBub25jZQ==") -> bytes:
    """
    构造带鉴权的 WebSocket 升级请求

    参数:
    - path: WebSocket 路径
    - key: WebSocket 握手密钥

    返回:
    - bytes: 原始 HTTP 升级请求
    """
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
    """
    读取一个未掩码的服务端 WebSocket 帧

    参数:
    - reader: 流读取器

    返回:
    - tuple[int, bytes]: 操作码与载荷
    """
    first, second = await reader.readexactly(2)
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    return first & 0x0F, await reader.readexactly(length)


async def _wait_for_counter(server: MiniHTTPServer, name: str, value: int) -> None:
    """
    等待服务器连接计数达到预期值

    参数:
    - server: 服务器
    - name: 计数字段名称
    - value: 预期计数
    """
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
    """
    发送 GET 请求并解析 JSON 响应

    参数:
    - port: 端口
    - path: 路径
    - token: Bearer 令牌; 为 None 时不发送鉴权头

    返回:
    - tuple[int, dict[str, Any]]: 发送 GET 请求并解析 JSON 响应
    """
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
async def test_json_get_and_query_param(echo_server: int):
    """
    GET 返回 JSON, query 参数经 _query_param 解析

    参数:
    - echo_server: echo服务器
    """
    port = echo_server
    status, data = await _json_get(port, "/api/echo?x=hello%20world")
    assert status == 200
    assert data["ok"] is True
    assert data["q"] == "hello world"


@pytest.mark.asyncio
async def test_unknown_route_404(echo_server: int):
    """
    未知路由返回 404 JSON

    参数:
    - echo_server: echo服务器
    """
    port = echo_server
    status, data = await _json_get(port, "/api/nope")
    assert status == 404
    assert "unknown route" in str(data["error"])


@pytest.mark.asyncio
async def test_api_requires_authentication(echo_server: int):
    """
    普通 API 缺少令牌时返回 401

    参数:
    - echo_server: 测试服务器端口
    """
    status, data = await _json_get(echo_server, "/api/echo", token=None)
    assert status == 401
    assert data["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_cors_preflight(echo_server: int):
    """
    OPTIONS 预检返回 204 + CORS 头

    参数:
    - echo_server: echo服务器
    """
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
    """
    非白名单浏览器来源在鉴权前直接拒绝

    参数:
    - echo_server: 测试服务器端口
    """
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
async def test_websocket_handshake_and_frame(echo_server: int):
    """
    WS 握手返回 101, 随后收到文本帧与关闭帧

    参数:
    - echo_server: echo服务器
    """
    port = echo_server
    request = (
        b"GET /ws/test HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Origin: http://localhost:5173\r\n"
        b"Authorization: Bearer test-token-that-is-at-least-thirty-two-characters\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
    )
    resp = await _raw_request(port, request)
    idx = resp.find(b"\r\n\r\n")
    assert idx >= 0
    head, rest = resp[:idx], resp[idx + 4:]
    assert b"101 Switching Protocols" in head

    assert rest[0] == 0x81
    # 第一帧: 文本帧 (0x81)
    length = rest[1]
    payload = json.loads(rest[2:2 + length])
    assert payload["type"] == "subscribed"
    assert payload["path"] == "/ws/test"


@pytest.mark.asyncio
async def test_websocket_rejects_missing_authentication(echo_server: int):
    """
    WebSocket 握手必须携带有效 Bearer 或会话 Cookie

    参数:
    - echo_server: 测试服务器端口
    """
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
async def test_connection_limit_rejects_excess_client_and_releases_counter():
    """总连接达到上限时返回 503 并在断开后释放配额"""
    server = _EchoServer(max_connections=1)
    port = await _start_server(server)
    first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        first_writer.write(b"GET /api/echo HTTP/1.1\r\nHost: 127.0.0.1\r\n")
        await first_writer.drain()
        await _wait_for_counter(server, "_active_connections", 1)

        response = await _raw_request(port, _websocket_request())
        assert b"503 Service Unavailable" in response
        assert b"service unavailable" in response
        assert server._active_connections == 1
    finally:
        first_writer.close()
        await first_writer.wait_closed()
        await first_reader.read()
        await _wait_for_counter(server, "_active_connections", 0)
        await server.stop()


@pytest.mark.asyncio
async def test_websocket_limit_rejects_excess_upgrade_and_releases_counter():
    """WebSocket 达到独立上限时拒绝新升级并在断开后释放配额"""
    server = _EchoServer(
        max_websocket_connections=1,
        hold_websocket=True,
    )
    port = await _start_server(server)
    first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        first_writer.write(_websocket_request())
        await first_writer.drain()
        first_head = await first_reader.readuntil(b"\r\n\r\n")
        assert b"101 Switching Protocols" in first_head
        await _wait_for_counter(server, "_active_websockets", 1)

        response = await _raw_request(
            port,
            _websocket_request(key="c2Vjb25kIHNhbXBsZSBub25jZQ=="),
        )
        assert b"503 Service Unavailable" in response
        assert server._active_websockets == 1
    finally:
        first_writer.close()
        await first_writer.wait_closed()
        await first_reader.read()
        await _wait_for_counter(server, "_active_websockets", 0)
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
    """
    白名单本地前端可建立 HttpOnly Cookie 会话

    参数:
    - echo_server: 测试服务器端口
    """
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
        assert b"413 Content Too Large" in response
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
    """
    构造 ChatHTTPServer (fake 模型配置, 不启动端口)

    参数:
    - tmp_path: tmp路径

    返回:
    - ChatHTTPServer: 构造 ChatHTTPServer (fake 模型配置, 不启动端口)
    """
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
    """
    聊天服务器 HTTP 层: /api/chat/health

    参数:
    - tmp_path: tmp路径
    """
    server = _make_chat_server(tmp_path)
    status, data = await server._route("GET", "/api/chat/health", b"")
    assert status == 200
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_chat_server_lists_conversations_from_service_database(tmp_path: Path):
    """
    会话列表接口必须读取 ChatService 当前平台数据库

    参数:
    - tmp_path: 临时目录
    """
    server = _make_chat_server(tmp_path)
    recorder = DisplayRecorder(
        db_path=server.service._display_db_path,
        conversation_id="conversation-1",
    )
    recorder.save_meta("default")
    recorder.close()

    status, data = await server._route("GET", "/api/chat/conversations", b"")

    assert status == 200
    assert [item["conversation_id"] for item in data["conversations"]] == ["conversation-1"]
    await server.service.close()


@pytest.mark.asyncio
async def test_chat_server_manages_history_and_trash(tmp_path: Path):
    """
    Chat 热管理接口应查询、回收并恢复完整历史

    参数:
    - tmp_path: 临时目录
    """
    server = _make_chat_server(tmp_path)
    recorder = DisplayRecorder(
        db_path=server.service._display_db_path,
        conversation_id="history-1",
    )
    recorder.save_meta("default")
    recorder.start_turn("历史测试")
    recorder.end_turn("完成")
    recorder.close()

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
