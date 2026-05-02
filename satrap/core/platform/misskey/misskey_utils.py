from __future__ import annotations

from typing import Any

from satrap.core.components import At, File, Image, Plain, PlatformComponentType, Record, Video
from satrap.core.platform.event import MessageChain
from satrap.core.type import Group, MessageMember, PlatformMessage, PlatformMessageType


class FileIDExtractor:
    """从 Misskey API 响应中提取文件 ID"""

    @staticmethod
    def extract_file_id(result: Any) -> str | None:
        """从多种响应结构中提取文件 ID"""
        if not isinstance(result, dict):
            return None
        candidates = [
            result.get("createdFile", {}).get("id") if isinstance(result.get("createdFile"), dict) else None,
            result.get("file", {}).get("id") if isinstance(result.get("file"), dict) else None,
            result.get("id"),
        ]
        for candidate in candidates:
            if candidate:
                return str(candidate)
        return None


def serialize_message_chain(chain: list[Any] | MessageChain) -> tuple[str, bool]:
    """将 Satrap 消息链序列化为 Misskey 文本"""
    components = chain.components if isinstance(chain, MessageChain) else chain
    text_parts: list[str] = []
    has_at = False

    for component in components:
        component_type = getattr(component, "type", None)
        if component_type == PlatformComponentType.Plain or isinstance(component, Plain):
            text = getattr(component, "text", "")
            if text:
                text_parts.append(str(text))
            continue
        if component_type == PlatformComponentType.At or isinstance(component, At):
            has_at = True
            name = getattr(component, "name", "") or getattr(component, "qq", "")
            if name:
                text_parts.append(f"@{name}")
            continue
        if component_type == PlatformComponentType.Image or isinstance(component, Image):
            text_parts.append("[图片]")
            continue
        if component_type == PlatformComponentType.Record or isinstance(component, Record):
            text_parts.append("[音频]")
            continue
        if component_type == PlatformComponentType.Video or isinstance(component, Video):
            text_parts.append("[视频]")
            continue
        if component_type == PlatformComponentType.File or isinstance(component, File):
            text_parts.append("[文件]")
            continue
        text = getattr(component, "text", None)
        if text:
            text = str(text)
            if "@" in text:
                has_at = True
            text_parts.append(text)

    return "".join(text_parts), has_at


def is_valid_chat_session_id(session_id: str | Any) -> bool:
    """检查是否为 chat%<user_id> 会话"""
    return isinstance(session_id, str) and session_id.startswith("chat%") and len(session_id.split("%", 1)[1]) > 0


def is_valid_room_session_id(session_id: str | Any) -> bool:
    """检查是否为 room%<room_id> 会话"""
    return isinstance(session_id, str) and session_id.startswith("room%") and len(session_id.split("%", 1)[1]) > 0


def is_valid_note_session_id(session_id: str | Any) -> bool:
    """检查是否为 note%<user_id> 会话"""
    return isinstance(session_id, str) and session_id.startswith("note%") and len(session_id.split("%", 1)[1]) > 0


def is_valid_user_session_id(session_id: str | Any) -> bool:
    """检查是否为用户会话"""
    return is_valid_chat_session_id(session_id) or is_valid_note_session_id(session_id)


def extract_user_id_from_session_id(session_id: str) -> str:
    """从 session_id 中提取用户 ID"""
    return session_id.split("%", 1)[1] if "%" in session_id else session_id


def extract_room_id_from_session_id(session_id: str) -> str:
    """从 session_id 中提取房间 ID"""
    return session_id.split("%", 1)[1] if "%" in session_id else session_id


def add_at_mention_if_needed(text: str, user_info: dict[str, Any] | None, has_at: bool = False) -> str:
    """必要时为 note 回复补充 @username"""
    if has_at or not user_info:
        return text
    username = user_info.get("username")
    if not username:
        return text
    mention = f"@{username}"
    if text.startswith(mention):
        return text
    return f"{mention}\n{text}".strip()


def resolve_message_visibility(
    user_id: str | None = None,
    user_cache: dict[str, Any] | None = None,
    self_id: str | None = None,
    raw_message: dict[str, Any] | None = None,
    default_visibility: str = "public",
) -> tuple[str, list[str] | None]:
    """解析 Misskey note 可见性"""
    visibility = default_visibility
    visible_user_ids: list[str] | None = None
    source = raw_message or {}
    if user_id and user_cache and user_id in user_cache:
        source = user_cache[user_id]
    if source:
        visibility = source.get("visibility", default_visibility)
        if visibility == "specified":
            users = list(source.get("visible_user_ids") or source.get("visibleUserIds") or [])
            sender_id = user_id or source.get("userId") or source.get("sender_id")
            if sender_id:
                users.append(str(sender_id))
            if self_id:
                users.append(str(self_id))
            visible_user_ids = sorted({u for u in users if u})
    return visibility, visible_user_ids


def format_poll(poll: dict[str, Any]) -> str:
    """格式化 Misskey 投票内容"""
    if not poll:
        return ""
    choices = poll.get("choices") or []
    text_choices = [
        f"({idx}) {choice.get('text', '')} [{choice.get('votes', 0)}票]"
        for idx, choice in enumerate(choices, start=1)
        if isinstance(choice, dict)
    ]
    if not text_choices:
        return "[投票]"
    mode = "允许多选" if poll.get("multiple") else "单选"
    return f"[投票] {mode} 选项: " + ", ".join(text_choices)


def extract_sender_info(raw_data: dict[str, Any], is_chat: bool = False) -> dict[str, Any]:
    """提取 Misskey 发送者信息"""
    if is_chat:
        sender = raw_data.get("fromUser") or {}
        sender_id = str(sender.get("id") or raw_data.get("fromUserId") or "")
    else:
        sender = raw_data.get("user") or {}
        sender_id = str(sender.get("id") or raw_data.get("userId") or "")
    return {
        "sender": sender,
        "sender_id": sender_id,
        "nickname": sender.get("name") or sender.get("username") or sender_id,
        "username": sender.get("username") or "",
    }


def create_base_message(
    raw_data: dict[str, Any],
    sender_info: dict[str, Any],
    bot_self_id: str,
    *,
    is_chat: bool = False,
    room_id: str | None = None,
) -> PlatformMessage:
    """创建 Satrap 平台消息对象"""
    message = PlatformMessage()
    sender_id = sender_info["sender_id"]
    if room_id:
        message.type = PlatformMessageType.GROUP_MESSAGE
        message.session_id = f"room%{room_id}"
        message.group = Group(group_id=room_id, group_name=str(raw_data.get("toRoom", {}).get("name", "")))
    elif is_chat:
        message.type = PlatformMessageType.FRIEND_MESSAGE
        message.session_id = f"chat%{sender_id or 'unknown'}"
    else:
        message.type = PlatformMessageType.OTHER_MESSAGE
        message.session_id = f"note%{sender_id or 'unknown'}"
    message.self_id = bot_self_id
    message.message_id = str(raw_data.get("id", ""))
    message.sender = MessageMember(user_id=sender_id, nickname=sender_info["nickname"])
    message.message = []
    message.message_str = ""
    message.raw_message = raw_data
    return message


def process_at_mention(
    message: PlatformMessage,
    raw_text: str,
    bot_username: str,
    bot_self_id: str,
) -> tuple[list[str], str]:
    """处理文本中的 bot @ 提及"""
    if not raw_text:
        return [], ""
    if bot_username and raw_text.startswith(f"@{bot_username}"):
        prefix = f"@{bot_username}"
        rest = raw_text[len(prefix):].strip()
        message.message.append(At(qq=bot_self_id, name=bot_username))
        if rest:
            message.message.append(Plain(rest))
            return [rest], rest
        return [], ""
    message.message.append(Plain(raw_text))
    return [raw_text], raw_text


def create_file_component(file_info: dict[str, Any]) -> tuple[Any, str]:
    """创建 Satrap 文件类消息组件"""
    file_url = file_info.get("url") or ""
    file_name = file_info.get("name") or "未知文件"
    file_type = file_info.get("type") or ""
    if file_type.startswith("image/"):
        return Image(file=file_url or file_name, url=file_url), f"图片[{file_name}]"
    if file_type.startswith("audio/"):
        return Record(file=file_url or file_name, url=file_url), f"音频[{file_name}]"
    if file_type.startswith("video/"):
        return Video(file=file_url or file_name, url=file_url), f"视频[{file_name}]"
    return File(name=file_name, url=file_url), f"文件[{file_name}]"


def process_files(message: PlatformMessage, files: list[Any], include_text_parts: bool = True) -> list[str]:
    """处理 Misskey 文件列表"""
    parts: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        component, text = create_file_component(item)
        message.message.append(component)
        if include_text_parts:
            parts.append(text)
    return parts


def cache_user_info(
    user_cache: dict[str, Any],
    sender_info: dict[str, Any],
    raw_data: dict[str, Any],
    bot_self_id: str,
    *,
    is_chat: bool = False,
) -> None:
    """缓存用户上下文, 供回复时恢复可见性和 replyId"""
    sender_id = sender_info["sender_id"]
    if not sender_id:
        return
    if is_chat:
        user_cache[sender_id] = {
            "username": sender_info["username"],
            "nickname": sender_info["nickname"],
            "visibility": "specified",
            "visible_user_ids": [bot_self_id, sender_id],
        }
        return
    user_cache[sender_id] = {
        "username": sender_info["username"],
        "nickname": sender_info["nickname"],
        "visibility": raw_data.get("visibility", "public"),
        "visible_user_ids": raw_data.get("visibleUserIds", []),
        "reply_to_note_id": raw_data.get("id"),
    }


def cache_room_info(user_cache: dict[str, Any], raw_data: dict[str, Any], bot_self_id: str) -> None:
    """缓存房间上下文"""
    room_id = raw_data.get("toRoomId")
    room_data = raw_data.get("toRoom") or {}
    if room_id:
        user_cache[f"room:{room_id}"] = {
            "room_id": room_id,
            "room_name": room_data.get("name", ""),
            "visible_user_ids": [bot_self_id],
        }


async def resolve_component_url_or_path(comp: Any) -> tuple[str | None, str | None]:
    """从消息组件中解析远程 URL 或本地路径"""
    if hasattr(comp, "get_file"):
        try:
            value = await comp.get_file(True)
            if isinstance(value, str) and value.startswith("http"):
                return value, None
        except Exception:
            pass
    for attr in ("url", "file", "path", "src", "source"):
        try:
            value = getattr(comp, attr, None)
        except Exception:
            continue
        if not isinstance(value, str) or not value:
            continue
        if value.startswith("http"):
            return value, None
        return None, value
    if hasattr(comp, "convert_to_file_path"):
        try:
            value = await comp.convert_to_file_path()
            if isinstance(value, str):
                if value.startswith("http"):
                    return value, None
                return None, value
        except Exception:
            pass
    return None, None


async def upload_local_with_retries(
    api: Any,
    local_path: str,
    preferred_name: str | None,
    folder_id: str | None,
) -> str | None:
    """上传本地文件并提取 file id"""
    try:
        result = await api.upload_file(local_path, preferred_name, folder_id)
        if isinstance(result, dict):
            file_id = result.get("id") or FileIDExtractor.extract_file_id(result.get("raw"))
            return str(file_id) if file_id else None
    except Exception:
        return None
    return None
