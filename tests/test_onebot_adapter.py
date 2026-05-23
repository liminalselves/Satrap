from __future__ import annotations

import pytest

from satrap.core.components import At, AtAll, Face, Image, Json, Plain, Reply, Unknown
from satrap.core.platform import PlatformAdapterManager, PlatformConfig, registry
from satrap.core.platform.event import MessageChain
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.onebot.onebot_utils import (
    create_platform_message,
    group_session_id,
    message_chain_to_onebot_segments,
    onebot_segments_to_components,
    private_session_id,
)
from satrap.core.type import PlatformMessageType


class FakeOneBotClient:
    def __init__(self):
        self.calls = []

    async def send_private_msg(self, **kwargs):
        self.calls.append(("send_private_msg", kwargs))
        return {"message_id": 1}

    async def send_group_msg(self, **kwargs):
        self.calls.append(("send_group_msg", kwargs))
        return {"message_id": 2}


def make_adapter(settings=None) -> OneBotAdapter:
    adapter = OneBotAdapter(
        PlatformConfig(
            id="onebot_main",
            type="onebot",
            settings=settings or {"host": "127.0.0.1", "port": 6700},
        )
    )
    adapter.bot_self_id = "10000"
    adapter.client_self_id = "10000"
    adapter._bot = FakeOneBotClient()
    return adapter


def test_onebot_and_aiocqhttp_aliases_are_registered():
    """onebot 和 aiocqhttp 都能创建同一个适配器类"""
    assert registry.get("onebot") is OneBotAdapter
    assert registry.get("aiocqhttp") is OneBotAdapter

    manager = PlatformAdapterManager(registry=registry)
    first = manager.add_adapter(PlatformConfig(id="ob1", type="onebot", settings={}))
    second = manager.add_adapter(PlatformConfig(id="ob2", type="aiocqhttp", settings={}))

    assert isinstance(first, OneBotAdapter)
    assert isinstance(second, OneBotAdapter)
    assert manager.list_adapters() == ["ob1", "ob2"]


def test_config_defaults_and_aliases():
    adapter = make_adapter({"listen_host": "0.0.0.0", "listen_port": 8081})

    assert adapter.host == "0.0.0.0"
    assert adapter.port == 8081
    assert adapter.enable_private is True
    assert adapter.enable_group is True


def test_onebot_segments_to_components():
    components, text = onebot_segments_to_components(
        [
            {"type": "text", "data": {"text": "hello"}},
            {"type": "at", "data": {"qq": "all"}},
            {"type": "at", "data": {"qq": "123"}},
            {"type": "face", "data": {"id": "14"}},
            {"type": "image", "data": {"file": "a.png", "url": "https://cdn/a.png"}},
            {"type": "reply", "data": {"id": "msg-1"}},
            {"type": "json", "data": {"data": '{"ok": true}'}},
            {"type": "custom", "data": {"x": 1}},
        ]
    )

    assert isinstance(components[0], Plain)
    assert isinstance(components[1], AtAll)
    assert isinstance(components[2], At)
    assert isinstance(components[3], Face)
    assert isinstance(components[4], Image)
    assert isinstance(components[5], Reply)
    assert isinstance(components[6], Json)
    assert isinstance(components[7], Unknown)
    assert "hello" in text
    assert "@全体成员" in text
    assert "[图片]" in text


@pytest.mark.asyncio
async def test_message_chain_to_onebot_segments():
    segments = await message_chain_to_onebot_segments(
        [
            Plain("hi"),
            At(qq="123"),
            Image(file="https://cdn/a.png", url="https://cdn/a.png"),
            Reply(id="msg-1"),
            Json({"ok": True}),
        ]
    )

    assert segments[0] == {"type": "text", "data": {"text": "hi"}}
    assert segments[1] == {"type": "at", "data": {"qq": "123"}}
    assert segments[2] == {"type": "image", "data": {"file": "https://cdn/a.png"}}
    assert segments[3] == {"type": "reply", "data": {"id": "msg-1"}}
    assert segments[4] == {"type": "json", "data": {"data": '{"ok": true}'}}


def test_create_platform_message_private_and_group():
    private = create_platform_message(
        {
            "self_id": 10000,
            "message_id": 10,
            "message_type": "private",
            "user_id": 123,
            "sender": {"nickname": "Alice"},
            "message": [{"type": "text", "data": {"text": "hello"}}],
            "time": 1,
        },
        "10000",
    )
    group = create_platform_message(
        {
            "self_id": 10000,
            "message_id": 11,
            "message_type": "group",
            "group_id": 456,
            "user_id": 123,
            "sender": {"card": "AliceCard"},
            "message": [{"type": "text", "data": {"text": "group hi"}}],
            "time": 2,
        },
        "10000",
    )

    assert private.type == PlatformMessageType.FRIEND_MESSAGE
    assert private.session_id == private_session_id(123)
    assert private.sender.nickname == "Alice"
    assert private.message_str == "hello"
    assert group.type == PlatformMessageType.GROUP_MESSAGE
    assert group.session_id == group_session_id(456)
    assert group.group is not None
    assert group.group.group_id == "456"
    assert group.sender.nickname == "AliceCard"


@pytest.mark.asyncio
async def test_convert_message_commits_event():
    adapter = make_adapter()

    await adapter._handle_message_event(
        {
            "self_id": 10000,
            "message_id": 10,
            "message_type": "private",
            "user_id": 123,
            "sender": {"nickname": "Alice"},
            "message": [{"type": "text", "data": {"text": "hello"}}],
        }
    )

    event = adapter._event_queue.get_nowait()
    assert event.platform_meta.id == "onebot_main"
    assert event.session_id == "private%123"
    assert event.session_type == "onebot"
    assert event.platform_message.type == PlatformMessageType.FRIEND_MESSAGE


@pytest.mark.asyncio
async def test_send_message_routes_private_and_group():
    adapter = make_adapter()

    await adapter.send_message("private%123", MessageChain([Plain("dm")]))
    await adapter.send_message("group%456", MessageChain([Plain("group")]))

    client = adapter._bot
    assert client.calls[0] == (
        "send_private_msg",
        {"user_id": 123, "message": [{"type": "text", "data": {"text": "dm"}}]},
    )
    assert client.calls[1] == (
        "send_group_msg",
        {"group_id": 456, "message": [{"type": "text", "data": {"text": "group"}}]},
    )
