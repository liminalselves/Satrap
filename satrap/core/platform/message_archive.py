"""将统一消息转换为档案快照, 不下载媒体或保存本地文件与原始事件"""
from __future__ import annotations

from urllib.parse import urlsplit

from satrap.core.components.message import PlatformComponentType
from satrap.core.config.platform_messages import ArchiveMessage
from satrap.core.type import PlatformMessage
from satrap.core.log import logger


def archive_snapshot(message: PlatformMessage, *, direction: str = "inbound") -> ArchiveMessage:
    """
    提取原始正文和有限组件摘要, 分开记录引用发送者与被提及者

    参数:
    - message: 已由适配器核验身份并归一的真实平台消息
    - direction: inbound 入站或 outbound 已确认发送与自身回显

    返回:
    - 可持久化的消息快照, 不包含引用正文, 本地路径或 Base64
    """
    components: list[dict[str, object]] = []
    media: list[dict[str, object]] = []
    mentions: list[str] = []
    quote: str | None = None
    texts: list[str] = []
    for component in message.message:
        kind = component.type
        summary: dict[str, object] = {"type": kind.value}
        if kind == PlatformComponentType.Plain:
            texts.append(str(getattr(component, "text", "") or ""))
        elif kind == PlatformComponentType.At:
            user_id = str(getattr(component, "qq", "") or "")
            if user_id and user_id != "all":
                mentions.append(user_id)
                summary["user_id"] = user_id
        elif kind == PlatformComponentType.Reply:
            message_id = str(getattr(component, "id", "") or "")
            if message_id:
                quote = quote or message_id
                summary["message_id"] = message_id
        elif kind in {PlatformComponentType.Image, PlatformComponentType.Record, PlatformComponentType.Video,
                      PlatformComponentType.File}:
            reference: dict[str, object] = {"type": kind.value}
            native_id = getattr(component, "native_media_id", "")
            if isinstance(native_id, str) and 0 < len(native_id) <= 256 and not any(ord(c) < 32 for c in native_id):
                reference["native_id"] = native_id
            raw_url = getattr(component, "url", "") or getattr(component, "file", "") or ""
            if isinstance(raw_url, str) and len(raw_url) <= 4096:
                try:
                    url = urlsplit(raw_url)
                    if url.scheme in {"https", "http"} and url.hostname and not url.username and not url.password:
                        reference["url"] = raw_url
                except ValueError:
                    logger.warning("[消息档案] 媒体 URL 无法解析, 仅保存组件类型")
                # 无法作为远程引用的媒体只保存类型, 不保存服务器路径或内嵌数据
            media.append(reference)
        elif kind == PlatformComponentType.Face:
            summary["id"] = str(getattr(component, "id", "") or "")[:256]
        elif kind == PlatformComponentType.Forward:
            summary["id"] = str(getattr(component, "id", "") or "")[:256]
        components.append(summary)
    return ArchiveMessage(
        message_id=message.message_id, sender_id=message.sender.user_id,
        message_time=float(message.timestamp), time_source=message.timestamp_source, text="".join(texts),
        nickname=message.sender.nickname or "", card=message.sender.card or "", direction=direction,
        components=components, reply_to_message_id=quote, mentions=mentions, media=media,
        truncated=len(components) > 128 or len(media) > 128,
    )
