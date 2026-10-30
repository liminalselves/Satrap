"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
import asyncio
import pytest
from typing import Any, cast
from satrap.core.platform.event import MessageChain
from satrap.core.components import File, Image, Plain
from satrap.core.type import PlatformMessageType
from support.misskey import FakeMisskeyAPI, make_adapter
import aiohttp
import logging
from types import TracebackType
import threading
import json
from satrap.core.platform.misskey import client as client_module
from satrap.core.platform.misskey.client import APIError, APIRateLimitError, AuthenticationError, MisskeyAPI
from satrap.core.platform.misskey.client import StreamingClient


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['room', 'chat', 'note'])
async def test_real_publish_receipts_keep_errors_and_never_retry_unknown_submission(tmp_path, kind):
    """
    实际 HTTP 覆盖三种发布载体, 成功归一回执, 错误保留详情且不重试

    参数:
    - tmp_path: 独立的临时目录
    - kind: 房间, 私聊或帖子载体
    """
    from aiohttp import web
    from satrap.core.utils import paths
    from unittest.mock import patch, AsyncMock
    from satrap.core.platform.misskey.client import APIConnectionError
    counts, uploads = [], []
    state = {'mode': 'success'}
    async def handler(request):
        endpoint = request.match_info['endpoint']
        if endpoint == 'drive/files/create':
            reader = await request.multipart()
            async for part in reader:
                if part.name == 'file':
                    uploads.append(await part.read())
            if state['mode'] == 'upload_failed':
                return web.json_response({'error': {'code': 'INVALID_FILE', 'message': '图片格式被拒绝'}}, status=400)
            return web.json_response({'id': 'drive-image'})
        counts.append(endpoint)
        if state['mode'] in ('rejected', 'unknown'):
            return web.json_response({'error': {'code': 'TEST_ERROR', 'message': '模拟错误 token=hidden-secret'}}, status=400 if state['mode'] == 'rejected' else 503)
        message = {'id': 'confirmed'}
        return web.json_response({'createdNote': message} if endpoint == 'notes/create' else message)
    app = web.Application()
    app.router.add_post('/api/{endpoint:.*}', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    api = MisskeyAPI(f'http://127.0.0.1:{port}', 'isolated-credential')
    adapter = make_adapter()
    adapter._client = api
    source = tmp_path / 'image.png'
    source.write_bytes(b'isolated-image-bytes')
    target = {'room': 'room%r1', 'chat': 'chat%u1', 'note': 'note%u1'}[kind]
    try:
        with patch.object(paths, '_configured_media_roots', (str(tmp_path),)):
            for mode in ('success', 'rejected', 'unknown', 'upload_failed'):
                state['mode'] = mode
                previous = len(counts)
                message = MessageChain([Plain('hello'), Image(file=str(source))]) if mode == 'upload_failed' else MessageChain.from_text('hello')
                result = await adapter.send_message(target, message)
                assert result.status == {'success': 'success', 'rejected': 'failed', 'unknown': 'unknown', 'upload_failed': 'failed'}[mode]
                assert len(counts) - previous == (0 if mode == 'upload_failed' else 1)
                if mode == 'success':
                    assert result.message_ids == ('confirmed',)
                elif mode in ('rejected', 'unknown'):
                    assert result.error_code == 'TEST_ERROR' and '模拟错误' in result.error_message
                    assert 'hidden-secret' not in result.error_message and result.stage == 'publish'
                else:
                    assert result.error_code == 'INVALID_FILE' and result.stage == 'upload'
            assert uploads == [source.read_bytes()]
        attempts = []
        async def lost_response(endpoint, data=None):
            attempts.append(endpoint)
            raise APIConnectionError('模拟提交后回执丢失')
        with patch.object(api, 'request_once', lost_response), patch('satrap.core.platform.misskey.client.asyncio.sleep', new=AsyncMock()):
            result = await adapter.send_message(target, MessageChain.from_text('once'))
        assert result.status == 'unknown' and len(attempts) == 1
        async def changed_upload(message):
            adapter.bot_self_id = 'replacement-account'
            return ['drive-image']
        with patch.object(adapter, '_collect_file_ids', changed_upload):
            result = await adapter.send_message(target, MessageChain.from_text('stale'))
        assert result.status == 'failed' and result.reason == 'stale_call'
        assert len(attempts) == 1
    finally:
        await api.close()
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["private", "group", "discussion"])
async def test_file_delivery_streams_to_drive_then_publishes_once_to_original_conversation(tmp_path, kind):
    """真实 multipart 传输与原生发布, 上传或发布失败不能被文字回复掩盖"""
    from aiohttp import web
    from satrap.core.call_context import CallOrigin
    from satrap.core.file_access.artifacts import freeze_file
    body = bytes(range(256)) * 1100
    posted, transferred = [], []
    mode = ["success"]
    async def upload(request):
        from urllib.parse import unquote
        reader = await request.multipart()
        data = bytearray()
        while part := await reader.next():
            if part.name == "file":
                assert unquote(part.filename) == "文档.zip"
                while chunk := await part.read_chunk():
                    data.extend(chunk)
        transferred.append(bytes(data))
        if mode[0] == "upload_failed":
            return web.json_response({"error": {"code": "INVALID_FILE", "message": "rejected"}}, status=400)
        return web.json_response({"id": "drive-file"})
    async def publish(request):
        data = await request.json()
        posted.append((request.path, data))
        if mode[0] == "publish_failed":
            return web.json_response({"error": {"code": "INVALID_PARAM", "message": "rejected"}}, status=400)
        return web.json_response({"createdNote": {"id": "message"}} if kind == "discussion" else {"id": "message"})
    app = web.Application()
    app.router.add_post("/api/drive/files/create", upload)
    for endpoint in ("chat/messages/create-to-user", "chat/messages/create-to-room", "notes/create"):
        app.router.add_post("/api/" + endpoint, publish)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = cast(Any, site._server).sockets[0].getsockname()[1]
    adapter = make_adapter()
    adapter._running = True
    client = MisskeyAPI(f"http://127.0.0.1:{port}", "local-test-token")
    adapter._client = client
    source = tmp_path / "file.zip"
    source.write_bytes(body)
    artifact = freeze_file(source, tmp_path / "artifacts", "文档.zip")
    origin = CallOrigin("mk", "bot-id", "GroupMessage" if kind == "group" else "FriendMessage" if kind == "private" else "OtherMessage",
                        "room" if kind == "group" else "user", "user", "source-note", "delivery", conversation_kind=kind)
    try:
        result = await adapter.deliver_file(origin, artifact, lambda: None)
        assert result["ok"] and result["status"] == "published", result
        assert transferred == [body] and len(posted) == 1
        endpoint, payload = posted[0]
        if kind == "discussion":
            assert endpoint.endswith("notes/create") and payload["replyId"] == "source-note"
            assert payload["fileIds"] == ["drive-file"]
        else:
            field = "toRoomId" if kind == "group" else "toUserId"
            assert payload[field] == origin.chat_id and payload["fileId"] == "drive-file"
        mode[0] = "upload_failed"
        result = await adapter.deliver_file(origin, artifact, lambda: None)
        assert not result["ok"] and len(posted) == 1
        mode[0] = "publish_failed"
        result = await adapter.deliver_file(origin, artifact, lambda: None)
        assert not result["ok"] and result["status"] == "failed" and len(posted) == 2
        adapter.enable_file_upload = False
        assert (await adapter.deliver_file(origin, artifact, lambda: None))["status"] == "failed"
        assert len(transferred) == 3
    finally:
        await client.close()
        await runner.cleanup()

def test_config_frontend_aliases():
    adapter = make_adapter({"base_url": "https://misskey.example", "api_token": "token", "chat_enabled": False, "room_enabled": False})

    assert adapter.enable_chat is False
    assert adapter.enable_room is False

@pytest.mark.asyncio
async def test_convert_note_message_with_files_and_poll():
    adapter = make_adapter()
    raw: dict[str, Any] = {
        "id": "note-1",
        "text": "@bot hello",
        "visibility": "specified",
        "visibleUserIds": ["sender-id"],
        "user": {"id": "sender-id", "username": "alice", "name": "Alice"},
        "files": [
            {"url": "https://cdn.example/a.png", "name": "a.png", "type": "image/png"},
            {"url": "https://cdn.example/a.txt", "name": "a.txt", "type": "text/plain"},
        ],
        "poll": {"multiple": False, "choices": [{"text": "yes", "votes": 1}]},
    }

    message = await adapter.convert_message(raw)

    assert message.type == PlatformMessageType.OTHER_MESSAGE
    assert message.session_id == "note%sender-id"
    assert message.sender.nickname == "Alice"
    assert any(isinstance(comp, Image) for comp in message.message)
    assert any(isinstance(comp, File) for comp in message.message)
    assert "[投票]" in message.message_str
    assert adapter._user_cache["sender-id"]["reply_to_note_id"] == "note-1"

@pytest.mark.asyncio
async def test_convert_chat_and_room_messages():
    adapter = make_adapter()
    chat = await adapter.convert_chat_message(
        {
            "id": "chat-1",
            "text": "hello",
            "fromUserId": "u1",
            "fromUser": {"id": "u1", "username": "bob"},
        }
    )
    room = await adapter.convert_room_message(
        {
            "id": "room-msg-1",
            "text": "room hello",
            "fromUserId": "u2",
            "fromUser": {"id": "u2", "username": "carol"},
            "toRoomId": "room-1",
            "toRoom": {"name": "Room"},
        }
    )

    assert chat.type == PlatformMessageType.FRIEND_MESSAGE
    assert chat.session_id == "chat%u1"
    assert room.type == PlatformMessageType.GROUP_MESSAGE
    assert room.session_id == "room%room-1"
    assert room.group is not None
    assert room.group.group_id == "room-1"

@pytest.mark.asyncio
async def test_send_message_routes_note_chat_room():
    adapter = make_adapter()
    adapter._user_cache["u1"] = {
        "username": "alice",
        "visibility": "specified",
        "visible_user_ids": ["u1"],
        "reply_to_note_id": "note-origin",
    }

    await adapter.send_message("note%u1", MessageChain([Plain("reply")]))
    await adapter.send_message("chat%u1", MessageChain([Plain("dm")]))
    await adapter.send_message("room%r1", MessageChain([Plain("room")]))

    api = cast(FakeMisskeyAPI, adapter._client)
    assert api.calls[0][0] == "create_note"
    assert api.calls[0][1]["reply_id"] == "note-origin"
    assert api.calls[0][1]["visibility"] == "specified"
    assert api.calls[1] == ("send_message", {"toUserId": "u1", "text": "dm"})
    assert api.calls[2] == ("send_room_message", {"toRoomId": "r1", "text": "room"})

@pytest.mark.asyncio
async def test_send_message_uploads_file_components(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SATRAP_EXTRA_MEDIA_ROOTS", str(tmp_path))
    adapter = make_adapter()
    path = tmp_path / "demo.txt"
    path.write_text("hello", encoding="utf-8")

    await adapter.send_message(
        "chat%u1",
        MessageChain(
            [
                Plain("file"),
                Image(file="https://cdn.example/a.png", url="https://cdn.example/a.png"),
                File(name="demo.txt", file=str(path)),
            ]
        ),
    )

    api = cast(FakeMisskeyAPI, adapter._client)
    assert api.calls[0][0] == "upload_and_find_file"
    assert api.calls[1][0] == "upload_file"
    assert api.calls[2] == ("send_message", {"toUserId": "u1", "text": "file[图片][文件]", "fileId": "file-url"})

def _fake_response(status: int) -> aiohttp.ClientResponse:
    """FakeResponse 替身类型边界: 替身实现 status/json/text 调用面, cast 集中在此工厂"""
    return cast(aiohttp.ClientResponse, FakeResponse(status=status))

class FakeResponse:
    def __init__(self, status: int = 200, payload: dict[str, Any] | None = None, text: str = "error"):
        self.status = status
        self.payload: dict[str, Any] = payload if payload is not None else {}
        self._text = text

    async def json(self) -> dict[str, Any]:
        return self.payload

    async def text(self):
        return self._text

class AsyncBodyRecorder:
    """异步收集 multipart 请求体, 用于核对实际上传内容"""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    async def write(self, data: bytes) -> None:
        """记录一段请求体"""
        self.chunks.append(bytes(data))

async def multipart_body(form: Any) -> bytes:
    """序列化 FormData 为请求体字节"""
    recorder = AsyncBodyRecorder()
    await form().write(recorder)
    return b"".join(recorder.chunks)

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

    monkeypatch.setattr(api, "request_once", fake_make_request)

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
        await api._process_response(_fake_response(401), "i")
    with pytest.raises(APIRateLimitError):
        await api._process_response(_fake_response(429), "i")
    with pytest.raises(APIError):
        await api._process_response(_fake_response(400), "i")

@pytest.mark.asyncio
async def test_upload_file_uses_drive_create_and_reads_disk_off_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """drive/files/create 上传真实内容, 且磁盘读取在工作线程执行, 事件循环线程不读盘"""
    path = tmp_path / "demo.txt"
    path.write_text("hello", encoding="utf-8")
    api = MisskeyAPI("https://misskey.example", "token")
    fake_session = FakeSession()
    monkeypatch.setattr(api, "_session", fake_session)
    threads: list[threading.Thread] = []
    real_read = client_module._read_file_bytes

    def recording_read(file_path: str) -> bytes:
        """记录读取发生的线程"""
        threads.append(threading.current_thread())
        return real_read(file_path)

    monkeypatch.setattr(client_module, "_read_file_bytes", recording_read)

    result = await api.upload_file(str(path), name="demo.txt", folder_id="folder-1")

    assert result["id"] == "file-1"
    assert fake_session.calls[0][0] == "https://misskey.example/api/drive/files/create"
    body = await multipart_body(fake_session.calls[0][1]["data"])
    assert b'filename="demo.txt"' in body
    assert b"Content-Type: text/plain" in body
    assert b"\r\nhello\r\n" in body
    assert threads and threads[0] is not threading.main_thread()

@pytest.mark.asyncio
async def test_upload_file_missing_path_reports_api_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """文件缺失仍映射为 APIError, 不泄漏 FileNotFoundError"""
    api = MisskeyAPI("https://misskey.example", "token")
    monkeypatch.setattr(api, "_session", FakeSession())
    with pytest.raises(APIError, match="File not found"):
        await api.upload_file(str(tmp_path / "missing.txt"))
    with pytest.raises(APIError, match="No file path"):
        await api.upload_file("")

@pytest.mark.asyncio
async def test_upload_and_find_file_uploads_downloaded_bytes_without_temp_file(monkeypatch: pytest.MonkeyPatch):
    """下载内容直接上传: 不再落临时文件, Drive 文件名取自 URL"""
    api = MisskeyAPI("https://misskey.example", "token")
    fake_session = FakeSession()
    monkeypatch.setattr(api, "_session", fake_session)

    async def fake_download(url: str, ssl_verify: bool = True) -> bytes:
        """模拟下载成功"""
        return b"image-bytes"

    monkeypatch.setattr(api, "_download_bytes", fake_download)

    result = await api.upload_and_find_file("https://cdn.example/files/photo.png?token=1")

    assert result is not None and result["id"] == "file-1"
    body = await multipart_body(fake_session.calls[0][1]["data"])
    assert b'filename="photo.png"' in body
    assert b"Content-Type: image/png" in body
    assert b"image-bytes" in body

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
    """Streaming URL 应编码 token 中的查询分隔符"""
    captured: dict[str, object] = {}

    class FakeWebSocket:
        """最小 WebSocket 连接替身"""

    async def fake_connect(url: str, **kwargs: object) -> FakeWebSocket:
        """记录 WebSocket 连接参数"""
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
            """保存一条过滤后的日志记录"""
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
    """不安全 TLS 回退不得应用到任意外部媒体 URL"""
    api = MisskeyAPI(
        "https://misskey.example",
        "token",
        allow_insecure_downloads=True,
    )
    calls: list[bool] = []

    async def fail_download(url: str, ssl_verify: bool = True) -> bytes:
        """记录 TLS 校验选项并模拟下载失败"""
        calls.append(ssl_verify)
        raise APIError("TLS failure")

    monkeypatch.setattr(api, "_download_bytes", fail_download)

    assert await api.upload_and_find_file("https://cdn.example/file.png") is None
    assert calls == [True]
