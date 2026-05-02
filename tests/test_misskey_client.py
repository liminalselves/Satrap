import json

import pytest

from satrap.core.platform.misskey.client import (
    APIError,
    APIRateLimitError,
    AuthenticationError,
    MisskeyAPI,
    StreamingClient,
)


class FakeResponse:
    def __init__(self, status=200, payload=None, text="error"):
        self.status = status
        self.payload = payload if payload is not None else {}
        self._text = text

    async def json(self):
        return self.payload

    async def text(self):
        return self._text


class FakePostContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self):
        self.closed = False
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakePostContext(FakeResponse(payload={"ok": True, "id": "file-1"}))

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_create_note_payload(monkeypatch):
    api = MisskeyAPI("https://misskey.example", "token")
    captured = {}

    async def fake_make_request(endpoint, data):
        captured["endpoint"] = endpoint
        captured["data"] = data
        return {"createdNote": {"id": "note-1"}}

    monkeypatch.setattr(api, "_make_request", fake_make_request)

    result = await api.create_note(
        text="hello",
        visibility="specified",
        visible_user_ids=["u1"],
        file_ids=["f1"],
        local_only=True,
        reply_id="r1",
    )

    assert result["createdNote"]["id"] == "note-1"
    assert captured["endpoint"] == "notes/create"
    assert captured["data"] == {
        "visibility": "specified",
        "localOnly": True,
        "text": "hello",
        "replyId": "r1",
        "visibleUserIds": ["u1"],
        "fileIds": ["f1"],
    }


@pytest.mark.asyncio
async def test_status_error_mapping():
    api = MisskeyAPI("https://misskey.example", "token")

    with pytest.raises(AuthenticationError):
        await api._process_response(FakeResponse(status=401), "i")
    with pytest.raises(APIRateLimitError):
        await api._process_response(FakeResponse(status=429), "i")
    with pytest.raises(APIError):
        await api._process_response(FakeResponse(status=400), "i")


@pytest.mark.asyncio
async def test_upload_file_uses_drive_create(tmp_path):
    path = tmp_path / "demo.txt"
    path.write_text("hello", encoding="utf-8")
    api = MisskeyAPI("https://misskey.example", "token")
    fake_session = FakeSession()
    api._session = fake_session

    result = await api.upload_file(str(path), name="demo.txt", folder_id="folder-1")

    assert result["id"] == "file-1"
    assert fake_session.calls[0][0] == "https://misskey.example/api/drive/files/create"
    assert "data" in fake_session.calls[0][1]


@pytest.mark.asyncio
async def test_streaming_subscribe_and_dispatch():
    sent = []

    class FakeWebSocket:
        async def send(self, message):
            sent.append(json.loads(message))

    streaming = StreamingClient("https://misskey.example", "token")
    streaming.websocket = FakeWebSocket()
    streaming.is_connected = True

    seen = {}

    async def handler(body):
        seen["body"] = body

    channel_id = await streaming.subscribe_channel("main")
    streaming.add_message_handler("main:notification", handler)
    await streaming._handle_message(
        {
            "type": "channel",
            "body": {
                "id": channel_id,
                "type": "notification",
                "body": {"type": "mention"},
            },
        }
    )

    assert sent[0]["type"] == "connect"
    assert sent[0]["body"]["channel"] == "main"
    assert seen["body"] == {"type": "mention"}
