"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

import pytest
from typing import Any, cast
from satrap.core.platform.misskey.adapter import MisskeyAdapter
from satrap.core.components import File, Image
from satrap.core.platform import PlatformConfig

class FakeMisskeyAPI:
    def __init__(self):
        self.calls: list[Any] = []

    async def create_note(self, **kwargs: Any):
        self.calls.append(("create_note", kwargs))
        return {"createdNote": {"id": "note-1"}}

    async def send_message(self, payload: dict[str, Any]):
        self.calls.append(("send_message", payload))
        return {"id": "chat-1"}

    async def send_room_message(self, payload: dict[str, Any]):
        self.calls.append(("send_room_message", payload))
        return {"id": "room-1"}

    async def upload_and_find_file(self, url: str, name: str | None = None, folder_id: str | None = None):
        self.calls.append(("upload_and_find_file", {"url": url, "name": name, "folder_id": folder_id}))
        return {"id": "file-url"}

    async def upload_file(self, path: str, name: str | None = None, folder_id: str | None = None):
        self.calls.append(("upload_file", {"path": path, "name": name, "folder_id": folder_id}))
        return {"id": "file-local"}

    async def close(self):
        self.calls.append(("close", {}))

def make_adapter(settings: dict[str, Any] | None = None):
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
    adapter._client = cast(Any, FakeMisskeyAPI())
    return adapter
