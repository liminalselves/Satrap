"""已认证控制 API 的记忆人工管理与范围隔离"""
from urllib.parse import urlencode
import json
import time

import pytest

from satrap.core.backend import control_server
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.memory.scoped import ScopedMemories
from satrap.core.storage.layout import StorageLayout
from .test_control_dispatch import _request, _json_body, _use_config


@pytest.mark.asyncio
async def test_cold_memory_management_is_authenticated_and_revision_bound(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("future")
    monkeypatch.setattr(control_server, "_configured_storage_layout", lambda: layout)
    archive = PlatformMessageStore(layout.platform_db("future"), "future")
    scope = MessageScope("future", "bot:/a", "group", "群/中文")
    archive.record(scope, ArchiveMessage("source", "user:a", time.time(), "原始消息"))
    query = urlencode({"self_id": scope.self_id, "chat_id": scope.chat_id, "conversation_kind": "group"})
    root = "/api/platforms/future/memory/memories?" + query
    assert (await _request(root, authorized=False)).startswith(b"HTTP/1.1 401")
    payload = {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会", "idempotency_key": "create"}
    response = await _request(root, "POST", json.dumps(payload).encode())
    created = _json_body(response)
    assert created["status"] == "saved"
    memory_id = created["memory_id"]
    assert _json_body(await _request(root))["items"][0]["source_status"] == "operator"
    target = f"/api/platforms/future/memory/memories/{memory_id}?{query}"
    wrong = urlencode({"self_id": "other", "chat_id": scope.chat_id})
    assert (await _request(f"/api/platforms/future/memory/memories/{memory_id}?{wrong}")).startswith(b"HTTP/1.1 404")
    conflict = await _request(target, "PATCH", json.dumps({"content": "新内容", "expected_revision": 2, "idempotency_key": "update"}).encode())
    assert conflict.startswith(b"HTTP/1.1 409")
    delete = json.dumps({"expected_revision": 1, "idempotency_key": "delete"}).encode()
    assert _json_body(await _request(target, "DELETE", delete))["status"] == "deleted"
    assert _json_body(await _request(target, "DELETE", delete))["status"] == "deleted"
    assert archive.get(scope, "source")["text"] == "原始消息"


@pytest.mark.asyncio
async def test_approval_api_returns_applied_result(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("future")
    monkeypatch.setattr(control_server, "_configured_storage_layout", lambda: layout)
    archive = PlatformMessageStore(layout.platform_db("future"), "future")
    scope = MessageScope("future", "bot", "group", "group")
    archive.record(scope, ArchiveMessage("source", "member", time.time(), "周五开会"))
    repository = ScopedMemories(archive, scope)
    proposal = repository.mutate("create", {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会", "source_message_ids": ["source"]},
                                 actor="member", current_message="source", operation_id="propose")
    query = urlencode({"self_id": "bot", "chat_id": "group"})
    root = f"/api/platforms/future/memory/proposals/{proposal['proposal_id']}/decision?{query}"
    result = _json_body(await _request(root, "POST", json.dumps({"approve": True, "expected_revision": 0}).encode()))
    assert result["status"] == "approved" and result["memory"]["content"] == "周五开会"
