"""业务链路场景与集中边界契约"""
from __future__ import annotations

from email.message import Message
import urllib.error
import urllib.parse
import asyncio
from pathlib import Path
import pytest
from typing import NoReturn, cast
import json
import io
from satrap.core.backend.static_ui import SPAStaticService
from satrap.display.recorder import DisplayRecorder
from satrap.core.backend import control_server
from satrap.core.storage import StorageLayout
from satrap.core.type import UserInfo
import gzip
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from typing import Any
from unittest.mock import AsyncMock
import base64
from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.providers import EdictumProvider
from satrap.core.group_chat.stickers import StickerStore
from satrap.core.platform import PlatformAdapterManager, PlatformConfig, set_current_adapter_manager
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.event import MessageChain
from satrap.core.components import Image
from satrap.core.type import SessionConfig
from satrap.core.utils import paths
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.plugin_compatibility import PluginEnvironment
from support.media import png
from support.administration import _setup_adapter, _async_tools, _origin
from importlib import import_module
from setuptools import find_packages
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.Base import Session


class _control_static_BufferWriter:
    """记录控制服务响应的测试写入器"""

    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        """记录响应字节"""
        self.data.extend(data)

    async def drain(self) -> None:
        """模拟等待响应写出"""

    def get_extra_info(self, name: str) -> tuple[str, int] | None:
        """提供与真实流写入器一致的回环对端信息"""
        return ("127.0.0.1", 40000) if name == "peername" else None

    def close(self) -> None:
        """记录连接关闭状态"""
        self.closed = True

async def _request(
    path: str,
    method: str = "GET",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    authenticated: bool = True,
) -> bytes:
    """直接调用控制服务连接处理器"""
    reader = asyncio.StreamReader()
    request_headers = dict(headers or {})
    if authenticated:
        request_headers.setdefault("Authorization", f"Bearer {control_server._CONTROL_AUTH.token}")
    extra_headers = "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
    header = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{extra_headers}"
        "Connection: close\r\n\r\n"
    ).encode()
    reader.feed_data(header + body)
    reader.feed_eof()
    writer = _control_static_BufferWriter()
    await control_server._handle_request(
        reader,
        cast(asyncio.StreamWriter, writer),
    )
    assert writer.closed is True
    return bytes(writer.data)

@pytest.mark.asyncio
async def test_control_config_snapshots_overview_and_damaged_sources(tmp_path, monkeypatch):
    """配置摘要与正文版本共同更新, 保留脱敏并明确拒绝损坏来源"""
    model_path = tmp_path / 'models.json'
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps({'model_config_path': str(model_path), 'session_class_config_path': str(tmp_path / 'sessions.json'), 'platforms': []}), encoding='utf-8')
    monkeypatch.setattr(control_server, 'CONFIG_PATH', config_path)
    assert b'200' in await _request('/config/models/llm/bench', 'POST', b'{"model":"first","api_key":"private-test-key"}')
    first = await _request('/config/snapshot?resource=models&type=llm')
    snapshot = json.loads(first.split(b'\r\n\r\n', 1)[1])
    assert snapshot['data']['bench']['model'] == 'first' and b'private-test-key' not in first
    overview = json.loads((await _request('/config/overview')).split(b'\r\n\r\n', 1)[1])
    assert overview['model_counts']['llm'] == len(snapshot['data'])
    assert isinstance(overview['session_count'], int) and 'versions' in overview
    await _request('/config/models/llm/bench', 'PATCH', b'{"model":"second"}')
    changed = json.loads((await _request('/config/snapshot?resource=models&type=llm')).split(b'\r\n\r\n', 1)[1])
    assert changed['data']['bench']['model'] == 'second' and changed['versions'] != snapshot['versions']
    model_path.write_text('{', encoding='utf-8')
    assert b'400' in (await _request('/config/snapshot?resource=models&type=llm')).split(b'\r\n', 1)[0]
    assert b'400' in (await _request('/config/overview')).split(b'\r\n', 1)[0]


async def test_control_api_requires_authentication() -> None:
    """控制服务管理 API 缺少令牌时返回 401"""
    response = await _request("/status", authenticated=False)
    assert b"401 Error" in response
    assert b"unauthorized" in response

@pytest.mark.asyncio
async def test_control_api_rejects_untrusted_origin() -> None:
    """控制服务拒绝非白名单浏览器来源且不返回通配 CORS"""
    response = await _request(
        "/status",
        headers={"Origin": "https://evil.example"},
    )
    assert b"403 Error" in response
    assert b"Access-Control-Allow-Origin: *" not in response

@pytest.mark.asyncio
async def test_control_api_rejects_oversized_json_body() -> None:
    """控制服务应在读取超限 JSON 请求体前返回 413"""
    reader = asyncio.StreamReader()
    header = (
        "PUT /config HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"Authorization: Bearer {control_server._CONTROL_AUTH.token}\r\n"
        f"Content-Length: {control_server.CONTROL_MAX_BODY_BYTES + 1}\r\n\r\n"
    ).encode("utf-8")
    reader.feed_data(header)
    reader.feed_eof()
    writer = _control_static_BufferWriter()

    await control_server._handle_request(reader, cast(asyncio.StreamWriter, writer))

    assert b"413 Error" in writer.data
    assert b"request body too large" in writer.data

@pytest.mark.asyncio
async def test_control_server_rejects_connections_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """控制服务达到连接上限时应返回 503 且不扰动现有计数"""
    monkeypatch.setattr(
        control_server,
        "_CONTROL_ACTIVE_CONNECTIONS",
        control_server.CONTROL_MAX_CONNECTIONS,
    )
    response = await _request("/status")
    assert b"503 Error" in response
    assert b"service unavailable" in response

@pytest.mark.asyncio
async def test_control_server_serves_react_without_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """控制服务应在平台后端未运行时提供 React 入口和 SPA 路由"""
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("control-ui", encoding="utf-8")
    monkeypatch.setattr(
        control_server,
        "CONTROL_STATIC_UI",
        SPAStaticService(static_dir, excluded_prefixes=("/status", "/config")),
    )

    index_response = await _request("/")
    route_response = await _request("/settings")

    assert b"200 OK" in index_response
    assert b"control-ui" in index_response
    assert b"200 OK" in route_response
    assert b"control-ui" in route_response

@pytest.mark.asyncio
async def test_control_api_routes_are_not_captured_by_spa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """控制 API 应优先于 React SPA 路由"""
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("control-ui", encoding="utf-8")
    monkeypatch.setattr(
        control_server,
        "CONTROL_STATIC_UI",
        SPAStaticService(static_dir, excluded_prefixes=("/status", "/config")),
    )
    monkeypatch.setattr(control_server, "_check_backend_health", lambda: {"running": False})

    response = await _request("/status")

    assert b"Content-Type: application/json" in response
    assert b'"running": false' in response
    assert b"control-ui" not in response

@pytest.mark.asyncio
async def test_control_ui_config_uses_saved_backend_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """控制服务应向前端提供当前来源和已保存的平台后端地址"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "api:\n  host: 127.0.0.1\n  port: 29970\nplatforms: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(control_server, "CONFIG_PATH", config_path)

    response = await _request("/ui-config.json")

    assert b"Content-Type: application/json" in response
    assert b'"control_api": "http://127.0.0.1"' in response
    assert b'"backend_api": "http://127.0.0.1:29970"' in response
    assert b'"chat_api": "http://127.0.0.1:19872"' in response

def test_backend_runtime_record_round_trip_and_legacy_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后端 PID 文件应保存完整身份并拒绝旧版裸 PID"""
    runtime_file = tmp_path / "backend.pid"
    monkeypatch.setattr(control_server, "BACKEND_PID_FILE", runtime_file)
    record = control_server.BackendRuntimeRecord(1234, "runtime-token", "127.0.0.1", 29970)

    control_server._write_backend_runtime(record)

    assert control_server._read_backend_runtime() == record
    assert control_server._runtime_matches(
        record,
        {"pid": 1234, "runtime_id": "runtime-token"},
    )
    assert not control_server._runtime_matches(
        record,
        {"pid": 1234, "runtime_id": "other-token"},
    )

    runtime_file.write_text("1234", encoding="utf-8")
    assert control_server._read_backend_runtime() is None

def test_cleanup_backend_uses_configured_endpoint_and_rejects_stale_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清理后端应使用配置端口且不得终止身份不匹配的 PID"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api:\n  host: 127.0.0.1\n  port: 29970\n", encoding="utf-8")
    runtime_file = tmp_path / "backend.pid"
    monkeypatch.setattr(control_server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(control_server, "BACKEND_PID_FILE", runtime_file)
    monkeypatch.setattr(control_server, "_backend_process", None)
    control_server._write_backend_runtime(
        control_server.BackendRuntimeRecord(4321, "expected", "127.0.0.1", 29970)
    )
    killed: list[int] = []
    requested_urls: list[str] = []

    def fake_health(*_args: object) -> dict[str, object]:
        """返回与 PID 文件不匹配的运行时身份"""
        return {"running": True, "pid": 4321, "runtime_id": "different"}

    def process_running(_pid: int) -> bool:
        """模拟 PID 对应进程仍然存活"""
        return True

    def record_kill(pid: int, _signal: int) -> None:
        """记录可能发生的进程终止调用"""
        killed.append(pid)

    def record_request(url: str, method: str = "GET") -> str:
        """记录控制服务尝试访问的后端 URL"""
        requested_urls.append(url)
        return method

    def fail_urlopen(*_args: object, **_kwargs: object) -> NoReturn:
        """模拟后端关闭接口不可达"""
        raise OSError("offline")

    monkeypatch.setattr(control_server, "_check_backend_health", fake_health)
    monkeypatch.setattr(control_server, "_is_process_running", process_running)
    monkeypatch.setattr(control_server.os, "kill", record_kill)
    monkeypatch.setattr(control_server, "_authenticated_request", record_request)
    monkeypatch.setattr(control_server.urllib.request, "urlopen", fail_urlopen)

    control_server._cleanup_backend()

    assert requested_urls == ["http://127.0.0.1:29970/api/shutdown"]
    assert killed == []
    assert runtime_file.exists() is False

def test_backend_health_keeps_identity_from_degraded_http_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """健康端点返回 503 时控制服务仍应解析运行时身份"""
    response_body = json.dumps({
        "running": True,
        "healthy": False,
        "pid": 1234,
        "runtime_id": "runtime-token",
    }).encode("utf-8")

    def degraded_response(*_args: object, **_kwargs: object) -> NoReturn:
        """模拟包含 JSON 健康信息的 503 响应"""
        raise urllib.error.HTTPError(
            "http://127.0.0.1:29970/api/health",
            503,
            "Service Unavailable",
            Message(),
            io.BytesIO(response_body),
        )

    monkeypatch.setattr(control_server.urllib.request, "urlopen", degraded_response)

    health = control_server._check_backend_health("127.0.0.1", 29970)

    assert health["running"] is True
    assert health["healthy"] is False
    assert health["pid"] == 1234
    assert health["runtime_id"] == "runtime-token"

@pytest.mark.asyncio
async def test_control_server_manages_models_while_backend_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """控制服务应在平台后端未启动时完整管理模型配置"""
    model_path = tmp_path / "models.json"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"model_config_path": str(model_path), "platforms": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(control_server, "CONFIG_PATH", config_path)

    created = await _request(
        "/config/models/llm/cold",
        "POST",
        b'{"model":"gpt-cold","api_key":"secret-key"}',
    )
    listed = await _request("/config/models?type=llm")
    updated = await _request(
        "/config/models/llm/cold",
        "PATCH",
        b'{"name":"cold-renamed","temperature":0.2,"api_key":"******-key"}',
    )

    assert b'"ok": true' in created
    assert b'"model": "gpt-cold"' in listed
    assert b"secret-key" not in listed
    revealed = await _request("/config/models/llm/cold-renamed/api-key", "POST")
    assert b'"api_key": "secret-key"' in revealed
    assert b"secret-key" not in await _request("/config/models?type=llm")
    missing_key = await _request("/config/models/llm/missing/api-key", "POST")
    assert b"404" in missing_key.split(b"\r\n", 1)[0]
    assert b"secret-key" not in await _request("/config/models/llm/cold-renamed/api-key")
    assert b'"ok": true' in updated

    manager = control_server._model_config_service().manager
    assert manager.has_config("llm", "cold") is False
    assert manager.get_llm_config("cold-renamed").temperature == 0.2
    assert manager.get_llm_config("cold-renamed").api_key == "secret-key"

    deleted = await _request("/config/models/llm/cold-renamed", "DELETE")
    assert b'"ok": true' in deleted

@pytest.mark.asyncio
async def test_control_server_manages_session_classes_while_backend_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """控制服务应在平台后端未启动时完整管理会话类配置"""
    session_class_path = tmp_path / "session-classes.json"
    session_scan_path = tmp_path / "sessions"
    session_scan_path.mkdir()
    (session_scan_path / "future.py").write_text(
        "raise RuntimeError('不应在冷配置阶段导入')\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "session_class_config_path": str(session_class_path),
                "session_scan_paths": [str(session_scan_path)],
                "platforms": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(control_server, "CONFIG_PATH", config_path)

    created = await _request(
        "/config/session-classes",
        "POST",
        b'{"name":"cold","class_path":"sessions.future.FutureSession","is_async":true,"params":{"model_name":"default"}}',
    )
    listed = await _request("/config/session-classes")
    renamed = await _request(
        "/config/session-classes/cold",
        "PATCH",
        b'{"name":"cold-renamed","description":"renamed"}',
    )
    disabled = await _request(
        "/config/session-classes/cold-renamed/disable",
        "POST",
    )
    fetched = await _request("/config/session-classes/cold-renamed")

    assert b'"ok": true' in created
    assert b'"sessions.future.FutureSession"' in listed
    assert b'"ok": true' in renamed
    assert b'"ok": true' in disabled
    assert b'"enabled": false' in fetched
    assert b'"description": "renamed"' in fetched

    service = control_server._session_class_config_service()
    assert service.get("cold") is None
    assert service.get("cold-renamed") is not None

    deleted = await _request("/config/session-classes/cold-renamed", "DELETE")
    assert b'"ok": true' in deleted
    assert control_server._session_class_config_service().list_configs() == {}

@pytest.mark.asyncio
async def test_control_server_manages_edictum_config_while_backend_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """控制服务应在平台后端未启动时完整管理 Edictum 命名冷配置"""
    monkeypatch.setattr(control_server, "_check_backend_health", lambda: {"running": False})
    edictum_path = tmp_path / "edictum.json"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "data_root": str(tmp_path / "data"),
            "edictum_config_path": str(edictum_path),
            "platforms": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(control_server, "CONFIG_PATH", config_path)

    types = await _request("/config/edictum/types")
    created = await _request(
        "/config/edictum/sessions",
        "POST",
        b'{"name":"assistant","edictum_type":"async_simple","model_name":"default","plugins":["session_commands"]}',
    )
    instance = await _request(
        "/config/session-instances",
        "POST",
        b'{"session_provider":"edictum","session_type":"assistant","session_id":"edictum-cold-1","params":{"topic":"demo"}}',
    )
    renamed = await _request(
        "/config/edictum/sessions/assistant",
        "PATCH",
        b'{"name":"platform-assistant","description":"cold"}',
    )
    disabled = await _request(
        "/config/edictum/sessions/platform-assistant/disable",
        "POST",
    )
    fetched = await _request("/config/edictum/sessions/platform-assistant")

    assert b'"async_simple"' in types
    assert b'"ok": true' in created
    assert b'"ok": true' in instance
    assert b'"name": "platform-assistant"' in renamed
    assert b'"enabled": false' in disabled
    assert b'"description": "cold"' in fetched

    stored = control_server._session_instance_config_service("local").store.get(
        "edictum-cold-1"
    )
    assert stored is not None
    assert stored.session_type_name == "platform-assistant"
    assert stored.session_config == {"topic": "demo"}

    blocked = await _request(
        "/config/edictum/sessions/platform-assistant",
        "DELETE",
    )
    assert b'"references"' in blocked
    await _request(
        "/config/session-instances/edictum-cold-1?platform_id=local",
        "DELETE",
    )
    deleted = await _request(
        "/config/edictum/sessions/platform-assistant",
        "DELETE",
    )
    assert b'"ok": true' in deleted
    assert json.loads(edictum_path.read_text(encoding="utf-8")) == {}

@pytest.mark.asyncio
async def test_control_server_lists_edictum_plugins():
    """控制服务应返回 Edictum 可用插件的元数据和配置结构"""
    response = await _request("/config/edictum/plugins")

    assert b'"plugins"' in response
    assert b'"session_commands"' in response
    assert b'"config_schema"' in response
    assert b'"capabilities"' in response

def test_configured_platform_ids_excludes_chat_display_scope():
    """平台会话冷管理不应包含 Chat 展示层保留域"""
    platform_ids = control_server._configured_platform_ids({
        "platforms": [
            {"id": "chat", "type": "onebot"},
            {"id": "onebot-main", "type": "onebot"},
        ],
    })

    assert platform_ids == ["local", "onebot-main"]

@pytest.mark.asyncio
async def test_control_server_cold_manages_chat_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Chat 服务停止时控制服务应查询、回收和恢复历史"""
    data_root = tmp_path / "data"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"data_root": str(data_root), "platforms": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(control_server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(control_server, "_check_chat_health", lambda: False)
    layout = StorageLayout(data_root)
    recorder = DisplayRecorder(str(layout.platform_db("chat")), "cold-history")
    recorder.save_meta("default")
    recorder.start_turn("冷管理历史")
    recorder.end_turn("完成")
    recorder.close()

    listed = await _request("/chat/history?search=%E5%86%B7%E7%AE%A1%E7%90%86")
    assert b'"conversation_id": "cold-history"' in listed
    deleted = await _request(
        "/chat/history/delete",
        "POST",
        b'{"mode":"selected","conversation_ids":["cold-history"]}',
    )
    assert b'"deleted_count": 1' in deleted
    trash = await _request("/chat/history/trash")
    trash_body = json.loads(trash.split(b"\r\n\r\n", 1)[1])
    archive_id = trash_body["items"][0]["archive_id"]

    restored = await _request(
        "/chat/history/trash/restore",
        "POST",
        json.dumps({"archive_id": archive_id}).encode("utf-8"),
    )
    assert b'"session_id": "cold-history"' in restored

@pytest.mark.asyncio
async def test_control_server_cold_manages_session_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """控制服务应在后端停止时冷创建, 列出和删除持久化会话实例"""
    session_scan_path = tmp_path / "sessions"
    session_scan_path.mkdir()
    (session_scan_path / "future.py").write_text(
        "raise RuntimeError('不应在冷配置阶段导入')\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
                {
                    "data_root": str(tmp_path / "data"),
                    "session_class_config_path": str(tmp_path / "session-classes.json"),
                    "session_scan_paths": [str(session_scan_path)],
                    "edictum_config_path": str(tmp_path / "edictum.json"),
                    "platforms": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(control_server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(control_server, "_check_backend_health", lambda: {"running": False})

    sandbox_root = tmp_path / "sandbox"
    await _request(
        "/config/session-classes",
        "POST",
        json.dumps(
            {
                "name": "cold",
                "class_path": "sessions.future.FutureSession",
                "params": {
                    "model_name": "default",
                    "sandbox_dir": str(sandbox_root),
                },
            }
        ).encode("utf-8"),
    )
    created = await _request(
        "/config/session-instances",
        "POST",
        b'{"session_provider":"session_class","session_type":"cold","session_id":"cold-1","params":{"topic":"demo"}}',
    )
    listed = await _request("/config/session-instances")

    assert b'"ok": true' in created
    assert b'"session_id": "cold-1"' in listed
    assert b'"active": false' in listed

    service = control_server._session_instance_config_service("local")
    sandbox_path = service.storage_layout.session_sandbox("local", "cold-1")
    assert not sandbox_path.exists()
    sandbox_path.mkdir(parents=True)
    (sandbox_path / "result.txt").write_text("data", encoding="utf-8")
    service.user_store.upsert(
        UserInfo(
            user_id="user-1",
            user_platform="onebot",
            user_nickname="tester",
            user_session=["cold-1"],
        )
    )
    service.user_store.upsert_context_session(
        "user-1",
        "onebot",
        "cold",
        "cold-1",
    )
    deleted = await _request(
        "/config/session-instances/bulk-delete",
        "POST",
        b'{"mode":"selected","session_refs":[{"platform_id":"local","session_id":"cold-1"}]}',
    )

    assert b'"deleted_ids": ["cold-1"]' in deleted
    assert not sandbox_path.exists()
    refreshed = control_server._session_instance_config_service("local")
    assert refreshed.store.get("cold-1") is None
    refreshed_user = refreshed.user_store.get("user-1")
    assert refreshed_user is not None
    assert refreshed_user.user_session == []
    assert refreshed.user_store.get_context_session("user-1", "onebot", "cold") is None

@pytest.mark.asyncio
async def test_control_server_creates_and_scans_session_directory_while_backend_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """控制服务应在平台后端未启动时创建并扫描配置中的会话目录"""
    configured_scan_path = "sessions"
    scan_path = tmp_path / configured_scan_path
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "session_scan_paths": [configured_scan_path],
                "platforms": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(control_server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(control_server, "PROJECT_ROOT", tmp_path)

    created = await _request(
        "/config/session/discovery/directories",
        "POST",
        json.dumps({"path": configured_scan_path}).encode(),
    )
    assert b'"ok": true' in created
    assert scan_path.is_dir()
    assert (scan_path / "__init__.py").is_file()

    (scan_path / "cold_session.py").write_text(
        "\n".join(
            [
                "from satrap.core.framework import Session",
                "",
                "class ColdSession(Session):",
                "    def __init__(self, session_id: str, topic: str = 'demo'):",
                "        super().__init__(session_id)",
            ]
        ),
        encoding="utf-8",
    )
    discovered = await _request(
        f"/config/session/discovery?path={urllib.parse.quote(configured_scan_path)}"
    )
    discovered_payload = json.loads(discovered.split(b"\r\n\r\n", 1)[1])

    assert b'"ColdSession"' in discovered
    assert b'"topic": ""' in discovered
    assert discovered_payload["paths"] == [configured_scan_path]

    rejected = await _request(
        "/config/session/discovery/directories",
        "POST",
        json.dumps({"path": str(tmp_path / "outside")}).encode(),
    )
    assert b"400" in rejected.split(b"\r\n", 1)[0]
    assert not (tmp_path / "outside").exists()

class _static_ui_BufferWriter:
    """记录响应字节的测试写入器"""

    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        """记录响应内容"""
        self.data.extend(data)

@pytest.mark.asyncio
async def test_spa_static_service_serves_index_assets_and_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """静态服务应提供入口, 资源和 SPA 路由回退"""
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<main>Satrap</main>", encoding="utf-8")
    (assets_dir / "app.js").write_text("window.satrap = true", encoding="utf-8")
    service = SPAStaticService(static_dir)

    index_writer = _static_ui_BufferWriter()
    assert await service.serve(cast(asyncio.StreamWriter, index_writer), "/") is True
    assert b"200 OK" in index_writer.data
    assert b"Cache-Control: no-cache" in index_writer.data
    assert b"<main>Satrap</main>" in index_writer.data

    asset_writer = _static_ui_BufferWriter()
    assert await service.serve(cast(asyncio.StreamWriter, asset_writer), "/assets/app.js?v=1") is True
    assert b"Content-Type: text/javascript" in asset_writer.data
    assert b"Cache-Control: no-cache" in asset_writer.data
    assert b"window.satrap = true" in asset_writer.data

    route_writer = _static_ui_BufferWriter()
    assert await service.serve(cast(asyncio.StreamWriter, route_writer), "/settings") is True
    assert b"<main>Satrap</main>" in route_writer.data
    assert b"Cache-Control: no-cache" in route_writer.data

    monkeypatch.setattr(control_server, 'CONTROL_STATIC_UI', SPAStaticService(
        static_dir, excluded_prefixes=control_server.CONTROL_STATIC_UI.excluded_prefixes,
    ))
    for route in ('/platforms', '/platforms/approvals?source=friend', '/platforms/ob/groups'):
        response = await _request(route, authenticated=False)
        assert b'200 OK' in response and b'<main>Satrap</main>' in response
    for route in ('/api/platforms/ob/groups', '/config/platforms'):
        response = await _request(route, authenticated=False)
        assert b'401 Error' in response and b'<main>Satrap</main>' not in response
    rejected = await _request('/api/platforms/ob/groups/settings', method='PATCH', body=b'{}', authenticated=False)
    assert b'401 Error' in rejected


@pytest.mark.asyncio
async def test_spa_static_service_uses_stable_javascript_mime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """JavaScript MIME 不应受操作系统类型映射影响"""
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "app-AbCd1234.js").write_text("export {}", encoding="utf-8")

    def guess_text_type(_: str) -> tuple[str, None]:
        """模拟不可靠的系统 MIME 映射"""
        return "text/plain", None

    monkeypatch.setattr("satrap.core.backend.static_ui.mimetypes.guess_type", guess_text_type)
    writer = _static_ui_BufferWriter()

    assert await SPAStaticService(static_dir).serve(
        cast(asyncio.StreamWriter, writer),
        "/assets/app-AbCd1234.js",
    ) is True

    assert b"Content-Type: text/javascript" in writer.data
    assert b"X-Content-Type-Options: nosniff" in writer.data

@pytest.mark.asyncio
async def test_spa_static_service_negotiates_precompressed_assets(tmp_path: Path):
    """静态服务应优先协商 Brotli 并支持 gzip 回退"""
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    asset_path = assets_dir / "app-AbCd1234.js"
    original = b"window.satrap = true"
    brotli_content = b"brotli-content"
    gzip_content = gzip.compress(original)
    asset_path.write_bytes(original)
    Path(f"{asset_path}.br").write_bytes(brotli_content)
    Path(f"{asset_path}.gz").write_bytes(gzip_content)
    service = SPAStaticService(static_dir)

    brotli_writer = _static_ui_BufferWriter()
    assert await service.serve(
        cast(asyncio.StreamWriter, brotli_writer),
        "/assets/app-AbCd1234.js",
        {"accept-encoding": "gzip, br"},
    ) is True
    assert b"Content-Encoding: br" in brotli_writer.data
    assert b"Vary: Accept-Encoding" in brotli_writer.data
    assert bytes(brotli_writer.data).endswith(brotli_content)

    gzip_writer = _static_ui_BufferWriter()
    assert await service.serve(
        cast(asyncio.StreamWriter, gzip_writer),
        "/assets/app-AbCd1234.js",
        {"accept-encoding": "br;q=0, gzip"},
    ) is True
    assert b"Content-Encoding: gzip" in gzip_writer.data
    assert bytes(gzip_writer.data).endswith(gzip_content)

    plain_writer = _static_ui_BufferWriter()
    assert await service.serve(
        cast(asyncio.StreamWriter, plain_writer),
        "/assets/app-AbCd1234.js",
    ) is True
    assert b"Content-Encoding:" not in plain_writer.data
    assert bytes(plain_writer.data).endswith(original)

@pytest.mark.asyncio
async def test_spa_static_service_rejects_api_and_directory_traversal(tmp_path: Path):
    """静态服务应交还 API 请求并拒绝目录越界"""
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("index", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    service = SPAStaticService(static_dir)

    api_writer = _static_ui_BufferWriter()
    assert await service.serve(cast(asyncio.StreamWriter, api_writer), "/api/health") is False
    assert api_writer.data == b""

    traversal_writer = _static_ui_BufferWriter()
    assert await service.serve(cast(asyncio.StreamWriter, traversal_writer), "/%2e%2e/secret.txt") is True
    assert b"404 Error" in traversal_writer.data
    assert b"secret" not in traversal_writer.data

@pytest.mark.asyncio
async def test_spa_static_service_rejects_hidden_files_and_source_maps(tmp_path: Path):
    """公开静态服务不得暴露隐藏文件或 source map"""
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / ".env").write_text("SECRET=value", encoding="utf-8")
    (assets_dir / "app.js.map").write_text('{"sources":[]}', encoding="utf-8")
    service = SPAStaticService(static_dir)

    hidden_writer = _static_ui_BufferWriter()
    source_map_writer = _static_ui_BufferWriter()
    assert await service.serve(cast(asyncio.StreamWriter, hidden_writer), "/.env") is True
    assert await service.serve(
        cast(asyncio.StreamWriter, source_map_writer),
        "/assets/app.js.map",
    ) is True
    assert b"404 Error" in hidden_writer.data
    assert b"SECRET=value" not in hidden_writer.data
    assert b"404 Error" in source_map_writer.data
    assert b"sources" not in source_map_writer.data

@pytest.mark.asyncio
async def test_backend_http_server_exposes_ui_runtime_config():
    """平台后端应提供前端运行时服务地址并避免被 SPA 捕获"""
    backend = BackendManager()
    backend.config.api_host = "0.0.0.0"
    backend.config.api_port = 29970
    server = BackendHTTPServer(backend)

    writer = _static_ui_BufferWriter()
    handled = await server._serve_static(cast(asyncio.StreamWriter, writer), "/ui-config.json")
    status, data = await server._route("GET", "/ui-config.json", b"")

    assert handled is False
    assert status == 200
    assert data["backend_api"] == "http://127.0.0.1:29970"
    assert data["control_api"] == "http://127.0.0.1:19871"

@pytest.mark.asyncio
async def test_managed_sticker_reaches_onebot_without_authorizing_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path)
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    store = StickerStore(StorageLayout(tmp_path))
    payload = png()
    store.create({"name": "验收表情", "collection": "测试", "tags": [], "idempotency_key": "create-1"}, payload)
    blob = next(store.files.iterdir())
    image = Image.fromFileSystem(str(blob))
    assert await image.convert_to_base64() == base64.b64encode(payload).decode()
    adapter = OneBotAdapter(PlatformConfig(id="test", type="onebot"))
    adapter._bot = AsyncMock()
    adapter._bot.send_group_msg.return_value = {"message_id": 42}
    receipt = await adapter.send_message("group%456", MessageChain([image]))
    assert receipt.status == "success" and receipt.message_ids == ("42",)
    adapter._bot.send_group_msg.assert_awaited_once()
    for denied in [store.database, tmp_path / "group-chat" / "secret.db", store.files / "secret.db"]:
        with pytest.raises(paths.MediaSourcePermissionError):
            paths.ensure_allowed_media_path(str(denied))
    paths.set_media_allowed_roots([str(tmp_path / "explicit")])
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(blob))

def test_sticker_symlink_cannot_authorize_external_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path)
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    outside = tmp_path / "private.db"
    outside.write_bytes(b"private")
    link = tmp_path / "group-chat" / "stickers" / ("a" * 64)
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前 Windows 会话无符号链接权限")
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(link))

@pytest.mark.asyncio
async def test_forced_approval_cannot_fall_back_to_direct_sdk():
    adapter = _setup_adapter()
    try:
        tool = next(t for t in _async_tools({"write_tools_enabled": True, "allowed_callers": "123", "high_risk_approval": True})
                    if t.tool_name == "group_admin_kick")
        with bind_call_origin(_origin()):
            result = await tool.execute(user_id="321")
        assert result["ok"] is False and "审批服务" in result["error"]
        adapter._bot.set_group_kick.assert_not_awaited()
    finally:
        set_current_adapter_manager(None)

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin_required,host_required", [(True, False), (False, True), (True, True), (False, False)])
@pytest.mark.parametrize("revocation", ["callers", "approval"])
async def test_approval_is_single_durable_request_and_sources_are_rechecked(tmp_path, plugin_required, host_required, revocation):
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[{
        "id": "bot", "type": "onebot", "session_provider": "edictum", "session_type": "assistant", "settings": settings,
    }]))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.patch_account("100", expected_revision=1, mode="all", approval_defaults={
        "ban_group_member": "approval_required" if host_required else "auto_execute",
    })
    directory.confirm_membership("100", "456", True)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / "edictum.json")
    plugin_config = {"write_tools_enabled": True, "allowed_callers": "123", "high_risk_approval": plugin_required}
    configs.create("assistant", {"edictum_type": "async_simple", "model_name": "base", "plugins": [
        {"name": "group_admin", "config": plugin_config},
    ]})
    provider = EdictumProvider(configs, registry, default_checkpoint_db=str(backend.platform_db_path("bot")))
    manager = SessionManager(db_path=backend.platform_db_path("bot"), platform_id="bot")
    manager.register_provider(provider)
    cfg = SessionConfig(session_id="source-session", session_type_name="assistant", provider_name="edictum")
    manager.store.upsert(cfg)
    session: Any = provider.create_session(cfg, llm=cast(Any, object()))
    session.plugin_environment = PluginEnvironment("platform", "onebot")
    session.user_input_provider = AsyncMock(side_effect=AssertionError("不能增加第二套即时审批"))
    manager.pool.put(cfg.session_id, session, "assistant")
    backend._platform_runtimes["bot"] = cast(Any, (manager, None))
    action_id = "approval-1"
    adapter.group_action_handler = lambda gid, action, params: backend.submit_group_action(
        "bot", "100", gid, action_id, action, params, actor_kind="model",
    )
    set_current_adapter_manager(backend._adapter_mgr)
    try:
        await provider.prepare_session_async(session)
        tool = session._wf.tools_manager.tools["group_admin_ban"]
        origin = CallOrigin("bot", "100", "GroupMessage", "456", "123", "message-1", "request-1")
        with bind_call_origin(origin):
            result = await tool.execute(user_id="42", duration=60)
        if plugin_required or host_required:
            assert result["data"]["state"] == "pending"
            adapter._bot.set_group_ban.assert_not_awaited()
            result = {"data": await backend.decide_group_action("bot", "100", "456", action_id, approve=True)}
        assert result["data"]["state"] == "succeeded"
        adapter._bot.set_group_ban.assert_awaited_once()
        session.user_input_provider.assert_not_awaited()
        action_id = "approval-2"
        plugin_config["high_risk_approval"] = True
        configs.update("assistant", {"plugins": [{"name": "group_admin", "config": plugin_config}]})
        tool.config["high_risk_approval"] = True
        with bind_call_origin(origin):
            submitted = await tool.execute(user_id="43", duration=60)
        assert submitted["data"]["state"] == "pending"
        if revocation == "callers":
            plugin_config["allowed_callers"] = ""
        else:
            plugin_config["high_risk_approval"] = False
        configs.update("assistant", {"plugins": [{"name": "group_admin", "config": plugin_config}]})
        revoked = await backend.decide_group_action("bot", "100", "456", action_id, approve=True)
        assert revoked["state"] == "failed" and cast(dict[str, Any], revoked["result"])["reason"] == "model_permission_revoked"
        adapter._bot.set_group_ban.assert_awaited_once()
    finally:
        await provider.release_session_async(session)
        set_current_adapter_manager(None)

@pytest.fixture(scope="module")
def packaged_modules() -> set[str]:
    """按项目的 setuptools 配置检查真实打包发现结果"""
    root = Path(__file__).resolve().parents[2]
    return set(find_packages(where=str(root), include=["satrap", "satrap.*"]))

@pytest.mark.parametrize("module,names", [
    ("satrap.core.APICall.EmbedCall", ["Embedding", "AsyncEmbedding", "parse_embedding_response"]),
    ("satrap.core.APICall.ReRankCall", ["ReRank", "AsyncReRank", "parse_rerank_result"]),
    ("satrap.core.framework.Base", ["Session", "AsyncSession", "ModelWorkflowFramework", "AsyncModelWorkflowFramework"]),
    ("satrap.core.framework.command", ["CommandHandler", "AsyncCommandHandler"]),
    ("satrap.core.utils.TCBuilder", ["Tool", "AsyncTool", "ToolsManager", "AsyncToolsManager", "create_tool_defined"]),
    ("satrap.core.utils.context", ["ContextManager", "AsyncContextManager", "PreparedModelContext", "add_bot_message"]),
    ("satrap.core.utils.skills", ["Skill", "SkillsManager", "SkillTool"]),
    ("satrap.core.utils.outbound", ["safe_sync_get", "safe_async_get", "resolve_outbound_http_url"]),
    ("satrap.core.utils.mcp", ["MCPClient", "MCPToolAdapter", "SyncMCPToolAdapter", "export_tools_to_mcp"]),
    ("satrap.edictum.simple_session", ["SimpleSession", "AsyncSimpleSession", "HandlerResult", "SessionHandler"]),
    ("satrap.expend.tools.agent", ["SubAgent", "AsyncSubAgent", "SubAgentModel", "AsyncSubAgentModel"]),
    ("satrap.expend.tools.search", ["SearchTool", "AsyncSearchTool", "FetchPageTool", "AsyncFetchPageTool"]),
    ("satrap.expend.tools.sandbox_tools", ["CodeSandboxTool", "AsyncCodeSandboxTool"]),
    ("satrap.expend.plugins.base_take.tools", ["get_tools", "ReadDocumentTool", "AsyncReadDocumentTool"]),
    ("satrap.expend.plugins.memory.tools", ["get_tools", "AddMemoryTool", "AsyncAddMemoryTool", "GetMemoryTool", "AsyncGetMemoryTool"]),
    ("satrap.expend.plugins.satrap_coding.tools", ["get_tools", "ReadFileTool", "AsyncReadFileTool", "WriteFileTool", "AsyncWriteFileTool"]),
])
def test_legacy_imports_and_package_discovery(module: str, names: list[str], packaged_modules: set[str]):
    assert module in packaged_modules
    loaded = import_module(module)
    assert loaded.__spec__ is not None and loaded.__spec__.submodule_search_locations is not None
    for name in names:
        assert callable(getattr(loaded, name))

def test_session_reexport_cannot_escape_its_package(tmp_path: Path, monkeypatch):
    class ExternalSession(Session):
        pass

    module = import_module("satrap.core.framework.Base")
    monkeypatch.setattr(module, "Session", ExternalSession)
    manager = SessionClassConfigManager(storage_path=tmp_path / "sessions.json")
    with pytest.raises(ValueError, match="不是模块内声明"):
        manager._load_class("satrap.core.framework.Base.Session")

@pytest.mark.asyncio
async def test_control_password_endpoints_gate_bootstrap_and_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """控制端访问密码: 免密状态, 设密码关闭回环引导, 登录签发 cookie, 改清密码即时生效, Bearer 不受影响"""
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
    origin = {"Origin": "http://127.0.0.1:5173"}

    status_response = await _request("/auth/password/status", authenticated=False)
    assert b'"password_required"' in status_response and b"false" in status_response
    bootstrap = await _request("/auth/session", method="POST", headers=origin, authenticated=False)
    assert b"200" in bootstrap

    update_dashboard_password("Secret-123")
    status_response = await _request("/auth/password/status", authenticated=False)
    assert b"true" in status_response
    bootstrap = await _request("/auth/session", method="POST", headers=origin, authenticated=False)
    assert b"401" in bootstrap
    bearer = await _request("/auth/session", method="POST", headers=origin)
    assert b"200" in bearer

    bad_login = await _request("/auth/password/login", method="POST", headers=origin,
                               body=b'{"password":"wrong-123"}', authenticated=False)
    assert b"401" in bad_login
    login = await _request("/auth/password/login", method="POST", headers=origin,
                           body=b'{"password":"Secret-123"}', authenticated=False)
    assert b"200" in login and b"Set-Cookie" in login

    wrong_current = await _request("/auth/password", method="PUT",
                                   body=b'{"current_password":"nope","new_password":"Other-456"}')
    assert b"403" in wrong_current
    changed = await _request("/auth/password", method="PUT",
                             body=b'{"current_password":"Secret-123","new_password":"Other-456"}')
    assert b"200" in changed
    relogin = await _request("/auth/password/login", method="POST", headers=origin,
                             body=b'{"password":"Other-456"}', authenticated=False)
    assert b"200" in relogin
    cleared = await _request("/auth/password", method="PUT",
                             body=b'{"current_password":"Other-456","new_password":null}')
    assert b"200" in cleared
    status_response = await _request("/auth/password/status", authenticated=False)
    assert b"false" in status_response
