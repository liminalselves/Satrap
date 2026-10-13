"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

from typing import Any

from satrap.core.platform import PlatformAdapter, PlatformConfig
from satrap.core.platform.event import MessageChain, PlatformMetadata


class RecorderAdapter(PlatformAdapter):
    """记录 send_message 调用的测试适配器"""

    def __init__(self, adapter_id: str = "rec1", settings: dict[str, Any] | None = None):
        super().__init__(PlatformConfig(id=adapter_id, type="rec", settings=settings or {}))
        self.sent: list[tuple[str, MessageChain, str]] = []

    async def run(self) -> None:
        return None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(name=self.config.id, id=self.config.id)

    async def send_message(
        self, session_id: str, message: MessageChain, *, request_id: str = "",
        purpose: str = "business", require_tracking: bool = False,
    ) -> Any:
        self.sent.append((session_id, message, purpose))
        return None
