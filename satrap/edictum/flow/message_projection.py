"""平台事件和档案记录到 FlowMessage 的纯投影"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from satrap.core.type.flow.ports import FlowMessage

if TYPE_CHECKING:
    from satrap.core.type.archive import MessageScope


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
