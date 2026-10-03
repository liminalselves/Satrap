"""OneBot 消息段与 Satrap 消息组件转换工具"""
from __future__ import annotations
import re

from typing import Any, cast
import json
import os

from pydantic import ValidationError

from satrap.core.components import (
    At,
    AtAll,
    BaseMessageComponent,
    Face,
    File,
    Forward,
    Image,
    Json,
    Node,
    Plain,
    Record,
    Reply,
    Unknown,
    Video,
    preferred_media_source,
)
from satrap.core.type import Group, MessageMember, PlatformMessage, PlatformMessageType
from satrap.core.log import logger


PRIVATE_SESSION_PREFIX = "private%"
GROUP_SESSION_PREFIX = "group%"
FORWARD_NODE_LIMIT = 20
"""单条合并转发进入组件层的最大节点数"""
FORWARD_DEPTH_LIMIT = 2
"""内联转发内容的最大展开层数, 超出后保留占位不再解析"""


def private_session_id(user_id: Any) -> str:
    """
    生成 OneBot 私聊会话 ID

    参数:
    - user_id: 用户 ID

    返回:
    - str: 生成 OneBot 私聊会话 ID
    """
    return f"{PRIVATE_SESSION_PREFIX}{user_id}"


def group_session_id(group_id: Any) -> str:
    """
    生成 OneBot 群聊会话 ID

    参数:
    - group_id: 群组 ID

    返回:
    - str: 生成 OneBot 群聊会话 ID
    """
    return f"{GROUP_SESSION_PREFIX}{group_id}"


def extract_private_user_id(session_id: str) -> str:
    """
    从私聊会话 ID 提取 user_id

    参数:
    - session_id: 会话 ID

    返回:
    - str: 从私聊会话 ID 提取 user_id
    """
    return session_id.removeprefix(PRIVATE_SESSION_PREFIX)


def extract_group_id(session_id: str) -> str:
    """
    从群聊会话 ID 提取 group_id

    参数:
    - session_id: 会话 ID

    返回:
    - str: 从群聊会话 ID 提取 group_id
    """
    return session_id.removeprefix(GROUP_SESSION_PREFIX)


def is_private_session(session_id: str) -> bool:
    """
    判断是否为 OneBot 私聊会话 ID

    参数:
    - session_id: 会话 ID

    返回:
    - bool: 判断是否为 OneBot 私聊会话 ID
    """
    return session_id.startswith(PRIVATE_SESSION_PREFIX) and len(session_id) > len(PRIVATE_SESSION_PREFIX)


def is_group_session(session_id: str) -> bool:
    """
    判断是否为 OneBot 群聊会话 ID

    参数:
    - session_id: 会话 ID

    返回:
    - bool: 判断是否为 OneBot 群聊会话 ID
    """
    return session_id.startswith(GROUP_SESSION_PREFIX) and len(session_id) > len(GROUP_SESSION_PREFIX)


def normalize_segments(message: Any) -> list[dict[str, Any]]:
    """
    将 OneBot message 字段统一为 segment 列表

    参数:
    - message: 消息内容

    返回:
    - list[dict[str, Any]]: 将 OneBot message 字段统一为 segment 列表
    """
    if isinstance(message, list):
        return [seg for seg in cast(list[Any], message) if isinstance(seg, dict)]
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]
    return []


def onebot_segments_to_components(segments: list[dict[str, Any]], depth: int = 0) -> tuple[list[BaseMessageComponent], str]:
    """
    将 OneBot 消息段转换为 Satrap 消息组件和可读文本

    参数:
    - segments: 消息段列表
    - depth: 当前所处的转发嵌套层数, 达到 FORWARD_DEPTH_LIMIT 后内联转发只保留占位

    返回:
    - tuple[list[BaseMessageComponent], str]: 将 OneBot 消息段转换为 Satrap 消息组件和可读文本
    """
    components: list[BaseMessageComponent] = []
    text_parts: list[str] = []

    for seg in segments:
        seg_type = str(seg.get("type", "")).lower()
        raw_data = seg.get("data")
        data = cast(dict[str, Any], raw_data) if isinstance(raw_data, dict) else {}

        if seg_type == "text":
            text = str(data.get("text", ""))
            components.append(Plain(text))
            text_parts.append(text)
        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            if qq == "all":
                components.append(AtAll())
                text_parts.append("@全体成员")
            else:
                components.append(At(qq=qq))
                text_parts.append(f"@{qq}")
        elif seg_type == "face":
            components.append(Face(id=data.get("id", "")))
            text_parts.append(f"[表情:{data.get('id', '')}]")
        elif seg_type == "image":
            native = str(data.get("file", ""))
            native = native if re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", native) else ""
            components.append(Image(file=str(data.get("file", "")), url=str(data.get("url", "")), native_media_id=native))
            text_parts.append("[图片]")
        elif seg_type == "record":
            components.append(Record(file=str(data.get("file", "")), url=str(data.get("url", ""))))
            text_parts.append("[语音]")
        elif seg_type == "video":
            components.append(Video(file=str(data.get("file", "")), url=str(data.get("url", ""))))
            text_parts.append("[视频]")
        elif seg_type == "file":
            components.append(
                File(
                    name=str(data.get("name") or data.get("file") or "file"),
                    file=str(data.get("file") or ""),
                    url=str(data.get("url") or ""),
                )
            )
            text_parts.append("[文件]")
        elif seg_type == "reply":
            components.append(Reply(id=str(data.get("id", ""))))
            text_parts.append("[回复]")
        elif seg_type == "forward":
            inline = data.get("content")
            nodes = parse_forward_nodes(cast(list[Any], inline), depth=depth + 1) if isinstance(inline, list) and depth < FORWARD_DEPTH_LIMIT else None
            components.append(Forward(id=str(data.get("id", "")), nodes=nodes))
            text_parts.append("[转发]")
        elif seg_type == "json":
            json_data = data.get("data", {})
            try:
                parsed = json.loads(json_data) if isinstance(json_data, str) else json_data
                if not isinstance(parsed, dict):
                    raise ValueError("JSON 段内容不是对象")
                components.append(Json(cast(dict[str, Any], parsed)))
            except (json.JSONDecodeError, ValueError, ValidationError):
                components.append(Unknown(text=str(json_data)))
            text_parts.append("[JSON]")
        else:
            components.append(Unknown(text=json.dumps(seg, ensure_ascii=False)))
            text_parts.append(f"[{seg_type or 'unknown'}]")

    return components, "".join(text_parts)


def forward_ids_in_message(message: Any, limit: int = 8) -> set[str]:
    """
    提取消息顶层组件中的合并转发 ID, 不递归推断嵌套转发

    参数:
    - message: OneBot message 字段, 支持 segment 列表与纯文本
    - limit: 最多收集的 ID 数, 防止超长消息拖慢归属核验

    返回:
    - set[str]: 顶层 forward 段的 id 集合; 段类型按组件规范同样归一为小写, 空值不计入
    """
    ids: set[str] = set()
    for segment in normalize_segments(message):
        if str(segment.get("type", "")).lower() != "forward":
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        raw = cast(dict[str, Any], data).get("id")
        text = str(raw).strip() if isinstance(raw, (str, int)) and not isinstance(raw, bool) else ""
        if text:
            ids.add(text)
        if len(ids) >= max(1, limit):
            break
    return ids


def parse_forward_nodes(items: list[Any], limit: int = FORWARD_NODE_LIMIT, depth: int = 1) -> list[Node]:
    """
    将标准或实现特定的转发节点字段归一为 Node 列表

    参数:
    - items: get_forward_msg 响应 messages 字段或消息段内联 content 字段
    - limit: 保留的最大节点数, 超出部分丢弃
    - depth: 当前节点所处的嵌套层数, 传递给正文转换以限制内联转发展开

    返回:
    - list[Node]: 按原顺序排列的节点; 节点正文递归复用消息段转换, 嵌套转发不再展开
    """
    nodes: list[Node] = []
    for item in items[:max(0, limit)]:
        if not isinstance(item, dict):
            continue
        entry = cast(dict[str, Any], item)
        raw = entry.get("data") if str(entry.get("type", "")).lower() == "node" else entry
        data = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        content = data.get("content", data.get("message"))
        components: list[BaseMessageComponent]
        if isinstance(content, list):
            components = onebot_segments_to_components(
                [seg for seg in cast(list[Any], content) if isinstance(seg, dict)], depth=depth,
            )[0]
        elif isinstance(content, str):
            components = [Plain(content)]
        else:
            components = []
        raw_time = data.get("time")
        nodes.append(Node(
            components,
            name=str(data.get("nickname") or data.get("name") or ""),
            uin=str(data.get("user_id") or data.get("uin") or ""),
            time=int(raw_time) if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool) else 0,
        ))
    return nodes


async def component_to_onebot_segment(component: BaseMessageComponent) -> dict[str, Any]:
    """
    将 Satrap 消息组件转换为 OneBot 消息段

    参数:
    - component: 消息组件

    返回:
    - dict[str, Any]: 将 Satrap 消息组件转换为 OneBot 消息段
    """
    if isinstance(component, Plain):
        return {"type": "text", "data": {"text": component.text}}
    if isinstance(component, At):
        return {"type": "at", "data": {"qq": str(component.qq)}}
    if isinstance(component, Face):
        return {"type": "face", "data": {"id": str(component.id)}}
    if isinstance(component, Image):
        return {"type": "image", "data": {"file": _normalize_file_source(preferred_media_source(component))}}
    if isinstance(component, Record):
        return {"type": "record", "data": {"file": _normalize_file_source(preferred_media_source(component))}}
    if isinstance(component, Video):
        return {"type": "video", "data": {"file": _normalize_file_source(preferred_media_source(component))}}
    if isinstance(component, File):
        return await component.to_dict()
    if isinstance(component, Reply):
        return {"type": "reply", "data": {"id": str(component.id)}}
    if isinstance(component, Json):
        return {"type": "json", "data": {"data": json.dumps(component.data, ensure_ascii=False)}}
    if isinstance(component, Unknown):
        return {"type": "text", "data": {"text": component.text}}
    return await component.to_dict()


async def message_chain_to_onebot_segments(components: list[BaseMessageComponent]) -> list[dict[str, Any]]:
    """
    将消息链转换为 OneBot segment 列表

    参数:
    - components: 消息组件列表

    返回:
    - list[dict[str, Any]]: 将消息链转换为 OneBot segment 列表
    """
    return [await component_to_onebot_segment(comp) for comp in components]


def create_platform_message(raw_event: dict[str, Any], self_id: str) -> PlatformMessage:
    """
    从 OneBot 原始事件创建 PlatformMessage

    参数:
    - raw_event: raw事件
    - self_id: 自身 ID

    返回:
    - PlatformMessage: 从 OneBot 原始事件创建 PlatformMessage
    """
    message = PlatformMessage()
    message.raw_message = raw_event
    message.self_id = str(raw_event.get("self_id") or self_id or "")
    raw_id = raw_event.get("message_id")
    message.message_id = str(raw_id) if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else ""
    try:
        message.timestamp = int(raw_event.get("time") or message.timestamp)
        if raw_event.get("time"):
            message.timestamp_source = "platform"
    except (TypeError, ValueError, OverflowError) as exc:
        logger.warning(f"[OneBot] 消息时间无效, 使用接收时间, 原因={type(exc).__name__}")
    # time 字段非数字时保留默认时间戳, 不丢整条消息

    raw_sender = raw_event.get("sender")
    sender = cast(dict[str, Any], raw_sender) if isinstance(raw_sender, dict) else {}
    user_id = str(raw_event.get("user_id") or sender.get("user_id") or "")
    nickname = str(sender.get("nickname") or sender.get("card") or user_id)
    message.sender = MessageMember(user_id=user_id, nickname=nickname, card=str(sender.get("card") or ""))

    segments = normalize_segments(raw_event.get("message"))
    message.message, message.message_str = onebot_segments_to_components(segments)

    message_type = str(raw_event.get("message_type", "")).lower()
    if message_type == "group":
        group_id = str(raw_event.get("group_id", ""))
        message.type = PlatformMessageType.GROUP_MESSAGE
        message.session_id = group_session_id(group_id)
        message.group = Group(group_id=group_id, group_name=group_id)
    elif message_type == "private":
        message.type = PlatformMessageType.FRIEND_MESSAGE
        message.session_id = private_session_id(user_id)
        message.group = None
    else:
        message.type = PlatformMessageType.OTHER_MESSAGE
        message.session_id = private_session_id(user_id) if user_id else str(raw_event.get("session_id", ""))
        message.group = None

    return message


def _normalize_file_source(source: str) -> str:
    """
    将本地路径转换为 OneBot 可识别的 file:// 来源

    参数:
    - source: 来源

    返回:
    - str: 将本地路径转换为 OneBot 可识别的 file:// 来源
    """
    if not source:
        return source
    if source.startswith(("http://", "https://", "file://", "base64://")):
        return source
    try:
        if os.path.exists(source):
            return f"file:///{os.path.abspath(source)}"
    except ValueError:
        pass
    # 含 NUL 等非法路径按原样透传, 由平台侧报错
    return source
