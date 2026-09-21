"""
引用与转发内容补全及模型输入投影

在唤醒与限流之后, 按事件级预算回源顶层 Reply 与 Forward 组件并填充字段,
再将当前正文与引用/转发上下文投影为一次请求的文本与媒体输入; 引用与转发内容
作为用户提供的资料, 不参与唤醒判断或命令解析
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from satrap.core.components import BaseMessageComponent, Forward, Node, PlatformComponentType, Reply
from satrap.core.platform.event import MessageEvent
from satrap.core.type import safe_getattr, safe_getattr_str

QUOTE_TEXT_LIMIT = 2000
"""单条引用原文进入模型输入的最大字符数"""
QUOTE_MEDIA_LIMIT = 4
"""引用与转发内图片/视频合计进入模型输入的最大数量"""
FORWARD_TEXT_LIMIT = 2000
"""单条转发投影进入模型输入的最大字符数"""
FORWARD_RESOLVE_LIMIT = 2
"""每事件最多回源的顶层转发数"""


@dataclass(frozen=True)
class ProjectedInput:
    """一次请求的模型输入投影"""

    message: str
    """当前正文, 含已标记的引用上下文"""
    images: tuple[str, ...] = ()
    videos: tuple[str, ...] = ()
    quote_status: str = "none"
    """none, resolved, unavailable 或 disabled"""
    forward_status: str = "none"
    """none, resolved, partial, unavailable 或 disabled"""
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
        ctype_str = str(safe_getattr(ctype, "value", ctype))
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


async def resolve_forwards(event: MessageEvent) -> str:
    """
    补全顶层 Forward 组件的节点, 内联节点优先, 每事件最多回源两条转发

    参数:
    - event: 已唤醒且已通过限流的事件

    返回:
    - str: none, resolved, partial, unavailable 或 disabled; 不递归展开节点内的嵌套转发
    """
    forwards = [
        c for c in event.get_messages()
        if c.type == PlatformComponentType.Forward and isinstance(c, Forward)
    ]
    if not forwards:
        return "none"
    enabled = event.policy_settings.get("forward_lookup", True) is not False
    fetch = getattr(event.adapter, "fetch_forward_message", None)
    attempts = 0
    for forward in forwards:
        if forward.nodes is not None:
            continue
        if not enabled or fetch is None or attempts >= FORWARD_RESOLVE_LIMIT:
            continue
        forward_id = safe_getattr_str(forward, "id")
        if not forward_id:
            continue
        attempts += 1
        try:
            nodes = await fetch(forward_id, event.session_id)
        except Exception:
            nodes = None
        if isinstance(nodes, list):
            forward.nodes = [n for n in cast(list[Any], nodes) if isinstance(n, Node)]
    resolved = sum(1 for f in forwards if f.nodes is not None)
    if resolved == len(forwards):
        return "resolved"
    if resolved:
        return "partial"
    return "unavailable" if enabled else "disabled"


def _components_brief_text(components: list[BaseMessageComponent]) -> str:
    """
    生成节点正文的单行摘要, 嵌套转发与引用只保留占位

    参数:
    - components: 节点正文组件

    返回:
    - str: 供转发投影使用的纯文本摘要
    """
    parts: list[str] = []
    for comp in components:
        ctype = safe_getattr(comp, "type")
        ctype_str = str(safe_getattr(ctype, "value", ctype)).lower()
        if ctype_str == "plain":
            parts.append(safe_getattr_str(comp, "text"))
        elif ctype_str == "at":
            parts.append(f"@{safe_getattr_str(comp, 'qq')}")
        elif ctype_str == "image":
            parts.append("[图片]")
        elif ctype_str == "video":
            parts.append("[视频]")
        elif ctype_str == "record":
            parts.append("[语音]")
        elif ctype_str == "file":
            parts.append(f"[文件 {safe_getattr_str(comp, 'name')}]".rstrip())
        elif ctype_str == "face":
            parts.append(f"[表情:{safe_getattr_str(comp, 'id')}]")
        elif ctype_str == "forward":
            parts.append("[转发]")
        elif ctype_str == "reply":
            parts.append("[回复]")
    return "".join(parts)


def project_input(event: MessageEvent, quote_status: str, forward_status: str = "none") -> ProjectedInput:
    """
    组装当前正文与引用/转发上下文, 合并顶层与补全内容的媒体

    参数:
    - event: 已完成引用与转发补全的事件
    - quote_status: resolve_quotes 的结果
    - forward_status: resolve_forwards 的结果

    返回:
    - ProjectedInput: 文本与媒体来源, 引用与转发内容以明确标记包裹
    """
    top = event.get_messages()
    images = _media_sources(top, "image")
    videos = _media_sources(top, "video")
    message = event.get_message_str()
    notes: list[str] = []
    media_budget = QUOTE_MEDIA_LIMIT
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
            for url in quote_media:
                if media_budget <= 0:
                    notes.append("quote_media_truncated")
                    break
                if url not in images:
                    images.append(url)
                    media_budget -= 1
            for url in quote_videos:
                if media_budget <= 0:
                    notes.append("quote_media_truncated")
                    break
                if url not in videos:
                    videos.append(url)
                    media_budget -= 1
            quoted_display = quoted_text or "(仅含附件)"
            message = f"[引用 {label} 的消息: {quoted_display}]\n{message}".rstrip("\n")
        elif quote_status in {"unavailable", "disabled"}:
            message = f"[引用了一条无法获取原文的消息]\n{message}".rstrip("\n")
            notes.append(f"quote_{quote_status}")
    for forward in [c for c in top if c.type == PlatformComponentType.Forward and isinstance(c, Forward)]:
        nodes = forward.nodes
        if not nodes:
            notes.append("forward_unresolved")
            continue
        message = message.replace("[转发]", "", 1).lstrip("\n")
        # 顶层占位符由转发上下文标记替代, 未解析的转发保留占位
        lines: list[str] = []
        for node in nodes:
            name = safe_getattr_str(node, "name") or safe_getattr_str(node, "uin") or "未知"
            content = safe_getattr(node, "content")
            node_components = [c for c in cast(list[Any], content) if isinstance(c, BaseMessageComponent)] if isinstance(content, list) else []
            lines.append(f"- {name}: {_components_brief_text(node_components) or '(仅含附件)'}")
            for url in _media_sources(node_components, "image"):
                if media_budget <= 0:
                    notes.append("forward_media_truncated")
                    break
                if url not in images:
                    images.append(url)
                    media_budget -= 1
            for url in _media_sources(node_components, "video"):
                if media_budget <= 0:
                    notes.append("forward_media_truncated")
                    break
                if url not in videos:
                    videos.append(url)
                    media_budget -= 1
        block = "\n".join(lines)
        if len(block) > FORWARD_TEXT_LIMIT:
            block = block[:FORWARD_TEXT_LIMIT] + "…"
            notes.append("forward_truncated")
        message = f"[转发消息 {len(lines)} 条:\n{block}]\n{message}".rstrip("\n")
    return ProjectedInput(
        message=message, images=tuple(images), videos=tuple(videos),
        quote_status=quote_status, forward_status=forward_status, notes=tuple(notes),
    )
