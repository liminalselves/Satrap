from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pathlib import Path

from satrap.core.config_document import (
    config_exists,
    create_default_config,
    configured_platform_types,
    delete_platform,
    find_config_path,
    load_config_document,
    parse_platforms_text,
    parse_raw_config,
    save_config_document,
    upsert_platform,
    update_common_fields,
)
from satrap.cli.client import DaemonClient
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.config_loader import ConfigLoader


def test_daemon_client_shutdown_uses_shutdown_route(monkeypatch: pytest.MonkeyPatch):
    """
    DaemonClient.shutdown 应请求后端 shutdown 路由

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    calls: list[Any] = []

    def fake_request(method: str, path: str, body: dict[str, Any] | None = None):
        calls.append((method, path, body))
        return {"ok": True}

    client = DaemonClient()
    monkeypatch.setattr(client, "_request", fake_request)

    assert client.shutdown() == {"ok": True}
    assert calls == [("POST", "/api/shutdown", None)]


@pytest.mark.asyncio
async def test_http_shutdown_route_sets_backend_event():
    """shutdown API 应触发 BackendManager 的关闭事件"""
    backend = BackendManager()
    event = asyncio.Event()
    backend.set_shutdown_event(event)
    server = BackendHTTPServer(backend)

    status, data = await server._route("POST", "/api/shutdown", b"")
    await asyncio.sleep(0)

    assert status == 200
    assert data == {"ok": True}
    assert event.is_set()


def test_config_editor_yaml_roundtrip(tmp_path: Path):
    """
    配置编辑器应能读写 YAML 配置

    参数:
    - tmp_path: tmp路径
    """
    path = tmp_path / "config.yaml"
    data: dict[str, Any] = {
        "api": {"host": "127.0.0.1", "port": 19870},
        "default_session_type": "misskey",
        "platforms": [{"id": "misskey1", "type": "misskey", "settings": {}}],
    }

    config = save_config_document(path, data)
    loaded = load_config_document(path)

    assert isinstance(config, dict)
    assert loaded["api"]["port"] == 19870
    assert loaded["platforms"][0]["id"] == "misskey1"


def test_config_editor_json_roundtrip(tmp_path: Path):
    """
    配置编辑器应能读写 JSON 配置

    参数:
    - tmp_path: tmp路径
    """
    path = tmp_path / "config.json"
    data: dict[str, Any] = {"api": {"host": "127.0.0.1", "port": 19871}, "platforms": []}

    config = save_config_document(path, data)
    loaded = load_config_document(path)

    assert config["api"]["port"] == 19871
    assert loaded["api"]["host"] == "127.0.0.1"


def test_parse_raw_config_rejects_non_object(tmp_path: Path):
    """
    原始配置根节点必须是对象

    参数:
    - tmp_path: tmp路径
    """
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
    config_data: dict[str, Any] = {"platforms": [{"id": "misskey1", "type": "misskey", "settings": {}}]}
    health: dict[str, Any] = {"adapters": {"qq1": {"config_type": "qq"}}}

    assert configured_platform_types(config_data, health) == ["misskey", "qq"]


def test_find_config_path_defaults_to_yaml(tmp_path: Path):
    """
    没有配置文件时默认指向 .satrap/config.yaml

    参数:
    - tmp_path: tmp路径
    """
    assert find_config_path(tmp_path) == tmp_path / ".satrap" / "config.yaml"


def test_find_config_path_prefers_root_config(tmp_path: Path):
    """
    根目录配置优先于 satrap 目录配置

    参数:
    - tmp_path: tmp路径
    """
    root_config = tmp_path / "config.yaml"
    nested_config = tmp_path / ".satrap" / "config.yaml"
    nested_config.parent.mkdir()
    root_config.write_text("platforms: []\n", encoding="utf-8")
    nested_config.write_text("api:\n  port: 19999\nplatforms: []\n", encoding="utf-8")

    assert find_config_path(tmp_path) == root_config


def test_create_default_config_under_satrap(tmp_path: Path):
    """
    无配置文件时应在 .satrap/config.yaml 创建默认配置

    参数:
    - tmp_path: tmp路径
    """
    assert not config_exists(tmp_path)

    path = tmp_path / ".satrap" / "config.yaml"
    config = create_default_config(path)

    assert path.exists()
    assert config["api"]["host"] == "127.0.0.1"
    assert load_config_document(path)["platforms"] == []


def test_config_loader_autodetect_creates_default_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    ConfigLoader.autodetect 无配置时创建并加载 .satrap/config.yaml

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    monkeypatch.chdir(tmp_path)

    config = ConfigLoader.autodetect()
    path = tmp_path / ".satrap" / "config.yaml"

    assert path.exists()
    assert config.api_port == 19870


def test_config_loader_autodetect_keeps_root_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    根目录已有配置时不创建 .satrap/config.yaml

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("api:\n  port: 19999\nplatforms: []\n", encoding="utf-8")

    config = ConfigLoader.autodetect()

    assert config.api_port == 19999
    assert not (tmp_path / ".satrap" / "config.yaml").exists()


def test_platform_upsert_add_update_and_delete():
    """平台快捷编辑应支持新增, 修改和删除"""
    platforms: list[dict[str, Any]] = []
    platforms = upsert_platform(
        platforms,
        {
            "id": "misskey_main",
            "type": "misskey",
            "settings": {
                "base_url": "https://misskey.example/",
                "api_token": "token",
                "chat_enabled": True,
                "room_enabled": False,
            },
        },
        original_id=None,
    )
    platforms = upsert_platform(
        platforms,
        {
            "id": "onebot_main",
            "type": "onebot",
            "settings": {
                "host": "127.0.0.1",
                "port": 6700,
                "access_token": "token",
                "secret": "",
                "enable_private": True,
                "enable_group": True,
            },
        },
        original_id=None,
    )
    platforms = upsert_platform(
        platforms,
        {"id": "misskey_prod", "type": "misskey", "settings": {"base_url": "https://misskey.prod/", "api_token": "token2"}},
        original_id="misskey_main",
    )

    assert [item["id"] for item in platforms] == ["misskey_prod", "onebot_main"]
    assert platforms[0]["settings"]["base_url"] == "https://misskey.prod/"

    platforms = delete_platform(platforms, "onebot_main")
    assert [item["id"] for item in platforms] == ["misskey_prod"]


def test_platform_upsert_rejects_duplicate_id():
    """修改平台 id 时不能覆盖另一个已有平台"""
    platforms: list[dict[str, Any]] = [
        {"id": "misskey_main", "type": "misskey", "settings": {}},
        {"id": "onebot_main", "type": "onebot", "settings": {}},
    ]

    try:
        upsert_platform(
            platforms,
            {"id": "onebot_main", "type": "misskey", "settings": {}},
            original_id="misskey_main",
        )
    except ValueError as e:
        assert "已存在" in str(e)
    else:
        raise AssertionError("应拒绝重复平台 id")


def test_save_config_document_validates_platforms(tmp_path: Path):
    """
    保存配置前应校验 platforms 结构

    参数:
    - tmp_path: tmp路径
    """
    path = tmp_path / "satrap" / "config.yaml"
    invalid_data: dict[str, Any] = {"platforms": [{"id": "", "type": "misskey", "settings": {}}]}

    try:
        save_config_document(path, invalid_data)
    except ValueError as e:
        assert "id 不能为空" in str(e)
    else:
        raise AssertionError("应拒绝空平台 id")
