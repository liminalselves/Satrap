"""
引用与转发内容补全及模型输入投影

在唤醒与限流之后, 按事件级预算回源顶层 Reply 与 Forward 组件并填充字段,
再将当前正文与引用/转发上下文投影为一次请求的文本与媒体输入; 引用与转发内容
作为用户提供的资料, 不参与唤醒判断或命令解析
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from satrap.core.pipeline.attachments import AttachmentResult, render_attachments
from satrap.core.config.platform_policy import policy_default
from satrap.core.components import BaseMessageComponent, Forward, Node, PlatformComponentType, Reply
from satrap.core.platform.event import MessageEvent
from satrap.core.type import safe_getattr, safe_getattr_str
from satrap.core.log import logger

QUOTE_TEXT_LIMIT = 2000
"""单条引用原文进入模型输入的最大字符数"""
FORWARD_TEXT_LIMIT = 2000
"""单条转发投影进入模型输入的最大字符数"""
FORWARD_RESOLVE_LIMIT = 2
"""每事件最多回源的顶层转发数"""


def _safe_int(value: Any, default: int = 0) -> int:
    """把平台回源字段收窄为 int, 非数字时返回默认值而不是抛出"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
    attachment_status: str = "none"
    """none, resolved, partial 或 failed"""
    """none, resolved, partial, unavailable 或 disabled"""
    notes: tuple[str, ...] = field(default_factory=tuple)


def media_sources(components: list[BaseMessageComponent], media_type: str) -> list[str]:
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
    except Exception as error:
        logger.debug(f"[input_projection] 引用回源异常 message_id={message_id}: {type(error).__name__}")
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
        reply.time = _safe_int(data.get("time"), 0)
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
        except Exception as error:
            logger.debug(f"[input_projection] 转发回源异常 forward_id={forward_id}: {type(error).__name__}")
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


@dataclass
class _ContextBlock:
    """一段待拼接的上下文: 来源标记头与资料内容分离计价, 内容为空时只保留标记头"""

    kind: str
    """quote, forward 或 attachment, 用于截断诊断说明"""
    header: str
    content: str
    suffix: str = ""


class ProjectionBudget:
    """
    模型输入文本总额度记账, 标记头, 分隔符与截断提示全部计入

    固定优先级: 当前问题正文 (保前缀) > 来源标记头 > 资料块内容;
    额度不足时按优先级截断并记录 notes, 最终拼接结果不超过总额度
    """

    def __init__(self, limit: int) -> None:
        """以 limit 为拼接结果的最大字符数, 必须为正整数"""
        if limit <= 0:
            raise ValueError("输入总额度必须为正数")
        self.limit = limit

    def assemble(self, body: str, blocks: list[_ContextBlock], notes: list[str]) -> str:
        """
        在总额度内拼接上下文块与正文

        参数:
        - body: 当前问题正文, 优先级最高, 超限时保留前缀
        - blocks: 按最终展示顺序排列的上下文块
        - notes: 诊断说明列表, 截断与丢弃就地追加

        返回:
        - str: 总长度不超过 limit 的拼接结果
        """
        if len(body) > self.limit:
            body = body[: self.limit - 1] + "…"
            notes.append("body_budget_truncated")
        used = len(body)
        kept: list[tuple[_ContextBlock, str]] = []
        for block in blocks:
            fixed = len(block.header) + len(block.suffix) + 1 + (1 if block.content else 0)
            # 计价含块标记, 与正文的分隔符与内容截断提示占位
            if used + fixed <= self.limit:
                used += fixed
                kept.append((block, ""))
            else:
                notes.append(f"{block.kind}_budget_dropped")
        rendered: list[str] = []
        for block, _ in kept:
            if not block.content:
                rendered.append(block.header + block.suffix)
                continue
            allowance = self.limit - used + 1
            # 完整内容可挪用截断提示的预留位
            if len(block.content) <= allowance:
                used += len(block.content) - 1
                rendered.append(block.header + block.content + block.suffix)
                continue
            take = self.limit - used
            used += take
            rendered.append(block.header + block.content[:take] + "…" + block.suffix)
            notes.append(f"{block.kind}_budget_truncated")
        return "\n".join([*rendered, body]).rstrip("\n")


class _MediaBudget:
    """引用与转发媒体共享的数量预算, 超出时记录一次诊断说明"""

    def __init__(self, limit: int, images: list[str], videos: list[str], notes: list[str]) -> None:
        self.remaining = limit
        self.images, self.videos, self.notes = images, videos, notes

    def merge(self, components: list[BaseMessageComponent], note: str) -> None:
        """
        把组件中的图片与视频并入顶层媒体列表

        参数:
        - components: 引用或转发节点的组件
        - note: 预算耗尽时写入的说明
        """
        for media_type, target in (("image", self.images), ("video", self.videos)):
            for url in media_sources(components, media_type):
                if self.remaining <= 0:
                    self.notes.append(note)
                    break
                # 预算耗尽只跳过当前媒体类型, 另一类型仍须检查
                if url not in target:
                    target.append(url)
                    self.remaining -= 1


def project_input(
    event: MessageEvent, quote_status: str, forward_status: str = "none", attachments: tuple[AttachmentResult, ...] = (),
) -> ProjectedInput:
    """
    组装当前正文与引用/转发上下文, 合并顶层与补全内容的媒体

    参数:
    - event: 已完成引用与转发补全的事件
    - quote_status: resolve_quotes 的结果
    - forward_status: resolve_forwards 的结果
    - attachments: resolve_attachments 的结果, 语音转写与文件正文作为资料块前置

    返回:
    - ProjectedInput: 文本与媒体来源, 引用, 转发与附件内容以明确标记包裹;
      顶层媒体按 input_media_limit 实际裁剪, 文本总量按 input_text_limit 记账拼接
    """
    top = event.get_messages()
    media_limit = int(event.policy_settings.get("input_media_limit", policy_default("input_media_limit")))
    images = media_sources(top, "image")
    videos = media_sources(top, "video")
    notes: list[str] = []
    if len(images) + len(videos) > media_limit:
        # 顶层存量实际截断列表本身, 余量再供引用/转发媒体消耗
        videos = videos[: max(0, media_limit - len(images))]
        images = images[: media_limit]
        notes.append("top_media_truncated")
    message = event.get_message_str()
    budget = _MediaBudget(media_limit - len(images) - len(videos), images, videos, notes)
    quote_blocks: list[_ContextBlock] = []
    forward_blocks: list[_ContextBlock] = []
    attachment_blocks: list[_ContextBlock] = []
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
            budget.merge(quoted_components, "quote_media_truncated")
            quoted_display = quoted_text or "(仅含附件)"
            quote_blocks.append(_ContextBlock("quote", f"[引用 {label} 的消息: ", quoted_display, "]"))
        elif quote_status in {"unavailable", "disabled"}:
            quote_blocks.append(_ContextBlock("quote", "[引用了一条无法获取原文的消息]", ""))
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
            budget.merge(node_components, "forward_media_truncated")
        block = "\n".join(lines)
        if len(block) > FORWARD_TEXT_LIMIT:
            block = block[:FORWARD_TEXT_LIMIT] + "…"
            notes.append("forward_truncated")
        forward_blocks.append(_ContextBlock("forward", f"[转发消息 {len(lines)} 条:\n", block, "]"))
    attachment_status = "none"
    if attachments:
        for placeholder in ("[语音]", "[文件]"):
            for _ in range(sum(1 for item in attachments if (item.kind == "record") == (placeholder == "[语音]"))):
                message = message.replace(placeholder, "", 1)
        message = message.strip()
        # 顶层占位符由附件块替代, 只移除与附件数量相同的次数
        resolved = sum(1 for item in attachments if item.status == "resolved")
        attachment_status = "resolved" if resolved == len(attachments) else "partial" if resolved else "failed"
        for item in attachments:
            if item.status != "resolved":
                notes.append(f"attachment_{item.status}")
        block = render_attachments(attachments)
        if block:
            attachment_blocks.append(_ContextBlock("attachment", "", block))
    text_limit = int(event.policy_settings.get("input_text_limit", policy_default("input_text_limit")))
    message = ProjectionBudget(text_limit).assemble(message, [*attachment_blocks, *forward_blocks, *quote_blocks], notes)
    return ProjectedInput(
        message=message, images=tuple(images), videos=tuple(videos),
        quote_status=quote_status, forward_status=forward_status, attachment_status=attachment_status, notes=tuple(notes),
    )
