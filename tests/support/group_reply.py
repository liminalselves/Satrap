"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

from unittest.mock import AsyncMock
from contextlib import contextmanager
from pathlib import Path
from typing import cast
import pytest
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.platform import PlatformAdapterManager, PlatformAdapterRegistry, PlatformConfig, current_adapter_manager, set_current_adapter_manager
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.group_chat.reply import bind_reply_turn
from satrap.core.call_context import bind_call_origin, bind_tool_workflow
from satrap.core.components import At, Plain
from satrap.core.platform.identity import BotIdentity
from satrap.core.group_chat.service import GroupChatService
from satrap.core.platform.receipt import SendReceipt
from satrap.core.platform.event import MessageEvent
from satrap.core.type import Group, MessageMember, PlatformMessage, PlatformMessageType
from satrap.core.storage import StorageLayout

SCOPE = MessageScope("ob", "10000", "group", "456")

COMPONENTS = [
    {"type": "text", "text": "甲: "}, {"type": "quote", "message_id": "77"},
    {"type": "mention", "source_message_id": "77"}, {"type": "text", "text": " 回复甲; "},
    {"type": "mention", "user_id": "789"}, {"type": "text", "text": " 回复乙"},
]

@pytest.fixture()
def restore_manager():
    previous = current_adapter_manager()
    yield
    set_current_adapter_manager(previous)

def setup(tmp_path: Path, *, private: bool = False) -> tuple[OneBotAdapter, MessageEvent, GroupChatService]:
    registry = PlatformAdapterRegistry()
    registry.register("onebot", OneBotAdapter)
    manager = PlatformAdapterManager(registry)
    adapter = manager.add_adapter(PlatformConfig(id="ob", type="onebot", settings={"reply_with_quote": True, "reply_with_mention": True}))
    assert isinstance(adapter, OneBotAdapter)
    adapter._bot = AsyncMock()
    adapter._running = True
    adapter.bot_self_id = adapter.client_self_id = "10000"
    adapter.message_archive = PlatformMessageStore(StorageLayout(tmp_path).platform_db("ob"), "ob")
    import time
    adapter.message_archive.record(SCOPE, ArchiveMessage("77", "123", time.time(), "甲提及了乙", mentions=["789"]))
    adapter._bot.get_group_member_info.return_value = {"group_id": 456, "user_id": 789, "nickname": "乙"}
    adapter.resolve_self_identity = AsyncMock(return_value=BotIdentity("10000", "机器人", "456", "群名片"))
    adapter.send_message = AsyncMock(return_value=SendReceipt("success", ("sent-1",)))
    set_current_adapter_manager(manager)
    message = PlatformMessage()
    message.type = PlatformMessageType.FRIEND_MESSAGE if private else PlatformMessageType.GROUP_MESSAGE
    message.self_id = "10000"
    message.session_id = "private%123" if private else "group%456"
    message.group_id = "" if private else "456"
    message.group = Group("456", "测试群")
    message.message_id = "77"
    message.sender = MessageMember(user_id="123", nickname="甲")
    message.message = [Plain("请回应讨论"), At(qq="10000")]
    event = MessageEvent("请回应讨论", message, adapter.meta(), message.session_id, adapter)
    event.is_wake = True
    return adapter, event, GroupChatService()

def send_mock(adapter: OneBotAdapter) -> AsyncMock:
    return cast(AsyncMock, adapter.send_message)

@contextmanager
def owner(event: MessageEvent):
    workflow = object()
    with bind_reply_turn(event) as turn, bind_call_origin(event.call_origin), bind_tool_workflow(workflow):
        turn.enabled, turn.workflow = True, workflow
        turn._enabled = lambda: True
        yield turn
