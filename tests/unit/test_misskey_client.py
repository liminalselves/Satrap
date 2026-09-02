import json
import logging
from types import TracebackType
from typing import Any

import pytest
from pathlib import Path

from satrap.core.platform.misskey.client import (
    APIError,
    APIRateLimitError,
    AuthenticationError,
    MisskeyAPI,
    StreamingClient,
)


class FakeResponse:
    def __init__(self, status: int = 200, payload: dict[str, Any] | None = None, text: str = "error"):
        self.status = status
        self.payload: dict[str, Any] = payload if payload is not None else {}
        self._text = text

    async def json(self) -> dict[str, Any]:
        return self.payload

    async def text(self):
        return self._text


class FakePostContext:
    def __init__(self, response: FakeResponse):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None):
        return False


class FakeSession:
    def __init__(self):
        self.closed = False
        self.calls: list[Any] = []

    def post(self, url: str, **kwargs: Any):
        self.calls.append((url, kwargs))
        return FakePostContext(FakeResponse(payload={"ok": True, "id": "file-1"}))

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_create_note_payload(monkeypatch: pytest.MonkeyPatch):
    api = MisskeyAPI("https://misskey.example", "token")
    captured = {}

    async def fake_make_request(endpoint: str, data: dict[str, Any]):
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
        await api._process_response(FakeResponse(status=401), "i")   # type: ignore[arg-type]
    with pytest.raises(APIRateLimitError):
        await api._process_response(FakeResponse(status=429), "i")   # type: ignore[arg-type]
    with pytest.raises(APIError):
        await api._process_response(FakeResponse(status=400), "i")   # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_upload_file_uses_drive_create(tmp_path: Path):
    path = tmp_path / "demo.txt"
    path.write_text("hello", encoding="utf-8")
    api = MisskeyAPI("https://misskey.example", "token")
    fake_session = FakeSession()
    api._session = fake_session   # type: ignore[assignment]

    result = await api.upload_file(str(path), name="demo.txt", folder_id="folder-1")

    assert result["id"] == "file-1"
    assert fake_session.calls[0][0] == "https://misskey.example/api/drive/files/create"
    assert "data" in fake_session.calls[0][1]


@pytest.mark.asyncio
async def test_streaming_subscribe_and_dispatch():
    sent: list[Any] = []

    class FakeWebSocket:
        async def send(self, message: str):
            sent.append(json.loads(message))

    streaming = StreamingClient("https://misskey.example", "token")
    streaming.websocket = FakeWebSocket()
    streaming.is_connected = True

    seen: dict[str, Any] = {}

    async def handler(body: dict[str, Any]):
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


@pytest.mark.asyncio
async def test_streaming_connection_encodes_access_token(monkeypatch: pytest.MonkeyPatch):
    """
    Streaming URL 应编码 token 中的查询分隔符

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    captured: dict[str, object] = {}

    class FakeWebSocket:
        """最小 WebSocket 连接替身"""

    async def fake_connect(url: str, **kwargs: object) -> FakeWebSocket:
        """
        记录 WebSocket 连接参数

        参数:
        - url: 连接地址
        - kwargs: 底层客户端关键字参数

        返回:
        - 最小 WebSocket 连接替身
        """
        captured["url"] = url
        captured.update(kwargs)
        return FakeWebSocket()

    monkeypatch.setattr("satrap.core.platform.misskey.client.websockets.connect", fake_connect)
    streaming = StreamingClient("https://misskey.example", "a&admin=true")

    assert await streaming.connect() is True
    assert captured["url"] == "wss://misskey.example/streaming?i=a%26admin%3Dtrue"
    websocket_logger = captured["logger"]
    assert isinstance(websocket_logger, logging.Logger)

    class _CaptureHandler(logging.Handler):
        """收集过滤后的日志文本"""

        def __init__(self) -> None:
            """初始化过滤后日志文本缓冲区"""
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            """
            保存一条过滤后的日志记录

            参数:
            - record: 日志记录
            """
            self.messages.append(record.getMessage())

    handler = _CaptureHandler()
    websocket_logger.addHandler(handler)
    websocket_logger.warning("连接地址: %s", captured["url"])
    assert handler.messages == [
        "连接地址: wss://misskey.example/streaming?i=[REDACTED]"
    ]


@pytest.mark.asyncio
async def test_streaming_disconnect_clears_runtime_and_controls_resubscribe_state():
    """瞬时断连保留订阅意图, 显式断开同时清除运行态与订阅意图"""
    class FakeWebSocket:
        async def close(self) -> None:
            """模拟关闭 WebSocket 连接"""
            return None

    streaming = StreamingClient("https://misskey.example", "token")
    streaming.websocket = FakeWebSocket()
    streaming.is_connected = True
    streaming.channels["old-id"] = "main"
    streaming.desired_channels["main"] = {"withReplies": True}

    await streaming.disconnect(clear_subscriptions=False)
    assert streaming.channels == {}
    assert streaming.desired_channels == {"main": {"withReplies": True}}

    streaming.websocket = FakeWebSocket()
    streaming.is_connected = True
    streaming.channels["new-id"] = "main"
    await streaming.disconnect()
    assert streaming.channels == {}
    assert streaming.desired_channels == {}


@pytest.mark.asyncio
async def test_insecure_download_fallback_is_limited_to_instance_origin(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    不安全 TLS 回退不得应用到任意外部媒体 URL

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    api = MisskeyAPI(
        "https://misskey.example",
        "token",
        allow_insecure_downloads=True,
    )
    calls: list[bool] = []

    async def fail_download(url: str, ssl_verify: bool = True) -> bytes:
        """
        记录 TLS 校验选项并模拟下载失败

        参数:
        - url: 下载地址
        - ssl_verify: 是否验证 TLS 证书

        返回:
        - 不返回结果, 始终抛出 APIError
        """
        calls.append(ssl_verify)
        raise APIError("TLS failure")

    monkeypatch.setattr(api, "_download_bytes", fail_download)

    assert await api.upload_and_find_file("https://cdn.example/file.png") is None
    assert calls == [True]
