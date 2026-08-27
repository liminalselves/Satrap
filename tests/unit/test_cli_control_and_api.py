from __future__ import annotations

from argparse import Namespace
import json
from typing import Any

import pytest
from pathlib import Path

from satrap.cli.client import DaemonClient
from satrap.main import _build_parser
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.UserManager import UserManager
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.registry import create_default_edictum_type_registry


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


def test_platform_parser_accepts_session_type_binding():
    """平台命令应支持显式选择入站消息使用的会话类"""
    parser = _build_parser()
    args = parser.parse_args(
        ["platform", "add", "mk", "--type", "misskey", "--session-type", "assistant"]
    )

    assert args.session_type == "assistant"


def test_global_flags_survive_subparser_defaults():
    """全局 flags 写在子命令前也不能被子命令默认值覆盖"""
    parser = _build_parser()

    args = parser.parse_args(["--offline", "session", "register", "demo", "--class-path", "x.Y"])
    assert args.offline is True

    args = parser.parse_args(["--api-host", "127.0.0.9", "status"])
    assert args.api_host == "127.0.0.9"

    args = parser.parse_args(["--config", "demo.yaml", "session", "list"])
    assert args.config == "demo.yaml"


def test_daemon_client_new_routes_call_expected_paths(monkeypatch: pytest.MonkeyPatch):
    """
    DaemonClient 新增方法应请求对应 HTTP 路由

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(method: str, path: str, body: dict[str, Any] | None = None):
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


@pytest.mark.asyncio
async def test_http_session_class_write_routes(tmp_path: Path):
    """
    后端 HTTP API 应支持 session class 注册和删除

    参数:
    - tmp_path: tmp路径
    """
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


@pytest.mark.asyncio
async def test_http_edictum_cold_config_routes(tmp_path: Path):
    """
    后端 HTTP API 应暴露 Edictum 类型与命名冷配置完整 CRUD

    参数:
    - tmp_path: 临时目录
    """
    backend = BackendManager()
    backend._edictum_types = create_default_edictum_type_registry()
    backend._edictum_cfg = EdictumConfigManager(
        backend._edictum_types,
        tmp_path / "edictum.json",
    )
    server = BackendHTTPServer(backend)

    status, data = await server._route("GET", "/api/config/edictum/types", b"")
    assert status == 200
    assert {item["name"] for item in data["types"]} == {"simple", "async_simple"}

    status, data = await server._route(
        "POST",
        "/api/config/edictum/sessions",
        b'{"name":"assistant","edictum_type":"simple","model_name":"default"}',
    )
    assert status == 200
    assert data["config"]["provider"] == "edictum"

    status, data = await server._route(
        "PUT",
        "/api/config/edictum/sessions/assistant",
        b'{"name":"renamed","description":"updated"}',
    )
    assert status == 200
    assert data["name"] == "renamed"

    status, data = await server._route(
        "POST",
        "/api/config/edictum/sessions/renamed/disable",
        b"",
    )
    assert status == 200
    assert data["config"]["enabled"] is False

    status, data = await server._route(
        "DELETE",
        "/api/config/edictum/sessions/renamed",
        b"",
    )
    assert status == 200
    assert data == {"ok": True}


@pytest.mark.asyncio
async def test_http_session_config_and_runtime_routes(tmp_path: Path):
    """
    React 会话页依赖的配置编辑和实例创建路由应完整工作

    参数:
    - tmp_path: 临时目录
    """
    backend = BackendManager()
    backend._session_cls_cfg = SessionClassConfigManager(storage_path=tmp_path / "sessions.json")
    session_manager = SessionManager(db_path=tmp_path / "session-config.db")
    session_manager.class_cfg_mgr = backend._session_cls_cfg
    user_manager = UserManager(session_manager, db_path=tmp_path / "users.db")
    session_manager.user_manager = user_manager
    backend._session_mgr = session_manager
    backend._user_mgr = user_manager
    backend._platform_runtimes = {"main": (session_manager, user_manager)}
    backend._session_cls_cfg.register_by_class_path("dummy", "satrap.core.framework.Base.Session")
    server = BackendHTTPServer(backend)

    status, data = await server._route(
        "PUT",
        "/api/config/session-classes/dummy",
        b'{"name":"renamed","description":"demo","context_key":"room_id","model_key":"model_name","params":{"model_name":"default"}}',
    )
    assert status == 200
    assert data["config"]["description"] == "demo"
    assert data["config"]["context_key"] == "room_id"
    session_class_mgr = backend.session_class_mgr
    assert session_class_mgr is not None
    assert session_class_mgr.has_config("dummy") is False
    assert session_class_mgr.has_config("renamed") is True

    status, data = await server._route(
        "POST",
        "/api/config/session-classes/renamed/disable",
        b"",
    )
    assert status == 200
    assert data["config"]["enabled"] is False

    status, data = await server._route(
        "POST",
        "/api/config/session-classes/renamed/enable",
        b"",
    )
    assert status == 200
    assert data["config"]["enabled"] is True

    status, data = await server._route(
        "POST",
        "/api/sessions",
        b'{"class_name":"renamed","session_id":"runtime-1","adapter_id":"main","llm_name":"default"}',
    )
    assert status == 200
    assert data["session"]["session_id"] == "runtime-1"
    assert data["session"]["session_config"]["adapter_id"] == "main"

    status, data = await server._route("GET", "/api/sessions", b"")
    assert status == 200
    assert data["sessions"][0]["session_id"] == "runtime-1"
    assert data["sessions"][0]["active"] is False

    session_manager.store.update_runtime_fields("runtime-1", 1.0, 2)
    empty = session_manager.register_session_from_provider_config(
        "session_class",
        "renamed",
        session_id="runtime-empty",
    )
    single = session_manager.register_session_from_provider_config(
        "session_class",
        "renamed",
        session_id="runtime-single",
    )
    session_manager.store.update_runtime_fields(single.session_id or "", 1.0, 1)
    user_manager.get_or_create_user("user-1", "onebot")
    user_manager.bind_session("user-1", empty.session_id or "")
    user_manager.store.upsert_context_session(
        "user-1",
        "onebot",
        "renamed",
        empty.session_id or "",
    )

    status, data = await server._route(
        "POST",
        "/api/sessions/bulk-delete",
        b'{"mode":"empty"}',
    )
    assert status == 200
    assert data["deleted_ids"] == ["runtime-empty"]
    assert user_manager.get_user_session_ids("user-1") == []
    assert user_manager.store.get_context_session("user-1", "onebot", "renamed") is None

    status, data = await server._route(
        "POST",
        "/api/sessions/bulk-delete",
        b'{"mode":"single"}',
    )
    assert status == 200
    assert data["deleted_ids"] == ["runtime-single"]

    selected = session_manager.register_session_from_provider_config(
        "session_class",
        "renamed",
        session_id="runtime-selected",
    )
    status, data = await server._route(
        "POST",
        "/api/sessions/bulk-delete",
        json.dumps({
            "mode": "selected",
            "session_refs": [{"platform_id": "main", "session_id": selected.session_id}],
        }).encode(),
    )
    assert status == 200
    assert data["deleted_ids"] == ["runtime-selected"]

    status, data = await server._route(
        "DELETE",
        "/api/sessions/runtime-1?platform_id=main",
        b"",
    )
    assert status == 200
    assert data["deleted_ids"] == ["runtime-1"]


@pytest.mark.asyncio
async def test_http_session_discovery_directory_is_limited_to_config(tmp_path: Path):
    """
    Session 目录 API 只能操作配置声明的扫描目录

    参数:
    - tmp_path: 临时目录
    """
    configured_path = tmp_path / "sessions"
    backend = BackendManager()
    backend.config.session_scan_paths = [str(configured_path)]
    server = BackendHTTPServer(backend)

    status, data = await server._route(
        "POST",
        "/api/session/discovery/directories",
        json.dumps({"path": str(configured_path)}).encode(),
    )
    assert status == 200
    assert Path(data["path"]).exists()

    status, data = await server._route(
        "POST",
        "/api/session/discovery/directories",
        b'{"path":"outside"}',
    )
    assert status == 400
    assert "只能创建" in data["error"]


@pytest.mark.asyncio
async def test_http_model_write_routes(tmp_path: Path):
    """
    后端 HTTP API 应支持模型配置写接口

    参数:
    - tmp_path: tmp路径
    """
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
