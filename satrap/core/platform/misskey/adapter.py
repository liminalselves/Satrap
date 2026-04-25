from __future__ import annotations

from typing import Any

from satrap.core.components import BaseMessageComponent, PlatformComponentType
from satrap.core.log import logger
from satrap.core.platform import (
    PlatformAdapter,
    PlatformConfig,
    register_platform_adapter,
)
from satrap.core.platform.event import MessageEvent, PlatformMetadata
from satrap.core.type import MessageMember, PlatformMessage, PlatformMessageType

from satrap.core.platform.misskey.client import MisskeyClient


@register_platform_adapter("misskey")
class MisskeyAdapter(PlatformAdapter):
    """Misskey 平台适配器"""

    adapter_type = "misskey"

    def __init__(
        self,
        config: PlatformConfig,
        event_handler=None,
        event_queue=None,
    ):
        super().__init__(config, event_handler, event_queue)
        settings = config.settings or {}
        self._instance_url: str = settings.get("instance_url", "")
        self._token: str = settings.get("token", "")
        self._client: MisskeyClient | None = None
        # user_id -> last_note_id 映射, 用于 send_text 时确定回复目标
        self._last_notes: dict[str, str] = {}

    def meta(self) -> PlatformMetadata:
        """返回平台元信息"""
        return PlatformMetadata(
            name=f"Misskey({self._instance_url})",
            id=self.config.id,
            adapter_display_name="Misskey",
            description="Misskey 平台适配器",
            support_streaming_message=False,
            support_proactive_message=False,
        )

    async def run(self):
        """主循环: 连接 Misskey WebSocket 并监听事件"""
        self._client = MisskeyClient(self._instance_url, self._token)
        async with self._client:
            await self._client.stream_connect("main", self._on_event)

    async def _on_event(self, body: dict):
        """处理 Misskey 推送的 notification 事件"""
        notif_type = body.get("type")
        if notif_type not in ("mention", "reply"):
            return

        note = body.get("note", {}) or {}
        user = body.get("user", {}) or {}
        user_id = user.get("id", "") or ""
        username = user.get("username", "") or ""
        text = note.get("text", "") or ""
        note_id = note.get("id", "") or ""

        if not user_id or not text:
            return

        # 记录最新 note_id 用于后续回复
        self._last_notes[user_id] = note_id

        pm = PlatformMessage()
        pm.type = PlatformMessageType.FRIEND_MESSAGE
        pm.self_id = self.client_self_id
        pm.session_id = user_id
        pm.message_id = note_id
        pm.sender = MessageMember(user_id=user_id, nickname=username)
        pm.message = [
            BaseMessageComponent(type=PlatformComponentType.Plain, text=text)
        ]
        pm.message_str = text
        pm.raw_message = body

        platform_meta = self.meta()

        event = MessageEvent(
            message_str=text,
            platform_message=pm,
            platform_meta=platform_meta,
            session_id=user_id,
            adapter=self,
        )

        self.commit_event(event)

    async def send_text(self, session_id: str, text: str) -> Any:
        """发送文本消息 (回复对应用户最近一条 note)"""
        if self._client is None:
            logger.error("[MisskeyAdapter] 客户端未初始化, 无法发送消息")
            return None
        reply_id = self._last_notes.get(session_id)
        return await self._client.create_note(text, reply_id=reply_id)
