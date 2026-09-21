"""
引用内容补全与模型输入投影

在唤醒与限流之后, 按事件级预算回源顶层 Reply 组件的原文并填充已有 Reply 字段,
再将当前正文与引用上下文投影为一次请求的文本与媒体输入; 引用内容作为用户提供的资料,
不参与唤醒判断或命令解析
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from satrap.core.components import BaseMessageComponent, PlatformComponentType, Reply
from satrap.core.platform.event import MessageEvent
from satrap.core.type import safe_getattr, safe_getattr_str

QUOTE_TEXT_LIMIT = 2000
"""单条引用原文进入模型输入的最大字符数"""
QUOTE_MEDIA_LIMIT = 4
"""引用内图片/视频合计进入模型输入的最大数量"""


@dataclass(frozen=True)
class ProjectedInput:
    """一次请求的模型输入投影"""

    message: str
    """当前正文, 含已标记的引用上下文"""
    images: tuple[str, ...] = ()
    videos: tuple[str, ...] = ()
    quote_status: str = "none"
    """none, resolved, unavailable 或 disabled"""
    notes: tuple[str, ...] = field(default_factory=tuple)


def _media_sources(components: list[BaseMessageComponent], media_type: str) -> list[str]:
    """
    从组件列表提取指定类型媒体来源

    参数:
    - components: 组件列表
    - media_type: image 或 video

    返回:
    - list[str]: URL 或文件路径, 去重保序
    """
    urls: list[str] = []
    for comp in components:
        ctype = safe_getattr(comp, "type")
        ctype_str = ctype.value if hasattr(ctype, "value") else str(ctype)
        if ctype_str.lower() != media_type:
            continue
        url = safe_getattr_str(comp, "url") or safe_getattr_str(comp, "file")
        if url and url not in urls:
            urls.append(url)
    return urls


async def resolve_quotes(event: MessageEvent) -> str:
    """
    回源当前消息顶层的首个 Reply 并填充其字段, 每事件最多一次回源

    参数:
    - event: 已唤醒且已通过限流的事件

    返回:
    - str: resolved, unavailable, disabled 或 none
    """
    replies = [c for c in event.get_messages() if c.type == PlatformComponentType.Reply]
    if not replies:
        return "none"
    reply = replies[0]
    if safe_getattr_str(reply, "message_str") or safe_getattr(reply, "chain"):
        return "resolved"
    if event.policy_settings.get("quote_lookup", True) is False:
        return "disabled"
    adapter = event.adapter
    fetch = getattr(adapter, "fetch_quoted_message", None)
    message_id = safe_getattr_str(reply, "id")
    if fetch is None or not message_id:
        return "unavailable"
    try:
        result = await fetch(message_id, event.session_id)
    except Exception:
        result = None
    if not isinstance(result, dict):
        return "unavailable"
    data = cast(dict[str, Any], result)
    components = data.get("components")
    if isinstance(reply, Reply):
        reply.chain = [c for c in cast(list[Any], components) if isinstance(c, BaseMessageComponent)] if isinstance(components, list) else []
        reply.message_str = str(data.get("message_str", ""))
        reply.sender_id = str(data.get("sender_id", ""))
        reply.sender_nickname = str(data.get("sender_nickname", ""))
        reply.time = int(data.get("time", 0) or 0)
    return "resolved"
    # 仅填充顶层首个引用, 引用内的引用保留占位, 避免无界递归回源


def project_input(event: MessageEvent, quote_status: str) -> ProjectedInput:
    """
    组装当前正文与引用上下文, 合并顶层与引用内的媒体

    参数:
    - event: 已完成引用补全的事件
    - quote_status: resolve_quotes 的结果

    返回:
    - ProjectedInput: 文本与媒体来源, 引用内容以明确标记包裹
    """
    top = event.get_messages()
    images = _media_sources(top, "image")
    videos = _media_sources(top, "video")
    message = event.get_message_str()
    notes: list[str] = []
    replies = [c for c in top if c.type == PlatformComponentType.Reply]
    if replies:
        message = message.replace("[回复]", "", 1).lstrip()
        # 顶层占位符由引用上下文标记替代, 只移除首个以免误删正文中的同名文字
        reply = replies[0]
        quoted_text = safe_getattr_str(reply, "message_str")
        chain = safe_getattr(reply, "chain")
        quoted_components = [c for c in cast(list[Any], chain) if isinstance(c, BaseMessageComponent)] if isinstance(chain, list) else []
        if quote_status == "resolved" and (quoted_text or quoted_components):
            if len(quoted_text) > QUOTE_TEXT_LIMIT:
                quoted_text = quoted_text[:QUOTE_TEXT_LIMIT] + "…"
                notes.append("quote_truncated")
            sender = safe_getattr_str(reply, "sender_nickname") or safe_getattr_str(reply, "sender_id") or "未知"
            own = safe_getattr_str(reply, "sender_id") == event.get_self_id()
            label = "机器人自己" if own else sender
            quote_media = _media_sources(quoted_components, "image")
            quote_videos = _media_sources(quoted_components, "video")
            budget = QUOTE_MEDIA_LIMIT
            for url in quote_media:
                if budget <= 0:
                    notes.append("quote_media_truncated")
                    break
                if url not in images:
                    images.append(url)
                    budget -= 1
            for url in quote_videos:
                if budget <= 0:
                    notes.append("quote_media_truncated")
                    break
                if url not in videos:
                    videos.append(url)
                    budget -= 1
            quoted_display = quoted_text or "(仅含附件)"
            message = f"[引用 {label} 的消息: {quoted_display}]\n{message}".rstrip("\n")
        elif quote_status in {"unavailable", "disabled"}:
            message = f"[引用了一条无法获取原文的消息]\n{message}".rstrip("\n")
            notes.append(f"quote_{quote_status}")
    return ProjectedInput(message=message, images=tuple(images), videos=tuple(videos), quote_status=quote_status, notes=tuple(notes))
