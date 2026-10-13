"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

from unittest.mock import AsyncMock
from typing import Any
import pytest
from satrap.expend.plugins.group_admin.tools import AsyncGroupAdminTool, get_tools
from satrap.core.call_context import CallOrigin
from satrap.core.framework.Base import Session
from satrap.core.platform import (
    PlatformAdapterManager,
    PlatformAdapterRegistry,
    PlatformConfig,
    current_adapter_manager,
    set_current_adapter_manager,
)
from satrap.core.platform.onebot.adapter import OneBotAdapter

def _origin(chat_type: str = "GroupMessage", chat_id: str = "456", actor: str = "123") -> CallOrigin:
    return CallOrigin(
        adapter_id="ob", self_id="10000", chat_type=chat_type, chat_id=chat_id,
        actor_id=actor, source_message_id="77", request_id="r1",
    )

def _setup_adapter(**settings: object) -> OneBotAdapter:
    registry = PlatformAdapterRegistry()
    registry.register("onebot", OneBotAdapter)
    manager = PlatformAdapterManager(registry=registry)
    adapter = manager.add_adapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings)))
    assert isinstance(adapter, OneBotAdapter)
    adapter._bot = AsyncMock()
    set_current_adapter_manager(manager)
    return adapter

def _async_tools(config: dict[str, Any]) -> list[AsyncGroupAdminTool]:
    """构造异步工具实例, 模拟 async_simple 会话"""
    from satrap.edictum import AsyncSimpleSession

    class _FakeAsync(AsyncSimpleSession):
        def __init__(self) -> None:
            pass

    return [tool for tool in get_tools(_FakeAsync(), config) if isinstance(tool, AsyncGroupAdminTool)]
