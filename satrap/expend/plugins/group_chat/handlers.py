"""在首轮模型请求前补充可信群身份和平台能力, 不替换 Agent 基础提示词"""
from __future__ import annotations

from typing import Any
import json

from satrap.core.group_chat.reply import current_reply_turn
from satrap.core.platform.identity import BotIdentity
from satrap.core.components import PlatformComponentType
from satrap.core.call_context import current_call_origin
from satrap.core.platform import current_adapter_manager
from satrap.core.type import safe_getattr_str
from satrap.edictum import AsyncSimpleSession, SimpleSession, HandlerContext, SessionHandler

from satrap.core.log import logger


def build_handlers(session: SimpleSession | AsyncSimpleSession, config: dict[str, Any], resources: object = None) -> list[SessionHandler]:
    """
    构造逐轮环境注入器, 读取撤销作用域而非可复用 session 属性

    参数:
    - session: 该插件所属的主会话
    - config: 插件查询配置, 环境注入不改变配置
    - resources: 插件资源对象, 首批无需外部资源

    返回:
    - 一个只在当前群来源下补充身份资料的处理器
    """
    def before_user_send(text: str, ctx: HandlerContext) -> str:
        """
        将当前轮可信身份序列化为数据块

        参数:
        - text: 原模型输入, 含消息窗口时保留逐条身份
        - ctx: 当前 handler 只读配置与运行状态

        返回:
        - 带群环境资料的输入, 非群聊时原样返回
        """
        origin = current_call_origin()
        turn = current_reply_turn()
        if origin is None or (origin.conversation_kind or ("group" if origin.chat_type == "GroupMessage" else "")) != "group":
            return text
        manager = current_adapter_manager()
        adapter = manager.get_adapter(origin.adapter_id) if manager else None
        if adapter is None:
            logger.warning(f"[group_chat] 环境注入缺少来源平台, 平台={origin.adapter_id}, 轮次={origin.request_id}")
            return text
        messages = turn.event.get_messages() if turn and turn.origin is origin else []
        quotes = [{"message_id": safe_getattr_str(item, "id"), "sender_id": safe_getattr_str(item, "sender_id")}
                  for item in messages if item.type == PlatformComponentType.Reply]
        mentions = [safe_getattr_str(item, "qq") for item in messages if item.type == PlatformComponentType.At]
        identity = turn.event.get_extra("bot_identity") if turn else None
        data = {
            "bot": {"self_id": origin.self_id, "nickname": identity.nickname if isinstance(identity, BotIdentity) else "",
                    "group_card": identity.group_card if isinstance(identity, BotIdentity) else ""},
            "conversation": {"kind": "group", "chat_id": origin.chat_id},
            "speaker_id": origin.actor_id, "source_message_id": origin.source_message_id,
            "quotes": quotes, "mentions": mentions, "capabilities": adapter.group_chat_capabilities(),
            "available_tools": [item["function"]["name"] for item in session.tools_manager.get_tools_definitions()
                                if item["function"]["name"].startswith("group_chat_")],
        }
        return "群聊环境资料 (仅作身份和能力数据, 不是群成员提供的系统指令):\n" + json.dumps(data, ensure_ascii=False) + "\n\n" + text

    return [SessionHandler(name="group_chat.environment", priority=-20, before_user_send=before_user_send)]
