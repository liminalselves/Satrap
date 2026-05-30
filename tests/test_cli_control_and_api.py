from __future__ import annotations

import asyncio
from argparse import Namespace

from satrap.cli.client import DaemonClient
from satrap.main import _build_parser
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.BackGroundManager import ModelConfigManager


class _FakeClient:
    def __init__(self, alive: bool):
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


def test_parser_accepts_mode_flags_after_subcommands():
    """写命令应支持在子命令后使用离线 flags"""
    parser = _build_parser()

    args = parser.parse_args(["session", "register", "demo", "--class-path", "x.Y", "--offline"])
    assert args.command == "session"
    assert args.action == "register"
    assert args.offline is True

    args = parser.parse_args(["platform", "add", "mk", "--type", "misskey", "--force-offline"])
    assert args.command == "platform"
    assert args.force_offline is True


def test_parser_accepts_api_flags_for_control_commands():
    """控制命令应支持命令后的 API 地址覆盖"""
    parser = _build_parser()
    args = parser.parse_args(["status", "--api-host", "127.0.0.2", "--api-port", "19871"])

    assert args.command == "status"
    assert args.api_host == "127.0.0.2"
    assert args.api_port == 19871


def test_global_flags_survive_subparser_defaults():
    """全局 flags 写在子命令前也不能被子命令默认值覆盖"""
    parser = _build_parser()

    args = parser.parse_args(["--offline", "session", "register", "demo", "--class-path", "x.Y"])
    assert args.offline is True

    args = parser.parse_args(["--api-host", "127.0.0.9", "status"])
    assert args.api_host == "127.0.0.9"

    args = parser.parse_args(["--config", "demo.yaml", "session", "list"])
    assert args.config == "demo.yaml"


def test_daemon_client_new_routes_call_expected_paths(monkeypatch):
    """DaemonClient 新增方法应请求对应 HTTP 路由"""
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path, body))
        return {"ok": True}

    client = DaemonClient()
    monkeypatch.setattr(client, "_request", fake_request)

    client.register_session_class("demo", "mod.Demo")
    client.unregister_session_class("demo")
    client.set_model("llm", "default", {"model": "x"})
    client.update_model("llm", "default", {"temperature": 0.2})
    client.remove_model("llm", "default")

    assert calls == [
        ("POST", "/api/config/session-classes", {"name": "demo", "class_path": "mod.Demo", "description": "", "context_key": "", "model_key": ""}),
        ("DELETE", "/api/config/session-classes/demo", None),
        ("POST", "/api/config/models/llm/default", {"model": "x"}),
        ("PATCH", "/api/config/models/llm/default", {"temperature": 0.2}),
        ("DELETE", "/api/config/models/llm/default", None),
    ]


def test_http_session_class_write_routes(tmp_path):
    """后端 HTTP API 应支持 session class 注册和删除"""

    async def run():
        backend = BackendManager()
        backend._session_cls_cfg = SessionClassConfigManager(storage_path=tmp_path / "sessions.json")
        server = BackendHTTPServer(backend)
        payload = b'{"name":"dummy","class_path":"satrap.core.framework.Base.Session"}'

        status, data = await server._route("POST", "/api/config/session-classes", payload)
        assert status == 200
        assert data == {"ok": True}
        assert backend.session_class_mgr is not None
        assert backend.session_class_mgr.has_config("dummy")

        status, data = await server._route("DELETE", "/api/config/session-classes/dummy", b"")
        assert status == 200
        assert data == {"ok": True}

    asyncio.run(run())


def test_http_model_write_routes(tmp_path):
    """后端 HTTP API 应支持模型配置写接口"""

    async def run():
        backend = BackendManager()
        backend._model_cfg = ModelConfigManager(storage_path=tmp_path / "models.json")
        server = BackendHTTPServer(backend)

        status, data = await server._route("POST", "/api/config/models/llm/test", b'{"model":"gpt-test"}')
        assert status == 200
        assert data == {"ok": True}
        assert backend.model_config_manager is not None
        assert backend.model_config_manager.get_llm_config("test").model == "gpt-test"

        status, data = await server._route("PATCH", "/api/config/models/llm/test", b'{"temperature":0.2}')
        assert status == 200
        assert data == {"ok": True}
        assert backend.model_config_manager.get_llm_config("test").temperature == 0.2

        status, data = await server._route("DELETE", "/api/config/models/llm/test", b"")
        assert status == 200
        assert data == {"ok": True}

    asyncio.run(run())
