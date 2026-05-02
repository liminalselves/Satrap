from __future__ import annotations

import asyncio
import os
import random
import re
from collections.abc import AsyncGenerator
from typing import Any

from satrap.core.components import File, Image, PlatformComponentType, Record, Video
from satrap.core.log import logger
from satrap.core.platform import (
    PlatformAdapter,
    PlatformConfig,
    register_platform_adapter,
)
from satrap.core.platform.event import MessageChain, MessageEvent, PlatformMetadata
from satrap.core.platform.misskey.client import MisskeyAPI, StreamingClient
from satrap.core.platform.misskey.misskey_utils import (
    add_at_mention_if_needed,
    cache_room_info,
    cache_user_info,
    create_base_message,
    extract_room_id_from_session_id,
    extract_sender_info,
    extract_user_id_from_session_id,
    format_poll,
    is_valid_chat_session_id,
    is_valid_note_session_id,
    is_valid_room_session_id,
    process_at_mention,
    process_files,
    resolve_component_url_or_path,
    resolve_message_visibility,
    serialize_message_chain,
    upload_local_with_retries,
)
from satrap.core.type import PlatformMessage


MAX_FILE_UPLOAD_COUNT = 16
"""Misskey 最大文件上传数量"""
DEFAULT_UPLOAD_CONCURRENCY = 3
"""Misskey 默认上传并发数"""


@register_platform_adapter("misskey")
class MisskeyAdapter(PlatformAdapter):
    """Misskey 平台适配器"""

    adapter_type = "misskey"

    def __init__(
        self,
        config: PlatformConfig,
        event_handler=None,
        event_queue=None,
    ) -> None:
        """初始化 MisskeyAdapter 实例

        参数:
        - config: 平台配置
        - event_handler: 事件处理函数
        - event_queue: 事件队列
        """
        super().__init__(config, event_handler, event_queue)
        settings = config.settings or {}
        self._instance_url: str = settings.get("instance_url") or settings.get("misskey_instance_url", "")
        self._token: str = settings.get("token") or settings.get("misskey_token", "")
        self.max_message_length = int(settings.get("max_message_length", 3000))
        self.default_visibility = settings.get("misskey_default_visibility", "public")
        self.local_only = bool(settings.get("misskey_local_only", False))
        self.enable_chat = bool(settings.get("misskey_enable_chat", True))
        self.enable_file_upload = bool(settings.get("misskey_enable_file_upload", True))
        self.upload_folder = settings.get("misskey_upload_folder")
        self.upload_concurrency = int(settings.get("misskey_upload_concurrency", DEFAULT_UPLOAD_CONCURRENCY))
        self.allow_insecure_downloads = bool(settings.get("misskey_allow_insecure_downloads", False))
        self.download_timeout = int(settings.get("misskey_download_timeout", 15))
        self.download_chunk_size = int(settings.get("misskey_download_chunk_size", 64 * 1024))
        max_download_bytes = settings.get("misskey_max_download_bytes")
        self.max_download_bytes = int(max_download_bytes) if max_download_bytes is not None else None

        self._client: MisskeyAPI | None = None
        self._running = False
        self.bot_self_id = ""
        self._bot_username = ""
        self._user_cache: dict[str, dict[str, Any]] = {}

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

    async def run(self) -> None:
        """启动 Misskey API 和 streaming 主循环"""
        if not self._instance_url or not self._token:
            logger.error("[MisskeyAdapter] 配置不完整, 无法启动")
            return

        self._client = MisskeyAPI(
            self._instance_url,
            self._token,
            allow_insecure_downloads=self.allow_insecure_downloads,
            download_timeout=self.download_timeout,
            chunk_size=self.download_chunk_size,
            max_download_bytes=self.max_download_bytes,
        )
        self._running = True

        try:
            user_info = await self._client.get_current_user()
            self.bot_self_id = str(user_info.get("id", ""))
            self.client_self_id = self.bot_self_id or self.client_self_id
            self._bot_username = str(user_info.get("username", ""))
            logger.info(f"[MisskeyAdapter] 已连接用户: {self._bot_username} ({self.bot_self_id})")
        except Exception as e:
            self._running = False
            self.record_error(f"[MisskeyAdapter] 获取用户信息失败: {e}")
            return

        await self._start_websocket_connection()

    def _register_event_handlers(self, streaming: StreamingClient) -> None:
        """注册 streaming 事件处理器

        参数:
        - streaming: Misskey streaming 客户端
        """
        streaming.add_message_handler("notification", self._handle_notification)
        streaming.add_message_handler("main:notification", self._handle_notification)
        if self.enable_chat:
            streaming.add_message_handler("newChatMessage", self._handle_chat_message)
            streaming.add_message_handler("messaging:newChatMessage", self._handle_chat_message)
            streaming.add_message_handler("_debug", self._debug_handler)

    async def _start_websocket_connection(self) -> None:
        """以退避策略维护 WebSocket 连接"""
        backoff_delay = 1.0
        max_backoff = 300.0
        attempts = 0
        while self._running:
            attempts += 1
            try:
                if not self._client:
                    logger.error("[MisskeyAdapter] API 客户端未初始化")
                    break
                streaming = self._client.get_streaming_client()
                self._register_event_handlers(streaming)
                if await streaming.connect():
                    attempts = 0
                    backoff_delay = 1.0
                    await streaming.subscribe_channel("main")
                    if self.enable_chat:
                        await streaming.subscribe_channel("messaging")
                        await streaming.subscribe_channel("messagingIndex")
                    await streaming.listen()
            except Exception as e:
                logger.error(f"[MisskeyAdapter] WebSocket 异常: {e}")

            if self._running:
                sleep_time = backoff_delay + random.uniform(0, 1.0)
                logger.info(f"[MisskeyAdapter] {sleep_time:.1f}s 后重连 (尝试 #{attempts + 1})")
                await asyncio.sleep(sleep_time)
                backoff_delay = min(backoff_delay * 1.5, max_backoff)

    async def _handle_notification(self, data: dict[str, Any]) -> None:
        """处理 mention/reply/quote 通知
        
        参数:
        - data: 通知数据
        """
        try:
            notification_type = data.get("type")
            if notification_type not in ("mention", "reply", "quote"):
                return
            note = data.get("note") if isinstance(data.get("note"), dict) else data
            if not isinstance(note, dict) or not self._is_bot_mentioned(note):
                return
            message = await self.convert_message(note)
            self._commit_platform_message(message)
        except Exception as e:
            logger.error(f"[MisskeyAdapter] 处理通知失败: {e}")

    async def _handle_chat_message(self, data: dict[str, Any]) -> None:
        """处理 Misskey chat 私聊和房间消息
        
        参数:
        - data: 聊天消息数据
        """
        try:
            sender_id = str(data.get("fromUserId") or data.get("fromUser", {}).get("id", ""))
            if sender_id == self.bot_self_id:
                return
            if data.get("toRoomId"):
                message = await self.convert_room_message(data)
            else:
                message = await self.convert_chat_message(data)
            self._commit_platform_message(message)
        except Exception as e:
            logger.error(f"[MisskeyAdapter] 处理聊天消息失败: {e}")

    async def _debug_handler(self, data: dict[str, Any]) -> None:
        """记录未处理 streaming 事件
        
        参数:
        - data: 事件数据
        """
        logger.debug(f"[MisskeyAdapter] 未处理事件: {data.get('type', 'unknown')}")

    def _is_bot_mentioned(self, note: dict[str, Any]) -> bool:
        """判断 note 是否提及当前 bot
        
        参数:
        - note: Misskey note 数据
        """
        text = note.get("text", "") or ""
        mentions = [str(item) for item in note.get("mentions", [])]
        if self._bot_username and f"@{self._bot_username}" in text:
            return True
        if self.bot_self_id and self.bot_self_id in mentions:
            return True
        reply = note.get("reply")
        if isinstance(reply, dict):
            reply_user_id = str(reply.get("user", {}).get("id", ""))
            return reply_user_id == self.bot_self_id and bool(text)
        return False

    def _commit_platform_message(self, message: PlatformMessage) -> None:
        """将 PlatformMessage 封装为 MessageEvent 并提交
        
        参数:
        - message: 要提交的 PlatformMessage
        """
        event = MessageEvent(
            message_str=message.message_str,
            platform_message=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            adapter=self,
        )
        self.commit_event(event)

    def _process_poll_data(
        self,
        message: PlatformMessage,
        poll: dict[str, Any],
        message_parts: list[str],
    ) -> None:
        """处理 Misskey 投票数据
        
        参数:
        - message: 要添加投票组件的 PlatformMessage
        - poll: Misskey 投票数据
        - message_parts: 消息组件列表，用于存储格式化后的投票文本
        """
        if not isinstance(message.raw_message, dict):
            message.raw_message = {}
        message.raw_message["poll"] = poll
        poll_text = format_poll(poll)
        if poll_text:
            from satrap.core.components import Plain

            message.message.append(Plain(poll_text))
            message_parts.append(poll_text)

    async def convert_message(self, raw_data: dict[str, Any]) -> PlatformMessage:
        """将 Misskey note 转换为 Satrap PlatformMessage
        
        参数:
        - raw_data: Misskey note 数据
        """
        sender_info = extract_sender_info(raw_data, is_chat=False)
        message = create_base_message(raw_data, sender_info, self.bot_self_id, is_chat=False)
        cache_user_info(self._user_cache, sender_info, raw_data, self.bot_self_id, is_chat=False)

        message_parts: list[str] = []
        raw_text = raw_data.get("text", "") or ""
        if raw_text:
            text_parts, _ = process_at_mention(message, raw_text, self._bot_username, self.bot_self_id)
            message_parts.extend(text_parts)
        message_parts.extend(process_files(message, raw_data.get("files", []) or []))

        poll = raw_data.get("poll")
        if isinstance(poll, dict):
            self._process_poll_data(message, poll, message_parts)

        message.message_str = " ".join(part for part in message_parts if part.strip())
        return message

    async def convert_chat_message(self, raw_data: dict[str, Any]) -> PlatformMessage:
        """将 Misskey 私聊消息转换为 Satrap PlatformMessage
        
        参数:
        - raw_data: Misskey 私聊消息数据
        """
        sender_info = extract_sender_info(raw_data, is_chat=True)
        message = create_base_message(raw_data, sender_info, self.bot_self_id, is_chat=True)
        cache_user_info(self._user_cache, sender_info, raw_data, self.bot_self_id, is_chat=True)

        raw_text = raw_data.get("text", "") or ""
        if raw_text:
            from satrap.core.components import Plain

            message.message.append(Plain(raw_text))
        process_files(message, raw_data.get("files", []) or [], include_text_parts=False)
        message.message_str = raw_text
        return message

    async def convert_room_message(self, raw_data: dict[str, Any]) -> PlatformMessage:
        """将 Misskey 房间消息转换为 Satrap PlatformMessage
        
        参数:
        - raw_data: Misskey 房间消息数据
        """
        sender_info = extract_sender_info(raw_data, is_chat=True)
        room_id = str(raw_data.get("toRoomId", ""))
        message = create_base_message(
            raw_data,
            sender_info,
            self.bot_self_id,
            is_chat=False,
            room_id=room_id,
        )
        cache_user_info(self._user_cache, sender_info, raw_data, self.bot_self_id, is_chat=False)
        cache_room_info(self._user_cache, raw_data, self.bot_self_id)

        raw_text = raw_data.get("text", "") or ""
        message_parts: list[str] = []
        if raw_text:
            if self._bot_username and f"@{self._bot_username}" in raw_text:
                text_parts, _ = process_at_mention(message, raw_text, self._bot_username, self.bot_self_id)
                message_parts.extend(text_parts)
            else:
                from satrap.core.components import Plain

                message.message.append(Plain(raw_text))
                message_parts.append(raw_text)
        message_parts.extend(process_files(message, raw_data.get("files", []) or []))
        message.message_str = " ".join(part for part in message_parts if part.strip())
        return message

    def _extract_additional_fields(self, session_id: str, message_chain: MessageChain) -> dict[str, Any]:
        """提取发送 note 时的额外字段
        
        参数:
        - session_id: 会话 ID
        - message_chain: 要提取额外字段的 MessageChain
        """
        fields = {"cw": None, "poll": None, "renote_id": None, "channel_id": None}
        for comp in message_chain:
            for attr, key in (("cw", "cw"), ("poll", "poll"), ("renote_id", "renote_id"), ("channel_id", "channel_id")):
                value = getattr(comp, attr, None)
                if value:
                    fields[key] = value
        user_id = extract_user_id_from_session_id(session_id)
        user_info = self._user_cache.get(user_id, {})
        if user_info.get("reply_to_note_id"):
            fields["reply_id"] = user_info["reply_to_note_id"]
        return fields

    def _has_file_component(self, comp: Any) -> bool:
        """判断组件是否可能携带文件
        
        参数:
        - comp: 要检查的组件
        """
        return isinstance(comp, (Image, Record, Video, File)) or any(
            hasattr(comp, attr) for attr in ("convert_to_file_path", "get_file", "file", "url", "path", "src", "source")
        )

    async def _upload_component(self, comp: Any, sem: asyncio.Semaphore) -> str | None:
        """上传单个文件组件并返回 fileId
        
        参数:
        - comp: 要上传的组件
        - sem: 用于并发控制的 Semaphore
        """
        async with sem:
            if not self._client:
                return None
            url_candidate, local_path = await resolve_component_url_or_path(comp)
            preferred_name = getattr(comp, "name", None) or getattr(comp, "file", None)
            if url_candidate:
                result = await self._client.upload_and_find_file(
                    str(url_candidate),
                    str(preferred_name) if preferred_name else None,
                    folder_id=self.upload_folder,
                )
                if isinstance(result, dict) and result.get("id"):
                    return str(result["id"])
            if local_path:
                file_id = await upload_local_with_retries(
                    self._client,
                    str(local_path),
                    str(preferred_name) if preferred_name else None,
                    self.upload_folder,
                )
                if file_id:
                    return file_id
            return None

    async def _collect_file_ids(self, message_chain: MessageChain) -> list[str]:
        """上传消息链中的文件组件并收集 fileId
        
        参数:
        - message_chain: 要上传文件组件的 MessageChain
        """
        if not self.enable_file_upload:
            return []
        components = [comp for comp in message_chain if self._has_file_component(comp)]
        if len(components) > MAX_FILE_UPLOAD_COUNT:
            logger.warning(f"[MisskeyAdapter] 文件数量超过限制, 只上传前 {MAX_FILE_UPLOAD_COUNT} 个")
            components = components[:MAX_FILE_UPLOAD_COUNT]
        concurrency = max(1, min(int(self.upload_concurrency), 10))
        sem = asyncio.Semaphore(concurrency)
        results = await asyncio.gather(*(self._upload_component(comp, sem) for comp in components))
        return [file_id for file_id in results if file_id]

    async def send_text(self, session_id: str, text: str) -> Any:
        """发送纯文本消息
        
        参数:
        - session_id: 会话 ID
        - text: 要发送的纯文本消息
        """
        return await self.send_message(session_id, MessageChain.from_text(text))

    async def send_message(self, session_id: str, message: MessageChain) -> Any:
        """按 session_id 发送完整消息链
        
        参数:
        - session_id: 会话 ID
        - message: 要发送的消息链
        """
        if not self._client:
            logger.error("[MisskeyAdapter] 客户端未初始化, 无法发送消息")
            return None

        text, has_at_user = serialize_message_chain(message)
        if len(text) > self.max_message_length:
            text = text[: self.max_message_length] + "..."
        file_ids = await self._collect_file_ids(message)
        if not text.strip() and not file_ids:
            logger.warning("[MisskeyAdapter] 消息为空且无文件, 跳过发送")
            return None

        if is_valid_room_session_id(session_id):
            payload: dict[str, Any] = {"toRoomId": extract_room_id_from_session_id(session_id), "text": text}
            if file_ids:
                payload["fileId"] = file_ids[0]
            return await self._client.send_room_message(payload)

        if is_valid_chat_session_id(session_id):
            payload = {"toUserId": extract_user_id_from_session_id(session_id), "text": text}
            if file_ids:
                payload["fileId"] = file_ids[0]
            return await self._client.send_message(payload)

        user_id = extract_user_id_from_session_id(session_id)
        user_info = self._user_cache.get(user_id)
        text = add_at_mention_if_needed(text, user_info, has_at_user)
        visibility, visible_user_ids = resolve_message_visibility(
            user_id=user_id,
            user_cache=self._user_cache,
            self_id=self.bot_self_id,
            default_visibility=self.default_visibility,
        )
        fields = self._extract_additional_fields(session_id, message)
        return await self._client.create_note(
            text=text,
            visibility=visibility,
            visible_user_ids=visible_user_ids,
            file_ids=file_ids or None,
            local_only=self.local_only,
            reply_id=fields.get("reply_id"),
            cw=fields.get("cw"),
            poll=fields.get("poll"),
            renote_id=fields.get("renote_id"),
            channel_id=fields.get("channel_id"),
        )

    async def send_stream(
        self,
        session_id: str,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> Any:
        """Misskey 流式发送降级为分段或合并发送
        
        参数:
        - session_id: 会话 ID
        - generator: 生成消息链的异步迭代器
        - use_fallback: 是否使用分段发送 (默认 False)
        """
        if not use_fallback:
            buffer: list[Any] = []
            async for chain in generator:
                buffer.extend(chain.components)
            if buffer:
                return await self.send_message(session_id, MessageChain(buffer))
            return None

        text_buffer = ""
        pattern = re.compile(r"[^。？！~…]+[。？！~…]+")
        async for chain in generator:
            for comp in chain:
                if getattr(comp, "type", None) == PlatformComponentType.Plain:
                    text_buffer += getattr(comp, "text", "")
                elif getattr(comp, "text", None):
                    text_buffer += str(getattr(comp, "text", ""))
                else:
                    if text_buffer.strip():
                        await self.send_text(session_id, text_buffer)
                        text_buffer = ""
                    await self.send_message(session_id, MessageChain([comp]))
                    await asyncio.sleep(1.5)
            while True:
                match = re.search(pattern, text_buffer)
                if not match:
                    break
                await self.send_text(session_id, match.group())
                text_buffer = text_buffer[match.end():]
                await asyncio.sleep(1.5)
        if text_buffer.strip():
            return await self.send_text(session_id, text_buffer)
        return None

    async def terminate(self) -> None:
        """终止 Misskey 适配器并释放资源"""
        self._running = False
        if self._client:
            await self._client.close()
            self._client = None
        await super().terminate()

    def get_client(self) -> Any:
        """返回底层 Misskey API 客户端"""
        return self._client
