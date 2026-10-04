"""已认证控制服务的冷提醒查询, 来源删除与取消"""
from urllib.parse import urlencode
import json
import time

import pytest

from satrap.core.backend import control_server
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.group_chat.reminders import ReminderStore
from satrap.core.storage.layout import StorageLayout
from .test_control_dispatch import _request, _json_body, _use_config


@pytest.mark.asyncio
async def test_authenticated_cold_reminder_read_and_cancel_do_not_change_archives(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("future")
    monkeypatch.setattr(control_server, "_configured_storage_layout", lambda: layout)
    archive = PlatformMessageStore(layout.platform_db("future"), "future")
    scope = MessageScope("future", "bot:/a", "group", "群/中文")
    archive.record(scope, ArchiveMessage("source", "member", time.time(), "请提醒我"))
    reminder = ReminderStore(archive.database).create(scope, actor="member", text="检查结果", mentions=[], source_message_id="source", operation_id="create", time_spec={"after_seconds": 30})["reminder"]
    query = urlencode({"self_id": scope.self_id, "chat_id": scope.chat_id, "conversation_kind": "group"})
    root = "/api/platforms/future/group-chat/reminders"
    assert (await _request(f"{root}?{query}", authorized=False)).startswith(b"HTTP/1.1 401")
    listing = _json_body(await _request(f"{root}?{query}"))
    assert listing["items"][0]["source_status"] == "available"
    target = f"{root}/{reminder['reminder_id']}"
    wrong = urlencode({"self_id": "other", "chat_id": scope.chat_id})
    assert (await _request(f"{target}?{wrong}")).startswith(b"HTTP/1.1 404")
    conflict = await _request(f"{target}/cancel?{query}", "POST", json.dumps({"expected_revision": 99}).encode())
    assert conflict.startswith(b"HTTP/1.1 409")
    cancelled = _json_body(await _request(f"{target}/cancel?{query}", "POST", json.dumps({"expected_revision": 1}).encode()))
    assert cancelled["status"] == "cancelled" and archive.get(scope, "source")["text"] == "请提醒我"
    archive.delete(scope, expected_revision=0, message_ids=["source"])
    detail = _json_body(await _request(f"{target}?{query}"))
    assert detail["reminder"]["source_status"] == "unavailable"
    assert detail["reminder"]["text"] == "检查结果" and detail["reminder"]["state"] == "cancelled"


def test_new_same_named_platform_has_a_new_incarnation_and_updates_preserve_it():
    from satrap.core.config.document import upsert_platform, delete_platform, validate_platforms

    legacy = validate_platforms([{"id": "same", "type": "future", "settings": {}}])[0]
    edited = upsert_platform([legacy], {"id": "same", "type": "future", "settings": {"x": 1}}, original_id="same")[0]
    assert edited["instance_id"] == legacy["instance_id"]
    recreated = upsert_platform(delete_platform([edited], "same"), {"id": "same", "type": "future", "settings": {}})[0]
    assert recreated["instance_id"] != legacy["instance_id"]
