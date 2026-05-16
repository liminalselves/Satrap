from __future__ import annotations

import asyncio

from satrap.admin_utils.config_editor import (
    configured_platform_types,
    find_config_path,
    load_config_document,
    parse_platforms_text,
    parse_raw_config,
    save_config_document,
    update_common_fields,
)
from satrap.cli.client import DaemonClient
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.backend.http_api import BackendHTTPServer


def test_daemon_client_shutdown_uses_shutdown_route(monkeypatch):
    """DaemonClient.shutdown 应请求后端 shutdown 路由"""
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path, body))
        return {"ok": True}

    client = DaemonClient()
    monkeypatch.setattr(client, "_request", fake_request)

    assert client.shutdown() == {"ok": True}
    assert calls == [("POST", "/api/shutdown", None)]


def test_http_shutdown_route_sets_backend_event():
    """shutdown API 应触发 BackendManager 的关闭事件"""

    async def run():
        backend = BackendManager()
        event = asyncio.Event()
        backend.set_shutdown_event(event)
        server = BackendHTTPServer(backend)

        status, data = await server._route("POST", "/api/shutdown", b"")
        await asyncio.sleep(0)

        assert status == 200
        assert data == {"ok": True}
        assert event.is_set()

    asyncio.run(run())


def test_config_editor_yaml_roundtrip(tmp_path):
    """配置编辑器应能读写 YAML 配置"""
    path = tmp_path / "config.yaml"
    data = {
        "api": {"host": "127.0.0.1", "port": 19870},
        "default_session_type": "misskey",
        "platforms": [{"id": "misskey1", "type": "misskey", "settings": {}}],
    }

    config = save_config_document(path, data)
    loaded = load_config_document(path)

    assert isinstance(config, BackendConfig)
    assert loaded["api"]["port"] == 19870
    assert loaded["platforms"][0]["id"] == "misskey1"


def test_config_editor_json_roundtrip(tmp_path):
    """配置编辑器应能读写 JSON 配置"""
    path = tmp_path / "config.json"
    data = {"api": {"host": "127.0.0.1", "port": 19871}, "platforms": []}

    config = save_config_document(path, data)
    loaded = load_config_document(path)

    assert config.api_port == 19871
    assert loaded["api"]["host"] == "127.0.0.1"


def test_parse_raw_config_rejects_non_object(tmp_path):
    """原始配置根节点必须是对象"""
    path = tmp_path / "config.json"

    try:
        parse_raw_config(path, "[]")
    except ValueError as e:
        assert "根节点" in str(e)
    else:
        raise AssertionError("应拒绝非对象配置")


def test_update_common_fields_and_parse_platforms():
    """常用配置保存应更新字段并解析 platforms"""
    platforms = parse_platforms_text('[{"id": "misskey1", "type": "misskey", "settings": {}}]')
    data = update_common_fields(
        {},
        api_host="127.0.0.1",
        api_port=19870,
        default_session_type="misskey",
        max_sessions=100,
        idle_timeout=60,
        llm_timeout=30.0,
        rate_limit=1.5,
        rate_burst=3,
        platforms=platforms,
    )

    assert data["api"]["port"] == 19870
    assert data["platforms"][0]["id"] == "misskey1"


def test_configured_platform_types_uses_config_and_health():
    """平台类型汇总应来自配置和后端运行态"""
    config_data = {"platforms": [{"id": "misskey1", "type": "misskey", "settings": {}}]}
    health = {"adapters": {"qq1": {"config_type": "qq"}}}

    assert configured_platform_types(config_data, health) == ["misskey", "qq"]


def test_find_config_path_defaults_to_yaml(tmp_path):
    """没有配置文件时默认创建 config.yaml"""
    assert find_config_path(tmp_path) == tmp_path / "config.yaml"
