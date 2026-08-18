"""迷你 HTTP/WS 服务器基类 (minihttp) 测试

覆盖 MiniHTTPServer 共享基础设施:
- 真实端口: JSON 响应 / CORS 预检 / query 参数解析 / 未知路由 404
- WebSocket 握手 101 + 文本帧收发
- ChatHTTPServer 路由级 (补齐聊天服务器 HTTP 层覆盖)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from satrap.core.utils.minihttp import MiniHTTPServer
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.server import ChatHTTPServer
from satrap.display.service import ChatService


class _EchoServer(MiniHTTPServer):
    """最小测试子类: 一个 echo 路由 + 一个 ws 端点"""

    def __init__(self) -> None:
        super().__init__(host="127.0.0.1", port=0)

    async def _route(self, method: str, path: str, body: bytes) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path.startswith("/api/echo"):
            return 200, {"ok": True, "q": self._query_param(path, "x")}
        return 404, {"error": f"unknown route: {method} {path}"}

    async def _ws_dispatch(
        self, path: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await self._ws_send(writer, {"type": "subscribed", "path": path})
        await self._ws_close(writer, 1000, "bye")


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


async def _json_get(port: int, path: str) -> tuple[int, dict[str, Any]]:
    """发送 GET 请求并解析 JSON 响应"""
    request = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode()
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
    """GET 返回 JSON, query 参数经 _query_param 解析"""
    port = echo_server
    status, data = await _json_get(port, "/api/echo?x=hello%20world")
    assert status == 200
    assert data["ok"] is True
    assert data["q"] == "hello world"


@pytest.mark.asyncio
async def test_unknown_route_404(echo_server: int):
    """未知路由返回 404 JSON"""
    port = echo_server
    status, data = await _json_get(port, "/api/nope")
    assert status == 404
    assert "unknown route" in data["error"]


@pytest.mark.asyncio
async def test_cors_preflight(echo_server: int):
    """OPTIONS 预检返回 204 + CORS 头"""
    port = echo_server
    request = b"OPTIONS /api/echo HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://localhost:5173\r\n\r\n"
    resp = await _raw_request(port, request)
    assert b"204 No Content" in resp
    assert b"Access-Control-Allow-Origin: *" in resp
    assert b"Access-Control-Allow-Methods" in resp


@pytest.mark.asyncio
async def test_websocket_handshake_and_frame(echo_server: int):
    """WS 握手返回 101, 随后收到文本帧与关闭帧"""
    port = echo_server
    request = (
        b"GET /ws/test HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
    )
    resp = await _raw_request(port, request)
    idx = resp.find(b"\r\n\r\n")
    assert idx >= 0
    head, rest = resp[:idx], resp[idx + 4:]
    assert b"101 Switching Protocols" in head

    # 第一帧: 文本帧 (0x81)
    assert rest[0] == 0x81
    length = rest[1]
    payload = json.loads(rest[2:2 + length])
    assert payload["type"] == "subscribed"
    assert payload["path"] == "/ws/test"


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
        _FakeModelConfig(),  # type: ignore[arg-type]
        reg,
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
    )
    return ChatHTTPServer(svc)


@pytest.mark.asyncio
async def test_chat_server_health_route(tmp_path: Path):
    """聊天服务器 HTTP 层: /api/chat/health"""
    server = _make_chat_server(tmp_path)
    status, data = await server._route("GET", "/api/chat/health", b"")
    assert status == 200
    assert data["ok"] is True
