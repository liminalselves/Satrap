"""实际临时 HTTP 端口的任务接受, 鉴权, 断线隔离和查询契约"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.utils.application_progress import progress_instance, progress_phase
from satrap.core.utils.minihttp import MiniHTTPServer
from satrap.display.server import ChatHTTPServer


_TOKEN = "test-progress-api-token-at-least-thirty-two-characters"


async def _request(server: MiniHTTPServer, method: str, path: str, payload: dict | None = None, *, authorized: bool = True) -> tuple[int, dict]:
    """请求隔离监听端口, 不读取真实后端令牌或访问正在运行的服务"""
    listener = server._server
    if listener is None:
        raise AssertionError("测试 HTTP 服务未启动")
    port = listener.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else b""
    auth = f"Authorization: Bearer {_TOKEN}\r\n" if authorized else ""
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n{auth}Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("utf-8") + body)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), 3)
    writer.close()
    await writer.wait_closed()
    headers, result = response.split(b"\r\n\r\n", 1)
    return int(headers.split(b" ", 2)[1]), json.loads(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plugins"])
async def test_async_application_http_auth_dedup_progress_and_legacy(monkeypatch, tmp_path, kind: str) -> None:
    """POST 接受后连接关闭不取消工作, 查询鉴权相同, 同 ID 不重复执行且旧调用保持原信封"""
    monkeypatch.setenv("SATRAP_API_TOKEN", _TOKEN)
    entered, release = asyncio.Event(), asyncio.Event()
    scopes = []

    async def apply(**scope):
        scopes.append(scope)
        with progress_instance("test", "one"), progress_phase("install_ms"):
            entered.set()
            await release.wait()
        return {"ok": True, "sessions": [{"ok": True, "session_id": "one"}]}

    async def reconcile_backend(**scope):
        return (await apply(**scope))["sessions"]

    if kind == "chat":
        server = ChatHTTPServer(cast(Any, SimpleNamespace(reconcile_plugins=apply)), port=0)
        endpoint = "/api/chat/plugins/reconcile"
        payload = {"plugin_name": "session_commands"}
    else:
        backend = SimpleNamespace(reconcile_edictum_plugins_async=reconcile_backend, reconcile_edictum_runtime_async=reconcile_backend)
        server = BackendHTTPServer(cast(Any, backend), port=0, static_dir=tmp_path)
        endpoint = "/api/edictum/plugins/reconcile" if kind == "plugins" else "/api/edictum/runtime/apply"
        payload = {"config_name": "one", "session_refs": [{"platform_id": "test", "session_id": "one"}]}
    key = "http-application-operation-001"
    try:
        await server.start()
        status, result = await _request(server, "POST", endpoint, {**payload, "operation_id": key}, authorized=False)
        assert status == 401 and not scopes
        status, result = await _request(server, "POST", endpoint, {**payload, "operation_id": key})
        assert status == 202 and result["operation_id"] == key
        await asyncio.wait_for(entered.wait(), 1)
        query = f"/api/runtime-operations/{key}"
        status, _body = await _request(server, "GET", query, authorized=False)
        assert status == 401
        status, result = await _request(server, "GET", query)
        assert status == 200 and result["status"] == "running" and result["instances"][0]["phase"] == "install"
        status, result = await _request(server, "POST", endpoint, {**payload, "operation_id": key})
        assert status == 202 and len(scopes) == 1
        status, _body = await _request(server, "POST", endpoint, {**payload, "operation_id": key, "plugin_name": "different"} if kind != "full" else {**payload, "operation_id": key, "config_name": "different"})
        assert status == 409 and len(scopes) == 1
        release.set()
        for _attempt in range(100):
            status, result = await _request(server, "GET", query)
            if result["status"] != "running":
                break
            await asyncio.sleep(0.01)
        assert status == 200 and result["status"] == "completed" and result["result"]["ok"]
        status, _body = await _request(server, "GET", "/api/runtime-operations/missing-operation-001")
        assert status == 404 and len(scopes) == 1
        status, result = await _request(server, "POST", endpoint, payload)
        assert status == 200 and result["ok"] and len(scopes) == 2
        assert "operation_id" not in scopes[0] and "operation_id" not in result
    finally:
        release.set()
        await server.stop()
