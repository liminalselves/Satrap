"""控制服务 React 静态托管测试"""
from __future__ import annotations

import asyncio
import json
import urllib.parse
from pathlib import Path
from typing import cast

import pytest

from satrap.core.backend import control_server
from satrap.core.backend.static_ui import SPAStaticService
from satrap.core.type import UserInfo
from satrap.core.storage import StorageLayout
from satrap.core.utils.paths import get_project_root
from satrap.display.recorder import DisplayRecorder


def test_control_server_uses_workspace_project_root():
    """控制服务应从共享路径工具获取工作区根目录"""
    assert control_server.PROJECT_ROOT == get_project_root()


class _BufferWriter:
    """记录控制服务响应的测试写入器"""

    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        """
        记录响应字节

        参数:
        - data: 响应字节
        """
        self.data.extend(data)

    async def drain(self) -> None:
        """模拟等待响应写出"""

    def close(self) -> None:
        """记录连接关闭状态"""
        self.closed = True


async def _request(path: str, method: str = "GET", body: bytes = b"") -> bytes:
    """
    直接调用控制服务连接处理器

    参数:
    - path: 请求路径
    - method: HTTP 方法
    - body: 请求体

    返回:
    - bytes: 完整响应
    """
    reader = asyncio.StreamReader()
    header = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    reader.feed_data(header + body)
    reader.feed_eof()
    writer = _BufferWriter()
    await control_server._handle_request(
        reader,
        cast(asyncio.StreamWriter, writer),
    )
    assert writer.closed is True
    return bytes(writer.data)


@pytest.mark.asyncio
async def test_control_server_serves_react_without_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    控制服务应在平台后端未运行时提供 React 入口和 SPA 路由

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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
    """
    控制 API 应优先于 React SPA 路由

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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
    """
    控制服务应向前端提供当前来源和已保存的平台后端地址

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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


@pytest.mark.asyncio
async def test_control_server_manages_models_while_backend_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    控制服务应在平台后端未启动时完整管理模型配置

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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
    """
    控制服务应在平台后端未启动时完整管理会话类配置

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    session_class_path = tmp_path / "session-classes.json"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "session_class_config_path": str(session_class_path),
                "session_scan_paths": [str(tmp_path / "sessions")],
                "platforms": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(control_server, "CONFIG_PATH", config_path)

    created = await _request(
        "/config/session-classes",
        "POST",
        b'{"name":"cold","class_path":"missing.future.FutureSession","is_async":true,"params":{"model_name":"default"}}',
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
    assert b'"missing.future.FutureSession"' in listed
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
    """
    控制服务应在平台后端未启动时完整管理 Edictum 命名冷配置

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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
    """
    Chat 服务停止时控制服务应查询、回收和恢复历史

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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
    """
    控制服务应在后端停止时冷创建, 列出和删除持久化会话实例

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
                {
                    "data_root": str(tmp_path / "data"),
                    "session_class_config_path": str(tmp_path / "session-classes.json"),
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
                "class_path": "missing.future.FutureSession",
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
    assert refreshed.user_store.get("user-1").user_session == []   # type: ignore[union-attr]
    assert refreshed.user_store.get_context_session("user-1", "onebot", "cold") is None


@pytest.mark.asyncio
async def test_control_server_creates_and_scans_session_directory_while_backend_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    控制服务应在平台后端未启动时创建并扫描配置中的会话目录

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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
