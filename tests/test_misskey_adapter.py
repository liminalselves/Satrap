import pytest

from satrap.core.components import File, Image, Plain
from satrap.core.platform import PlatformConfig
from satrap.core.platform.event import MessageChain
from satrap.core.platform.misskey.adapter import MisskeyAdapter
from satrap.core.type import PlatformMessageType


class FakeMisskeyAPI:
    def __init__(self):
        self.calls = []

    async def create_note(self, **kwargs):
        self.calls.append(("create_note", kwargs))
        return {"createdNote": {"id": "note-1"}}

    async def send_message(self, payload):
        self.calls.append(("send_message", payload))
        return {"id": "chat-1"}

    async def send_room_message(self, payload):
        self.calls.append(("send_room_message", payload))
        return {"id": "room-1"}

    async def upload_and_find_file(self, url, name=None, folder_id=None):
        self.calls.append(("upload_and_find_file", {"url": url, "name": name, "folder_id": folder_id}))
        return {"id": "file-url"}

    async def upload_file(self, path, name=None, folder_id=None):
        self.calls.append(("upload_file", {"path": path, "name": name, "folder_id": folder_id}))
        return {"id": "file-local"}

    async def close(self):
        self.calls.append(("close", {}))


def make_adapter(settings=None):
    adapter = MisskeyAdapter(
        PlatformConfig(
            id="mk",
            type="misskey",
            settings=settings
            or {
                "misskey_instance_url": "https://misskey.example",
                "misskey_token": "token",
            },
        )
    )
    adapter.bot_self_id = "bot-id"
    adapter.client_self_id = "bot-id"
    adapter._bot_username = "bot"
    adapter._client = FakeMisskeyAPI()
    return adapter


def test_config_aliases_and_defaults():
    adapter = make_adapter()

    assert adapter._instance_url == "https://misskey.example"
    assert adapter._token == "token"
    assert adapter.max_message_length == 3000
    assert adapter.default_visibility == "public"
    assert adapter.enable_chat is True
    assert adapter.enable_file_upload is True


@pytest.mark.asyncio
async def test_convert_note_message_with_files_and_poll():
    adapter = make_adapter()
    raw = {
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

    api = adapter._client
    assert api.calls[0][0] == "create_note"
    assert api.calls[0][1]["reply_id"] == "note-origin"
    assert api.calls[0][1]["visibility"] == "specified"
    assert api.calls[1] == ("send_message", {"toUserId": "u1", "text": "dm"})
    assert api.calls[2] == ("send_room_message", {"toRoomId": "r1", "text": "room"})


@pytest.mark.asyncio
async def test_send_message_uploads_file_components(tmp_path):
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

    api = adapter._client
    assert api.calls[0][0] == "upload_and_find_file"
    assert api.calls[1][0] == "upload_file"
    assert api.calls[2] == ("send_message", {"toUserId": "u1", "text": "file[图片][文件]", "fileId": "file-url"})
