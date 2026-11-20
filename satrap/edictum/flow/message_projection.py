"""平台事件和档案记录到 FlowMessage 的纯投影"""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from satrap.core.type.flow.ports import FlowMessage

if TYPE_CHECKING:
    from satrap.core.type.archive import MessageScope


def render_flow_message(message: FlowMessage, *, private: bool = False, current_id: str | None = None) -> str:
    """
    把一条消息渲染成交给模型的一行; 标记与非流程会话的输入投影一致 (input_projection.project_input)

    参数:
    - message: 档案或事件投影出的消息
    - private: 私聊对话; 只有一个对方, 与非流程会话一样不加来源标记
    - current_id: 本次触发消息的 ID; None 表示没有当前消息 (阈值批次, 交接),
      对应非流程会话的合成窗口

    返回:
    - str: 群聊当前消息或合成窗口为 "[用户 昵称 (ID x), 消息 y] 正文",
      有当前消息时其余消息为 "[先前窗口消息: [用户 …] 正文]"; 私聊用户发言为原文;
      档案里的机器人发言与用户发言混排, 来源标为 "[机器人自己, 消息 y]", 用词与引用块一致
    """
    from satrap.core.pipeline.wake_window import display_label, sender_label
    if message.is_bot:
        label = f"[机器人自己, 消息 {display_label(message.message_id)}]"
    elif private:
        return message.text
    else:
        label = sender_label(message.nickname, message.sender_id, message.message_id)
    if current_id is None or message.message_id == current_id:
        return f"{label} {message.text}"
    return f"[先前窗口消息: {label} {message.text}]"


def render_flow_messages(
    messages: Iterable[FlowMessage], *, private: bool = False, current_id: str | None = None,
) -> str:
    """按行渲染多条消息, 参数同 render_flow_message"""
    return "\n".join(render_flow_message(message, private=private, current_id=current_id) for message in messages)


def trigger_message_id(event: Any) -> str | None:
    """
    取流程事件的当前消息 ID

    参数:
    - event: FlowEvent; 唤醒事件带触发消息, 阈值批次与交接没有

    返回:
    - str | None: 唤醒事件的消息 ID, 其余事件为 None
    """
    if getattr(event, "kind", "") != "message":
        return None
    message = getattr(event, "message", None)
    return str(getattr(message, "message_id", "") or "") or None


def flow_message_from_event(event: Any) -> FlowMessage:
    """把平台消息事件转成 FlowMessage; mentions_bot 用平台解析结果而不是裸扫 @"""
    text = event.get_message_str() or ""
    mentions = False
    try:
        from satrap.core.pipeline.wake_policy import is_self_mention
        mentions = any(
            is_self_mention(component, event.call_origin.self_id) for component in event.get_messages()
        )
    except Exception:
        mentions = False
    if bool(getattr(event, "is_private_chat", lambda: False)()):
        mentions = True
    return FlowMessage(
        message_id=str(event.call_origin.source_message_id or ""),
        position=0,
        sender_id=str(event.call_origin.actor_id or ""),
        nickname=str(getattr(getattr(event, "sender", None), "nickname", "") or ""),
        text=text,
        is_bot=False,
        mentions_bot=mentions,
        time=event.platform_message.received_at,
    )


def flow_message_from_archive(item: Any, scope: MessageScope | None) -> FlowMessage:
    """档案消息转 FlowMessage; @ 只读结构化 mentions, 私聊恒为 True"""
    mentions = bool(scope and str(scope.self_id) in {
        str(value) for value in (item.get("mentions") or ())
    })
    if scope is not None and scope.conversation_kind == "private":
        mentions = True
    return FlowMessage(
        message_id=str(item.get("message_id") or ""),
        position=int(item.get("seq") or 0),
        sender_id=str(item.get("sender_id") or ""),
        nickname=str(item.get("nickname") or ""),
        text=str(item.get("text") or ""),
        is_bot=item.get("direction") == "outbound",
        mentions_bot=mentions,
        time=float(item.get("message_time") or 0.0),
    )
