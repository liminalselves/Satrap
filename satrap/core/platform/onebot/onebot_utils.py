from __future__ import annotations

import json
import os
from typing import Any, cast

from satrap.core.components import (
    At,
    AtAll,
    BaseMessageComponent,
    Face,
    File,
    Image,
    Json,
    Plain,
    Record,
    Reply,
    Unknown,
    Video,
)
from satrap.core.type import Group, MessageMember, PlatformMessage, PlatformMessageType


PRIVATE_SESSION_PREFIX = "private%"
GROUP_SESSION_PREFIX = "group%"


def private_session_id(user_id: Any) -> str:
    """生成 OneBot 私聊会话 ID"""
    return f"{PRIVATE_SESSION_PREFIX}{user_id}"


def group_session_id(group_id: Any) -> str:
    """生成 OneBot 群聊会话 ID"""
    return f"{GROUP_SESSION_PREFIX}{group_id}"


def extract_private_user_id(session_id: str) -> str:
    """从私聊会话 ID 提取 user_id"""
    return session_id.removeprefix(PRIVATE_SESSION_PREFIX)


def extract_group_id(session_id: str) -> str:
    """从群聊会话 ID 提取 group_id"""
    return session_id.removeprefix(GROUP_SESSION_PREFIX)


def is_private_session(session_id: str) -> bool:
    """判断是否为 OneBot 私聊会话 ID"""
    return session_id.startswith(PRIVATE_SESSION_PREFIX) and len(session_id) > len(PRIVATE_SESSION_PREFIX)


def is_group_session(session_id: str) -> bool:
    """判断是否为 OneBot 群聊会话 ID"""
    return session_id.startswith(GROUP_SESSION_PREFIX) and len(session_id) > len(GROUP_SESSION_PREFIX)


def normalize_segments(message: Any) -> list[dict[str, Any]]:
    """将 OneBot message 字段统一为 segment 列表"""
    if isinstance(message, list):
        return [seg for seg in message if isinstance(seg, dict)]
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]
    return []


def onebot_segments_to_components(segments: list[dict[str, Any]]) -> tuple[list[BaseMessageComponent], str]:
    """将 OneBot 消息段转换为 Satrap 消息组件和可读文本"""
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
            source = str(data.get("url") or data.get("file") or "")
            components.append(Image(file=source, url=str(data.get("url", ""))))
            text_parts.append("[图片]")
        elif seg_type == "record":
            components.append(Record(file=str(data.get("url") or data.get("file") or "")))
            text_parts.append("[语音]")
        elif seg_type == "video":
            components.append(Video(file=str(data.get("url") or data.get("file") or "")))
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
        elif seg_type == "json":
            json_data = data.get("data", {})
            try:
                components.append(Json(json.loads(json_data) if isinstance(json_data, str) else json_data))
            except json.JSONDecodeError:
                components.append(Unknown(text=str(json_data)))
            text_parts.append("[JSON]")
        else:
            components.append(Unknown(text=json.dumps(seg, ensure_ascii=False)))
            text_parts.append(f"[{seg_type or 'unknown'}]")

    return components, "".join(text_parts)


async def component_to_onebot_segment(component: BaseMessageComponent) -> dict[str, Any]:
    """将 Satrap 消息组件转换为 OneBot 消息段"""
    if isinstance(component, Plain):
        return {"type": "text", "data": {"text": component.text}}
    if isinstance(component, At):
        return {"type": "at", "data": {"qq": str(component.qq)}}
    if isinstance(component, Face):
        return {"type": "face", "data": {"id": str(component.id)}}
    if isinstance(component, Image):
        return {"type": "image", "data": {"file": _normalize_file_source(component.file or component.url or "")}}
    if isinstance(component, Record):
        return {"type": "record", "data": {"file": _normalize_file_source(component.file or component.url or "")}}
    if isinstance(component, Video):
        return {"type": "video", "data": {"file": _normalize_file_source(component.file or component.url or "")}}
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
    """将消息链转换为 OneBot segment 列表"""
    return [await component_to_onebot_segment(comp) for comp in components]


def create_platform_message(raw_event: dict[str, Any], self_id: str) -> PlatformMessage:
    """从 OneBot 原始事件创建 PlatformMessage"""
    message = PlatformMessage()
    message.raw_message = raw_event
    message.self_id = str(raw_event.get("self_id") or self_id or "")
    message.message_id = str(raw_event.get("message_id", ""))
    message.timestamp = int(raw_event.get("time") or message.timestamp)

    raw_sender = raw_event.get("sender")
    sender = cast(dict[str, Any], raw_sender) if isinstance(raw_sender, dict) else {}
    user_id = str(raw_event.get("user_id") or sender.get("user_id") or "")
    nickname = str(sender.get("nickname") or sender.get("card") or user_id)
    message.sender = MessageMember(user_id=user_id, nickname=nickname)

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
    """将本地路径转换为 OneBot 可识别的 file:// 来源"""
    if not source:
        return source
    if source.startswith(("http://", "https://", "file://", "base64://")):
        return source
    if os.path.exists(source):
        return f"file:///{os.path.abspath(source)}"
    return source
