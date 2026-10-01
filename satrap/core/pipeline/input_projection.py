"""
引用与转发内容补全及模型输入投影

在唤醒与限流之后, 按事件级预算回源顶层 Reply 与 Forward 组件并填充字段,
再将当前正文与引用/转发上下文投影为一次请求的文本与媒体输入; 引用与转发内容
作为用户提供的资料, 不参与唤醒判断或命令解析
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, cast

from satrap.core.pipeline.attachments import AttachmentResult, render_attachments
from satrap.core.config.platform_policy import policy_default
from satrap.core.components import BaseMessageComponent, Forward, Image, Node, PlatformComponentType, Reply, preferred_media_source
from satrap.core.pipeline.wake_window import PendingImage, PendingMessage, sender_label
from satrap.core.platform.event import MessageEvent
from satrap.core.type import safe_getattr, safe_getattr_str
from satrap.core.log import logger

QUOTE_TEXT_LIMIT = 2000
"""单条引用原文进入模型输入的最大字符数"""
FORWARD_TEXT_LIMIT = 2000
"""单条转发投影进入模型输入的最大字符数"""
FORWARD_RESOLVE_LIMIT = 2
"""每事件最多回源的顶层转发数"""
MEDIA_PLACEHOLDERS: dict[str, str] = {"image": "[图片]", "video": "[视频]"}
"""媒体类型到正文占位的映射, 与平台层生成的占位保持一致"""
MEDIA_FAILED_TEXT: dict[str, str] = {"image": "[图片读取失败]", "video": "[视频读取失败]"}
"""媒体解析失败时覆盖原占位的提示, 让模型不会以为自己已经拿到该媒体"""
MEDIA_FAILED_FEEDBACK = "图片读取失败，暂时无法处理该图片。"
"""纯媒体全部失败且无可读正文时回复的固定文案"""


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
    media_only_unavailable: bool = False
    """消息除失败媒体外没有任何可读内容, 调用方应直接反馈而不调用模型"""


def media_groups(
    components: list[BaseMessageComponent], media_type: str,
) -> list[tuple[str, tuple[BaseMessageComponent, ...]]]:
    """
    从组件列表按来源归组指定类型媒体

    参数:
    - components: 组件列表
    - media_type: image 或 video

    返回:
    - list[tuple[str, tuple[BaseMessageComponent, ...]]]: 来源与共享该来源的组件, 去重保序;
      无来源的组件不产生条目
    """
    groups: dict[str, list[BaseMessageComponent]] = {}
    order: list[str] = []
    for comp in components:
        ctype = safe_getattr(comp, "type")
        ctype_str = str(safe_getattr(ctype, "value", ctype))
        if ctype_str.lower() != media_type:
            continue
        source = preferred_media_source(comp)
        if not source:
            continue
        if source not in groups:
            groups[source] = []
            order.append(source)
        groups[source].append(comp)
    return [(source, tuple(groups[source])) for source in order]


def media_sources(components: list[BaseMessageComponent], media_type: str) -> list[str]:
    """
    从组件列表提取指定类型媒体来源

    参数:
    - components: 组件列表
    - media_type: image 或 video

    返回:
    - list[str]: URL 或文件路径, 去重保序; 无来源的组件不产生条目
    """
    return [source for source, _ in media_groups(components, media_type)]


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


def media_failures(event: MessageEvent) -> dict[str, str]:
    """
    取本次请求的媒体解析失败项

    参数:
    - event: 当前事件

    返回:
    - dict[str, str]: 解析失败的来源到原因码; 未运行解析阶段时为空字典
    """
    results = event.get_extra("media_resolution")
    failures: dict[str, str] = {}
    if not isinstance(results, (list, tuple)):
        return failures
    for item in cast(list[Any], results):
        source = safe_getattr_str(item, "source")
        status = safe_getattr_str(item, "status")
        if source and status and status != "resolved":
            failures[source] = safe_getattr_str(item, "reason") or "media_unavailable"
    return failures


def _components_brief_text(components: list[BaseMessageComponent], failures: dict[str, str] | None = None) -> str:
    """
    生成节点正文的单行摘要, 嵌套转发与引用只保留占位

    参数:
    - components: 节点正文组件
    - failures: 媒体解析失败项, 命中时该媒体输出失败提示而不是普通占位

    返回:
    - str: 供转发投影使用的纯文本摘要
    """
    parts: list[str] = []
    failed = failures or {}
    for comp in components:
        ctype = safe_getattr(comp, "type")
        ctype_str = str(safe_getattr(ctype, "value", ctype)).lower()
        if ctype_str == "plain":
            parts.append(safe_getattr_str(comp, "text"))
        elif ctype_str == "at":
            parts.append(f"@{safe_getattr_str(comp, 'qq')}")
        elif ctype_str in ("image", "video"):
            parts.append(
                MEDIA_FAILED_TEXT[ctype_str] if preferred_media_source(comp) in failed else MEDIA_PLACEHOLDERS[ctype_str]
            )
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


def _rewrite_media_placeholders(
    text: str, components: list[BaseMessageComponent], failures: dict[str, str],
) -> str:
    """
    按组件顺序把解析失败的媒体占位覆盖为失败提示

    参数:
    - text: 由同一批组件生成的正文, 每个媒体组件对应一个占位
    - components: 生成该正文的组件, 顺序必须与正文一致
    - failures: 媒体解析失败项

    返回:
    - str: 失败媒体占位已覆盖的正文; 未失败时原样返回
    """
    if not failures:
        return text
    for comp in components:
        ctype = safe_getattr(comp, "type")
        ctype_str = str(safe_getattr(ctype, "value", ctype)).lower()
        if ctype_str not in MEDIA_FAILED_TEXT:
            continue
        if preferred_media_source(comp) not in failures:
            continue
        # 按顺序逐个替换首个占位, 与组件一一对齐, 不使用无差别的全局替换
        text = text.replace(MEDIA_PLACEHOLDERS[ctype_str], MEDIA_FAILED_TEXT[ctype_str], 1)
    return text


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


@dataclass(frozen=True)
class MediaItem:
    """一条被选中进入模型的媒体及其来源组件"""

    source: str
    """选中时的来源, 同时作为去重键; 解析成功后组件上的 resolved_path 才是实际提交来源"""
    media_type: str
    """image 或 video"""
    origin: str
    """top, quote, forward 或 window"""
    components: tuple[BaseMessageComponent, ...]
    """共享该来源的组件, 解析结果需要写回全部组件以保证去重结论稳定"""


class _MediaBudget:
    """引用与转发媒体共享的数量预算, 超出时记录一次诊断说明"""

    def __init__(self, limit: int, images: list[str], videos: list[str], notes: list[str]) -> None:
        self.remaining = limit
        self.images, self.videos, self.notes = images, videos, notes
        self.items: list[MediaItem] = []
        """并入成功的条目, 供下载侧知道哪些组件需要解析"""

    def merge(self, components: list[BaseMessageComponent], note: str, origin: str = "context") -> None:
        """
        把组件中的图片与视频并入顶层媒体列表

        参数:
        - components: 引用或转发节点的组件
        - note: 预算耗尽时写入的说明
        - origin: 条目归属, quote 或 forward
        """
        for media_type, target in (("image", self.images), ("video", self.videos)):
            for source, grouped in media_groups(components, media_type):
                if self.remaining <= 0:
                    self.notes.append(note)
                    break
                # 预算耗尽只跳过当前媒体类型, 另一类型仍须检查
                if source not in target:
                    target.append(source)
                    self.remaining -= 1
                    self.items.append(MediaItem(source, media_type, origin, grouped))


@dataclass(frozen=True)
class MediaSelection:
    """一次请求的媒体选中结果, 由下载侧与投影侧共用同一份选中真相"""

    items: tuple[MediaItem, ...]
    top_notes: tuple[str, ...] = ()
    quote_notes: tuple[str, ...] = ()
    forward_notes: tuple[tuple[str, ...], ...] = ()
    """按转发顺序逐条记录, 供投影侧在对应转发位置插入预算说明"""
    window_messages: tuple[WindowInput, ...] = ()
    window_notes: tuple[str, ...] = ()
    window_synthetic: bool = False

    def sources(self, media_type: str) -> list[str]:
        """
        取指定类型的选中来源, 顺序与投影写入 ProjectedInput 的顺序一致

        参数:
        - media_type: image 或 video

        返回:
        - list[str]: 选中来源列表
        """
        return [item.source for item in self.items if item.media_type == media_type]


@dataclass(frozen=True)
class WindowInput:
    """本轮临时重建的窗口正文与图片组件, 不写回长期快照"""

    label: str
    parts: tuple[str | Image, ...]


WINDOW_MEDIA_DROPPED = "[图片未纳入本次输入]"
"""未选中或没有来源的窗口图片占位, 计入文本预算"""


def select_window_media(
    event: MessageEvent, selection: MediaSelection, snapshot: tuple[PendingMessage, ...],
    quote_status: str, forward_status: str, attachments: tuple[AttachmentResult, ...], synthetic: bool,
) -> MediaSelection:
    """
    先预留当前输入和完整图片标记, 再按最近消息优先合并窗口媒体

    参数:
    - event: 当前已通过限流的事件
    - selection: 当前消息, 引用及转发的媒体选中结果
    - snapshot: 实际认领的窗口消息, 真实事件已剔除当前消息
    - quote_status: 引用回源状态
    - forward_status: 转发补全状态
    - attachments: 当前事件的附件投影结果
    - synthetic: 是否只使用认领窗口, 不投影合成事件的正文和组件

    返回:
    - MediaSelection: 共用预算的媒体和可完整显示来源的窗口片段
    """
    if not snapshot and not synthetic:
        return selection
    if synthetic:
        selection = MediaSelection((), window_synthetic=True)
    base = project_input(event, quote_status, forward_status, attachments, selection)
    text_limit = int(event.policy_settings.get("input_text_limit", policy_default("input_text_limit")))
    reserve = len(base.message) + (1 if base.message else 0)
    reserve += sum(len(item.components) * len(MEDIA_FAILED_TEXT[item.media_type]) for item in selection.items)
    # 当前媒体失败可能扩大占位, 先保守预留, 避免已下载窗口图片的来源标记被最终裁掉
    remaining = max(0, text_limit - reserve)
    rows: list[WindowInput] = []
    notes: list[str] = []
    for item in reversed(snapshot):
        label = sender_label(item.nickname, item.actor_id, item.message_id)
        header = label + " " if synthetic else f"[先前窗口消息: {label} "
        suffix = "" if synthetic else "]"
        available = remaining - len(header) - len(suffix) - 1
        if available < 1:
            notes.append("window_budget_dropped")
            continue
        parts: list[str | Image] = []
        used = 0
        snapshot_parts = item.parts or (item.text,)
        if item.omitted_images:
            snapshot_parts = (*snapshot_parts, f"[另有 {item.omitted_images} 张图片超出窗口容量]")
            notes.append("window_media_storage_truncated")
        for part in snapshot_parts:
            if isinstance(part, PendingImage):
                if used + len(WINDOW_MEDIA_DROPPED) > available:
                    notes.append("window_budget_truncated")
                    break
                parts.append(Image(file=part.file, url=part.url or None))
                used += len(WINDOW_MEDIA_DROPPED)
            else:
                take = min(len(part), available - used)
                if take:
                    parts.append(part[:take])
                    used += take
                if take < len(part):
                    notes.append("window_budget_truncated")
                    break
        if not parts:
            continue
        rows.append(WindowInput(header, tuple(parts)))
        remaining -= len(header) + len(suffix) + used + 1
    items = list(selection.items)
    media_limit = int(event.policy_settings.get("input_media_limit", policy_default("input_media_limit")))
    for row in rows:
        for part in row.parts:
            if not isinstance(part, Image):
                continue
            source = preferred_media_source(part)
            existing = next((index for index, item in enumerate(items) if item.source == source and item.media_type == "image"), None)
            if source and existing is not None:
                items[existing] = replace(items[existing], components=(*items[existing].components, part))
            elif source and len(items) < media_limit:
                items.append(MediaItem(source, "image", "window", (part,)))
            else:
                notes.append("window_media_truncated" if source else "window_media_unavailable")
    return replace(selection, items=tuple(items), window_messages=tuple(reversed(rows)),
                   window_notes=tuple(dict.fromkeys(notes)), window_synthetic=synthetic)


def select_media(event: MessageEvent, quote_status: str) -> MediaSelection:
    """
    按 input_media_limit 选中会进入模型的媒体, 顶层截断与引用/转发共享同一预算

    参数:
    - event: 已完成引用与转发补全的事件
    - quote_status: resolve_quotes 的结果, 只有 resolved 的引用才参与媒体合并

    返回:
    - MediaSelection: 有序列出去重后的选中条目与各阶段预算说明;
      未解析的引用与转发不产生条目, 与投影的可见性判断保持一致
    """
    top = event.get_messages()
    media_limit = int(event.policy_settings.get("input_media_limit", policy_default("input_media_limit")))
    notes: list[str] = []

    # Step.1 顶层媒体先按预算实际截断, 余量再供引用与转发消耗
    top_images = media_groups(top, "image")
    top_videos = media_groups(top, "video")
    top_notes: tuple[str, ...] = ()
    if len(top_images) + len(top_videos) > media_limit:
        top_videos = top_videos[: max(0, media_limit - len(top_images))]
        top_images = top_images[: media_limit]
        notes.append("top_media_truncated")
        top_notes = ("top_media_truncated",)
    images: list[str] = []
    videos: list[str] = []
    items: list[MediaItem] = []
    for media_type, target, groups in (("image", images, top_images), ("video", videos, top_videos)):
        for source, components in groups:
            target.append(source)
            items.append(MediaItem(source, media_type, "top", components))
    budget = _MediaBudget(media_limit - len(images) - len(videos), images, videos, notes)

    # Step.2 引用媒体并入共享预算
    quote_notes: tuple[str, ...] = ()
    replies = [c for c in top if c.type == PlatformComponentType.Reply]
    if replies and quote_status == "resolved":
        chain = safe_getattr(replies[0], "chain")
        quoted_components = (
            [c for c in cast(list[Any], chain) if isinstance(c, BaseMessageComponent)]
            if isinstance(chain, list) else []
        )
        before = len(notes)
        budget.merge(quoted_components, "quote_media_truncated", "quote")
        quote_notes = tuple(notes[before:])

    # Step.3 逐个转发节点的媒体并入共享预算, 说明按转发分组以便投影就位插入
    forward_notes: list[tuple[str, ...]] = []
    for forward in [c for c in top if c.type == PlatformComponentType.Forward and isinstance(c, Forward)]:
        before = len(notes)
        for node in forward.nodes or []:
            content = safe_getattr(node, "content")
            node_components = (
                [c for c in cast(list[Any], content) if isinstance(c, BaseMessageComponent)]
                if isinstance(content, list) else []
            )
            budget.merge(node_components, "forward_media_truncated", "forward")
        forward_notes.append(tuple(notes[before:]))
    return MediaSelection((*items, *budget.items), top_notes, quote_notes, tuple(forward_notes))


def project_input(
    event: MessageEvent, quote_status: str, forward_status: str = "none", attachments: tuple[AttachmentResult, ...] = (),
    selection: MediaSelection | None = None,
) -> ProjectedInput:
    """
    组装当前正文与引用/转发上下文, 合并顶层与补全内容的媒体

    参数:
    - event: 已完成引用与转发补全的事件
    - quote_status: resolve_quotes 的结果
    - forward_status: resolve_forwards 的结果
    - attachments: resolve_attachments 的结果, 语音转写与文件正文作为资料块前置
    - selection: 已算出的媒体选中结果, 调度器传入解析阶段使用的同一份; 缺省时就地重算

    返回:
    - ProjectedInput: 文本与媒体来源, 引用, 转发与附件内容以明确标记包裹;
      顶层媒体按 input_media_limit 实际裁剪, 文本总量按 input_text_limit 记账拼接
    """
    selection = selection if selection is not None else select_media(event, quote_status)
    top = [] if selection.window_synthetic else event.get_messages()
    failures = media_failures(event)
    images: list[str] = []
    videos: list[str] = []
    failed_types: set[str] = set()
    for item in selection.items:
        # 解析成功的条目提交本地文件, 失败的条目必须排除, 否则模型层会再次对它发起回源
        source = "" if item.source in failures else preferred_media_source(item.components[0]) if item.components else item.source
        if not source:
            failed_types.add(item.media_type)
            continue
        (images if item.media_type == "image" else videos).append(source)
    notes: list[str] = [*selection.top_notes, *selection.window_notes]
    for source, reason in failures.items():
        notes.append(f"media_failed:{reason}" if source else "media_failed")
    message = "" if selection.window_synthetic else _rewrite_media_placeholders(event.get_message_str(), top, failures)
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
            notes.extend(selection.quote_notes)
            quoted_display = _rewrite_media_placeholders(quoted_text, quoted_components, failures) or "(仅含附件)"
            quote_blocks.append(_ContextBlock("quote", f"[引用 {label} 的消息: ", quoted_display, "]"))
        elif quote_status in {"unavailable", "disabled"}:
            quote_blocks.append(_ContextBlock("quote", "[引用了一条无法获取原文的消息]", ""))
            notes.append(f"quote_{quote_status}")
    forwards = [c for c in top if c.type == PlatformComponentType.Forward and isinstance(c, Forward)]
    for index, forward in enumerate(forwards):
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
            lines.append(f"- {name}: {_components_brief_text(node_components, failures) or '(仅含附件)'}")
        if index < len(selection.forward_notes):
            notes.extend(selection.forward_notes[index])
        block = "\n".join(lines)
        if len(block) > FORWARD_TEXT_LIMIT:
            block = block[:FORWARD_TEXT_LIMIT] + "…"
            notes.append("forward_truncated")
        forward_blocks.append(_ContextBlock("forward", f"[转发消息 {len(lines)} 条:\n", block, "]"))
    attachment_status = "none"
    if attachments and not selection.window_synthetic:
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
    blocks = [*attachment_blocks, *forward_blocks, *quote_blocks]
    assembled = ProjectionBudget(text_limit).assemble(message, blocks, notes)
    source_label = ""
    if message and not event.is_private_chat() and not selection.window_synthetic and event.call_origin.actor_kind != "management":
        origin = event.call_origin
        source_label = sender_label(event.get_sender_name(), origin.actor_id, origin.source_message_id) + " "
        if len(source_label) + len(assembled) <= text_limit:
            message = source_label + message
            assembled = ProjectionBudget(text_limit).assemble(message, blocks, notes)
        else:
            source_label = ""
            notes.append("sender_label_budget_dropped")
    message = assembled
    # 判定必须在占位覆盖之后做: 覆盖后的正文非空, 不能用空正文判断是否值得调用模型
    body = message.replace(source_label, "", 1) if source_label else message
    if top and all(component.type in {PlatformComponentType.Image, PlatformComponentType.Video}
                   or (component.type == PlatformComponentType.At and safe_getattr_str(component, "qq") == event.get_self_id())
                   or (component.type == PlatformComponentType.Plain and not safe_getattr_str(component, "text").strip())
                   for component in top):
        body = ""
    # 只有媒体和机器人寻址的当前消息不算可读正文, 发送者标记也不改变该结论
    selected_components = {id(component): item for item in selection.items for component in item.components}
    window_lines: list[str] = []
    for row in selection.window_messages:
        parts: list[str] = []
        for part in row.parts:
            if isinstance(part, str):
                parts.append(part)
                body += part
                continue
            item = selected_components.get(id(part))
            if item is None:
                parts.append(WINDOW_MEDIA_DROPPED)
                failed_types.add("image")
            elif item.source in failures:
                parts.append(MEDIA_FAILED_TEXT["image"])
            else:
                resolved = preferred_media_source(part)
                parts.append(f"[图片 {images.index(resolved) + 1}]" if resolved in images else WINDOW_MEDIA_DROPPED)
        window_lines.append(row.label + "".join(parts).strip() + ("" if selection.window_synthetic else "]"))
    if window_lines:
        message = "\n".join([*([message] if message else []), *window_lines])
    for placeholder in ("[图片]", "[视频]", WINDOW_MEDIA_DROPPED, *MEDIA_FAILED_TEXT.values()):
        body = body.replace(placeholder, "")
    media_only_unavailable = bool(failed_types) and not images and not videos and not body.strip()
    return ProjectedInput(
        message=message, images=tuple(images), videos=tuple(videos),
        quote_status=quote_status, forward_status=forward_status, attachment_status=attachment_status,
        notes=tuple(notes), media_only_unavailable=media_only_unavailable,
    )
