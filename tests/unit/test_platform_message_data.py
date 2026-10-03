"""档案管理接口的身份隔离, 动态目录与认证边界"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
from time import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from satrap.core.backend import control_server as control
from satrap.core.config.platform_message_data import PlatformMessageDataService, platform_archive_catalog
from satrap.core.config.platform_messages import ArchiveMessage, MessageArchiveError, MessageScope, PlatformMessageStore
from satrap.core.platform import registry
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.storage.layout import StorageLayout


class FutureAdapter(OneBotAdapter):
    """只声明额外对话类型, 禁止目录实例化适配器"""

    conversation_kinds = {"circle": "圈子讨论"}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("只读目录不能创建适配器或连接平台")


@pytest.fixture
def saved(tmp_path: Path, monkeypatch):
    document = {"data_root": str(tmp_path / "data"), "platforms": [
        {"id": "alpha", "type": "future"}, {"id": "beta", "type": "future"}]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    monkeypatch.setitem(registry._mapping, "future", FutureAdapter)
    layout = StorageLayout(tmp_path / "data")
    scopes = [MessageScope("alpha", "bot-1", "circle", "会话 A"), MessageScope("alpha", "bot-2", "circle", "会话 A"),
              MessageScope("beta", "bot-1", "circle", "会话 B")]
    for scope in scopes:
        layout.ensure_platform(scope.adapter_id, platform_type="future")
        store = PlatformMessageStore(layout.platform_db(scope.adapter_id), scope.adapter_id, retention_days=7)
        store.record(scope, ArchiveMessage("same", "member", time(), "真实原文", nickname="昵称"), label="讨论 % _")
    with sqlite3.connect(layout.platform_db("alpha")) as connection:
        connection.execute("CREATE TABLE chat_history(content TEXT)")
        connection.execute("INSERT INTO chat_history VALUES('模型上下文')")
    return layout, document, scopes


def _payload(scope: MessageScope, **values: Any) -> dict[str, Any]:
    """
    将明确选中的档案身份组成管理请求

    参数:
    - scope: 测试档案范围
    - values: 动作参数

    返回:
    - 不包含数据库路径的请求
    """
    return {"platform_id": scope.adapter_id, "self_id": scope.self_id, "conversation_kind": scope.conversation_kind,
            "chat_id": scope.chat_id, **values}


async def _request(path: str, payload: dict[str, Any] | None = None, *, authorized: bool = True) -> tuple[int, dict[str, Any]]:
    """
    经完整认证与路由分派读取控制响应, 不启动外部服务器

    参数:
    - path: 控制服务路径
    - payload: JSON 请求体, None 表示 GET
    - authorized: 是否携带控制令牌

    返回:
    - HTTP 状态和 JSON 响应
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    method = "POST" if payload is not None else "GET"
    headers = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
               f"Authorization: Bearer {control._CONTROL_AUTH.token if authorized else 'invalid'}\r\n"
               f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("utf-8")
    reader = asyncio.StreamReader()
    reader.feed_data(headers + body)
    reader.feed_eof()
    response = bytearray()
    writer = SimpleNamespace(write=response.extend, drain=AsyncMock(), close=Mock())
    await control._handle_request(reader, cast(asyncio.StreamWriter, writer))
    header, content = bytes(response).split(b"\r\n\r\n", 1)
    return int(header.split()[1]), json.loads(content.decode("utf-8"))


def test_catalog_has_dynamic_kinds_accounts_and_cross_platform_paging(saved) -> None:
    layout, document, _ = saved
    catalog = platform_archive_catalog(layout, document, {"limit": "2"})
    assert catalog["total"] == 3 and len(catalog["items"]) == 2
    assert catalog["conversation_kinds"] == [{"value": "circle", "label": "圈子讨论"}]
    assert catalog["self_ids"] == ["bot-1", "bot-2"]
    second = platform_archive_catalog(layout, document, {"offset": "2", "limit": "2"})
    assert len(second["items"]) == 1 and second["items"][0]["platform_id"] == "beta"
    selected = platform_archive_catalog(layout, document, {"platform_id": "alpha", "self_id": "bot-2", "q": "% _"})
    assert selected["total"] == 1 and selected["items"][0]["message_count"] == 1
    assert platform_archive_catalog(layout, document, {"q": "%%%%"})["total"] == 0


@pytest.mark.asyncio
async def test_archive_http_read_search_and_message_details(saved) -> None:
    _, _, scopes = saved
    status, catalog = await _request("/config/conversations/archive?platform_id=alpha&conversation_kind=circle")
    assert status == 200 and catalog["total"] == 2
    status, data = await _request("/config/conversations/archive/data", _payload(scopes[0], keyword="原文", sender_id="member"))
    assert status == 200 and data["items"][0]["message_id"] == "same"
    assert data["retention_days"] == 7 and data["scope"]["self_id"] == "bot-1"
    assert data["backups"] == [] and data["coverage"]["complete"] is False
    status, message = await _request("/config/conversations/archive/data", _payload(scopes[1], action="message", message_id="same"))
    assert status == 200 and message["item"]["nickname"] == "昵称"
    status, missing = await _request("/config/conversations/archive/data", _payload(scopes[0], action="message", message_id="other"))
    assert status == 404 and missing["code"] == "not_found"


@pytest.mark.asyncio
async def test_delete_clear_restore_and_revision_preserve_other_scopes_and_context(saved) -> None:
    layout, _, scopes = saved
    route = "/config/conversations/archive/data"
    status, deleted = await _request(route, _payload(scopes[0], action="delete", message_ids=["same"], expected_revision=0))
    assert status == 200 and deleted["deleted_count"] == 1
    status, state = await _request(route, _payload(scopes[0]))
    assert status == 200 and state["items"] == [] and state["revision"] == 1
    assert set(state["backups"][0]) == {"backup_id", "action", "created_at", "expires_at"}
    for scope in scopes[1:]:
        status, data = await _request(route, _payload(scope))
        assert status == 200 and data["items"][0]["text"] == "真实原文"
    status, conflict = await _request(route, _payload(scopes[0], action="clear", expected_revision=0))
    assert status == 409 and conflict["code"] == "revision_conflict"
    status, restored = await _request(route, _payload(scopes[0], action="restore", backup_id=deleted["backup_id"], expected_revision=1))
    assert status == 200 and restored["restored_count"] == 1
    status, cleared = await _request(route, _payload(scopes[0], action="clear", expected_revision=2))
    assert status == 200 and cleared["deleted_count"] == 1
    status, message = await _request(route, _payload(scopes[0], action="message", message_id="same"))
    assert status == 200 and message["item"]["status"] == "deleted" and message["item"]["text"] == ""
    status, cross_scope = await _request(route, _payload(scopes[1], action="restore", backup_id=cleared["backup_id"], expected_revision=0))
    assert status == 404 and cross_scope["code"] == "backup_not_found"
    with sqlite3.connect(layout.platform_db("alpha")) as connection:
        assert connection.execute("SELECT content FROM chat_history").fetchone()[0] == "模型上下文"


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"action": "edit", "text": "篡改"}, {"action": "read", "database": "other.db"},
                                    {"action": "delete", "expected_revision": 0, "message_ids": []},
                                    {"action": "delete", "message_ids": ["same"]}, {"action": "restore", "expected_revision": 0},
                                    {"action": "message"}, {"action": "read", "limit": True},
                                    {"action": "read", "start_time": "2026-10-03T01:00:00"},
                                    {"action": "clear", "expected_revision": True}])
async def test_invalid_management_requests_are_explicit(saved, extra: dict[str, Any]) -> None:
    _, _, scopes = saved
    status, error = await _request("/config/conversations/archive/data", _payload(scopes[0], **extra))
    assert status == 400 and error["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_unauthorized_and_unknown_platform_requests_do_not_mutate(saved) -> None:
    layout, _, scopes = saved
    status, _ = await _request("/config/conversations/archive", authorized=False)
    assert status == 401
    status, _ = await _request("/config/conversations/archive/data", _payload(scopes[0], action="clear", expected_revision=0), authorized=False)
    assert status == 401
    status, _ = await _request("/config/conversations/archive/data", {**_payload(scopes[0]), "platform_id": "../../arbitrary"})
    assert status == 404 and not layout.platform_db("../../arbitrary").exists()
    status, _ = await _request("/config/conversations/archive/data", {**_payload(scopes[0]), "chat_id": "unknown"})
    assert status == 404
    status, data = await _request("/config/conversations/archive/data", _payload(scopes[0]))
    assert status == 200 and data["revision"] == 0 and len(data["items"]) == 1


@pytest.mark.asyncio
async def test_corrupt_archive_is_partial_warning_for_all_and_error_for_selected(saved, caplog) -> None:
    layout, _, _ = saved
    layout.platform_db("alpha").write_bytes(b"corrupt")
    status, catalog = await _request("/config/conversations/archive")
    assert status == 200 and catalog["total"] == 1
    assert catalog["warnings"] == [{"platform_id": "alpha", "error": "archive_unavailable"}]
    status, error = await _request("/config/conversations/archive?platform_id=alpha")
    assert status == 503 and error["code"] == "archive_unavailable"
    assert "目录读取失败" in caplog.text and "管理操作失败" in caplog.text


def test_management_read_respects_concurrent_deletion_revision(saved, monkeypatch) -> None:
    layout, _, scopes = saved
    scope = scopes[0]
    store = PlatformMessageStore(layout.platform_db(scope.adapter_id), scope.adapter_id)
    original = store.query

    def raced_query(*args, **kwargs) -> dict[str, Any]:
        store.delete(scope, expected_revision=0)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "query", raced_query)
    with pytest.raises(MessageArchiveError, match="状态发生变化"):
        PlatformMessageDataService(store).operate({key: value for key, value in _payload(scope).items() if key != "platform_id"})


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("components_json", "{"), ("components_json", "[1]"),
                                         ("mentions_json", "{}"), ("media_json", '"invalid"')])
async def test_corrupt_component_data_is_storage_failure_then_service_remains_usable(saved, field: str, value: str) -> None:
    layout, _, scopes = saved
    with sqlite3.connect(layout.platform_db("alpha")) as connection:
        connection.execute(f"UPDATE platform_messages SET {field}=? WHERE scope_key=?", (value, scopes[0].key))
    status, error = await _request("/config/conversations/archive/data", _payload(scopes[0]))
    assert status == 503 and error["code"] == "archive_unavailable"
    status, data = await _request("/config/conversations/archive/data", _payload(scopes[1]))
    assert status == 200 and data["items"][0]["text"] == "真实原文"
