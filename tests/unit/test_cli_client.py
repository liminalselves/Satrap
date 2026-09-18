"""
CLI HTTP 客户端测试 (client.py)

覆盖:
- DaemonUnavailable / DaemonError 异常语义 (不再返回 {"error": ...})
- is_alive 探测不抛异常
- 新增路由方法 (会话实例 / edictum / 插件) 请求路径正确
- 控制服务与聊天服务工厂
"""
from __future__ import annotations

from argparse import Namespace
from email.message import Message
from typing import Any
import io
import urllib.error

import pytest

from satrap.cli import client as client_mod
from satrap.cli.client import DaemonClient, DaemonError, DaemonInfo, DaemonUnavailable
from satrap.cli import common


def _client() -> DaemonClient:
    return DaemonClient(daemon=DaemonInfo(host="127.0.0.1", port=19999), token="t")


def test_request_raises_unavailable_on_connection_error(monkeypatch: pytest.MonkeyPatch):
    """连接失败抛 DaemonUnavailable"""

    def _raise(req: Any, timeout: float = 0):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _raise)
    with pytest.raises(DaemonUnavailable):
        _client().health()


def test_request_raises_daemon_error_with_server_message(monkeypatch: pytest.MonkeyPatch):
    """HTTP 错误解析服务端 error 字段"""

    def _raise(req: Any, timeout: float = 0):
        raise urllib.error.HTTPError(
            req.full_url, 409, "Conflict", hdrs=Message(), fp=io.BytesIO(b'{"error": "still referenced"}'),
        )

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _raise)
    with pytest.raises(DaemonError, match="still referenced"):
        _client().health()


def test_is_alive_never_raises(monkeypatch: pytest.MonkeyPatch):
    """is_alive 探测在不可达时返回 False"""

    def _raise(req: Any, timeout: float = 0):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _raise)
    assert _client().is_alive() is False


def test_new_routes_call_expected_paths(monkeypatch: pytest.MonkeyPatch):
    """会话实例 / edictum / 插件方法请求对应路由"""
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    client = _client()
    monkeypatch.setattr(client, "_request", lambda m, p, body=None: calls.append((m, p, body)) or {"ok": True})

    client.list_session_instances()
    client.delete_session_instance("s1", "main")
    client.bulk_delete_session_instances("empty")
    client.restart_session_instance("s1", "main")
    client.list_edictum_types()
    client.create_edictum_config({"name": "a", "edictum_type": "simple"})
    client.set_edictum_enabled("a", False)
    client.preview_edictum_runtime("a")
    client.set_chat_plugin_enabled("p1", True)
    client.set_chat_plugin_capability("p1", "tools", "shell", False)
    client.save_chat_plugin_config("p1", {"k": "v"})
    client.start_backend()

    paths = [p for _, p, _ in calls]
    assert paths == [
        "/api/sessions",
        "/api/sessions/s1?platform_id=main",
        "/api/sessions/bulk-delete",
        "/api/sessions/s1/restart?platform_id=main",
        "/api/config/edictum/types",
        "/api/config/edictum/sessions",
        "/api/config/edictum/sessions/a/disable",
        "/api/edictum/runtime/preview",
        "/api/chat/plugins/p1/enable",
        "/api/chat/plugins/p1/capability",
        "/api/chat/plugins/p1/config",
        "/start",
    ]
    assert calls[2][2] == {"mode": "empty"}
    assert calls[9][2] == {"kind": "tools", "cap": "shell", "enabled": False}


def test_control_and_chat_factories():
    """控制/聊天服务工厂使用各自端口与探测路径"""
    control = common.control_client_from_args(Namespace())
    chat = common.chat_client_from_args(Namespace())
    assert control.daemon.port == client_mod.DEFAULT_CONTROL_PORT
    assert control.health_path == "/status"
    assert chat.daemon.port == client_mod.DEFAULT_CHAT_PORT
    assert chat.health_path == "/api/chat/health"
