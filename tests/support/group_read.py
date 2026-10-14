"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from satrap.core.call_context import CallOrigin
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.group_chat.service import GroupChatService
from satrap.core.group_chat.types import MemberRecord, MemberSnapshot, VerifiedMember, VerifiedMessage
from satrap.core.platform import PlatformAdapter, PlatformAdapterManager, PlatformAdapterRegistry, PlatformConfig, set_current_adapter_manager
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.event import PlatformMetadata

NOW = 1_800_000_000.0

def _setup(tmp_path: Path) -> tuple[GroupChatService, OneBotAdapter, CallOrigin]:
    """装配真实适配器管理器和平台数据库"""
    registry = PlatformAdapterRegistry()
    registry.register("onebot", OneBotAdapter)
    manager = PlatformAdapterManager(registry)
    adapter = manager.add_adapter(PlatformConfig(id="ob", type="onebot", settings={}))
    assert isinstance(adapter, OneBotAdapter)
    adapter._bot = AsyncMock()
    adapter._running = True
    adapter.bot_self_id = adapter.client_self_id = "10000"
    adapter.message_archive = PlatformMessageStore(tmp_path / "platform.db", "ob", clock=lambda: NOW)
    set_current_adapter_manager(manager)
    origin = CallOrigin("ob", "10000", "GroupMessage", "456", "123", "77", "request-1")
    return GroupChatService(), adapter, origin

class _FutureAdapter(PlatformAdapter):
    """用于证明宿主没有依赖 QQ 字段和数字 ID 的虚拟平台"""

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(id=self.config.id, name="future", adapter_display_name="未来平台")

    async def run(self) -> None:
        pass

    def group_chat_capabilities(self) -> dict[str, dict[str, str]]:
        result = super().group_chat_capabilities()
        for name in ("member_list", "member_info", "message_lookup"):
            result[name] = {"state": "supported", "reason": "virtual_adapter"}
        return result

    async def group_chat_members(self, scope: MessageScope) -> MemberSnapshot:
        return MemberSnapshot(scope, (MemberRecord("user:甲", "甲", "名片"),), NOW, True)

    async def group_chat_member(self, scope: MessageScope, user_id: str) -> VerifiedMember:
        return VerifiedMember(scope, MemberRecord(user_id, "甲", "名片"), NOW)

    async def group_chat_message(self, scope: MessageScope, message_id: str) -> VerifiedMessage:
        return VerifiedMessage(scope, ArchiveMessage(message_id, "user:甲", NOW, "未来平台原文"))
