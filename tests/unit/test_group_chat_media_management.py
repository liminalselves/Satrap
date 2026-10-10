"""认证控制服务的真实 multipart 上传, 冷群授权和编辑冲突"""
from urllib.parse import urlencode
from typing import cast
import asyncio
import json
import time
import pytest

from satrap.core.backend import control_server
from satrap.core.storage.layout import StorageLayout
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.config.group_chat_data import parse_sticker_upload
from .test_control_dispatch import _request, _json_body, _use_config, _BufferWriter
from .test_group_chat_assets import png


def multipart(fields=None):
    fields = fields or {"name": "收到", "tags": '["确认"]', "collection": "常用", "idempotency_key": "upload-one"}
    boundary = "satrap-test-boundary"
    body = b""
    for name, value in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="../../任意.png"\r\nContent-Type: image/png\r\n\r\n'.encode("utf-8") + png() + f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


async def upload(authorized=True):
    content_type, body = multipart()
    reader = asyncio.StreamReader()
    header = ("POST /api/group-chat/stickers/upload HTTP/1.1\r\nHost: 127.0.0.1\r\n"
              f"Authorization: Bearer {control_server._CONTROL_AUTH.token if authorized else 'invalid'}\r\n"
              f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
    reader.feed_data(header + body)
    reader.feed_eof()
    writer = _BufferWriter()
    await control_server._handle_request(reader, cast(asyncio.StreamWriter, writer))
    return bytes(writer.data)


def test_multipart_is_utf8_and_never_uses_client_filename():
    content_type, body = multipart()
    metadata, image = parse_sticker_upload(content_type, body)
    assert metadata["name"] == "收到" and metadata["tags"] == ["确认"] and image == png()
    with pytest.raises(ValueError):
        parse_sticker_upload("image/png", body)
    with pytest.raises(ValueError):
        parse_sticker_upload(*multipart({"name": "收到", "tags": "[]", "collection": "常用", "idempotency_key": "one", "path": "C:/secret"}))


@pytest.mark.asyncio
async def test_authenticated_upload_preview_cold_scope_and_delete_replay(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("future")
    monkeypatch.setattr(control_server, "_configured_storage_layout", lambda document=None: layout)
    archive = PlatformMessageStore(layout.platform_db("future"), "future")
    scope = MessageScope("future", "bot:/a", "circle", "群/中文")
    archive.record(scope, ArchiveMessage("source:a", "user:a", time.time(), "真实讨论"))
    assert (await upload(False)).startswith(b"HTTP/1.1 401")
    first = _json_body(await upload())
    assert first["ok"] and first["sticker"]["name"] == "收到"
    repeated = _json_body(await upload())
    assert repeated["replayed"] and repeated["sticker"]["sticker_id"] == first["sticker"]["sticker_id"]
    identity = first["sticker"]["sticker_id"]
    target = "/api/group-chat/stickers/" + identity
    assert (await _request(target + "/preview", authorized=False)).startswith(b"HTTP/1.1 401")
    assert _json_body(await _request(target + "/preview"))["preview"].startswith("data:image/png;base64,")
    query = urlencode({"self_id": scope.self_id, "chat_id": scope.chat_id, "conversation_kind": scope.conversation_kind})
    settings = "/api/platforms/future/group-chat/sticker-settings?" + query
    assert _json_body(await _request(settings))["collections"] == []
    payload = json.dumps({"collections": ["常用"], "expected_revision": 0, "idempotency_key": "intent-enable"}).encode()
    assert _json_body(await _request(settings, "PUT", payload))["revision"] == 1
    assert _json_body(await _request(settings, "PUT", payload))["revision"] == 1
    foreign = settings.replace("bot%3A%2Fa", "bot%3A%2Fb")
    assert (await _request(foreign)).startswith(b"HTTP/1.1 404")
    delete = json.dumps({"expected_revision": 1, "idempotency_key": "intent-delete"}).encode()
    assert _json_body(await _request(target, "DELETE", delete))["deleted"]
    assert _json_body(await _request(target, "DELETE", delete))["replayed"]
    assert (await _request(target + "/preview")).startswith(b"HTTP/1.1 404")
    assert _json_body(await _request("/api/group-chat/stickers"))["items"] == []


@pytest.mark.asyncio
async def test_native_catalog_comes_from_adapter_and_rejects_unknown_mapping(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("ob")
    document = json.loads(control_server.CONFIG_PATH.read_text(encoding="utf-8"))
    document["platforms"] = [{"id": "ob", "type": "onebot", "settings": {}}]
    control_server.CONFIG_PATH.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    archive = PlatformMessageStore(layout.platform_db("ob"), "ob")
    scope = MessageScope("ob", "bot", "group", "群")
    archive.record(scope, ArchiveMessage("source", "user", time.time(), "表情", components=[{"type": "Face", "id": "14"}]))
    catalog = _json_body(await _request("/api/group-chat/native-stickers?platform_id=ob"))
    assert catalog["adapter_type"] == "onebot" and catalog["items"] == [{"key": "face:14", "name": "平台表情 14"}]
    payload = {"platform_id": "ob", "native_key": "face:14", "name": "微笑", "tags": ["友好"], "collection": "常用", "idempotency_key": "native-one"}
    result = _json_body(await _request("/api/group-chat/stickers/native", "POST", json.dumps(payload).encode()))
    assert result["ok"] and result["sticker"]["kind"] == "native" and "native_key" not in result["sticker"]
    response = await _request("/api/group-chat/stickers/native", "POST", json.dumps({**payload, "native_key": "face:9999", "idempotency_key": "native-other"}).encode())
    assert response.startswith(b"HTTP/1.1 400")
