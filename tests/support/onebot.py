"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

from unittest.mock import AsyncMock
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform import PlatformConfig

def _adapter(**settings: object) -> OneBotAdapter:
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings)))
    adapter._bot = AsyncMock()
    if not adapter.bot_self_id:
        adapter.bot_self_id = "10000"
    return adapter
