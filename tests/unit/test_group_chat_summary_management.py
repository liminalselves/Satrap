from urllib.parse import urlencode
import json

import pytest

from satrap.core.backend import control_server
from satrap.core.storage.layout import StorageLayout
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.group_chat.summaries import SummaryStore
from .test_control_dispatch import _request, _json_body, _use_config


@pytest.mark.asyncio
async def test_authenticated_cold_summary_api_scope_revision_and_delete_retry(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("future")
    monkeypatch.setattr(control_server, "_configured_storage_layout", lambda: layout)
    archive = PlatformMessageStore(layout.platform_db("future"), "future")
    scope = MessageScope("future", "bot:/a", "group", "群/中文")
    import time
    archive.record(scope, ArchiveMessage("source:a", "user:a", time.time(), "真实讨论"))
    store = SummaryStore(archive)
    snapshot = store.prepare(scope, "request:a", start_time="2020-01-01T00:00:00+08:00", end_time="2030-01-01T00:00:00+08:00")
    saved = store.save(scope, "request:a", snapshot["snapshot_id"], "讨论摘要", [{"text": "真实概括", "source_message_ids": ["source:a"]}])["summary"]
    query = urlencode({"self_id": scope.self_id, "chat_id": scope.chat_id, "conversation_kind": scope.conversation_kind})
    path = "/api/platforms/future/group-chat/summaries"
    page = _json_body(await _request(path + "?" + query))
    assert b"401" in (await _request(path + "?" + query, authorized=False)).split(b"\r\n", 1)[0]
    assert page["items"][0]["summary_id"] == saved["summary_id"]
    wrong = urlencode({"self_id": "other-bot", "chat_id": scope.chat_id})
    response = await _request(path + "/" + saved["summary_id"] + "?" + wrong)
    assert response.startswith(b"HTTP/1.1 404")
    target = path + "/" + saved["summary_id"] + "?" + query
    conflict = await _request(target, "DELETE", json.dumps({"expected_revision": 2, "idempotency_key": "intent-a"}).encode())
    assert _json_body(conflict)["code"] == "revision_conflict"
    payload = json.dumps({"expected_revision": 1, "idempotency_key": "intent-a"}).encode()
    first = _json_body(await _request(target, "DELETE", payload))
    repeated = _json_body(await _request(target, "DELETE", payload))
    assert first["status"] == repeated["status"] == "deleted" and repeated["replayed"]
    assert _json_body(await _request(path + "?" + query))["items"] == []
    assert archive.get(scope, "source:a")["text"] == "真实讨论"
