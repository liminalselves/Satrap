"""OneBot 原消息转发, 从可信来源取得消息身份, 不接受模型重写正文"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
import asyncio
import json

from satrap.core.call_context import CallOrigin
from satrap.core.components import BaseMessageComponent, Node, Plain
from satrap.core.config.platform_messages import MessageScope
from satrap.core.config.group_action_origin import bind_group_action_preflight
from satrap.core.message_forward import ForwardError
from satrap.core.platform.event import MessageChain
from satrap.core.platform.onebot.onebot_utils import group_session_id, private_session_id


def conversation(value: Any, origin: CallOrigin) -> dict[str, str]:
    """
    解析通用对话地址, 不接受跨平台或跨机器人账号字段

    参数:
    - value: 群聊或私聊地址, None 使用当前对话
    - origin: 宿主提供的可信来源

    返回:
    - 已校验并归一化的对话类型和 ID, 无效地址抛出 ForwardError
    """
    if value is None:
        kind = origin.conversation_kind or ("group" if origin.chat_type == "GroupMessage" else "private")
        value = {"conversation_kind": kind, "conversation_id": origin.conversation_id or origin.chat_id}
    if not isinstance(value, dict) or set(value) != {"conversation_kind", "conversation_id"}:
        raise ForwardError("invalid_parameters", "对话地址必须包含 conversation_kind 和 conversation_id")
    kind, identity = value["conversation_kind"], value["conversation_id"]
    if kind not in {"group", "private"} or not isinstance(identity, str) or not identity.isascii() or not identity.isdecimal() or int(identity) <= 0:
        raise ForwardError("invalid_parameters", "OneBot 对话类型必须为 group 或 private, 对话 ID 必须为正整数")
    return {"conversation_kind": kind, "conversation_id": str(int(identity))}


class OneBotForwarding:
    """绑定当前适配器账号与连接代次的原消息转发"""

    def __init__(self, adapter: Any, origin: CallOrigin, check: Callable[[], None]) -> None:
        """
        冻结本轮来源账号与连接代次

        参数:
        - adapter: 当前 OneBot 适配器
        - origin: 宿主提供的可信来源
        - check: 执行前重新核验权限的回调
        """
        self.adapter, self.origin, self.check = adapter, origin, check
        self.generation = adapter.connection_generation()

    def verify(self) -> None:
        """平台读取及发送前复核冻结身份和插件权限"""
        self.check()
        if self.adapter.bot_self_id != self.origin.self_id or self.adapter.connection_generation() != self.generation:
            raise ForwardError("stale_account", "转发期间机器人账号或连接已变化")

    async def source_message(self, source: dict[str, str], message_id: Any) -> dict[str, Any]:
        """
        回源完整消息, 私聊无法证明聊天对象时拒绝读取

        参数:
        - source: 已校验的来源对话
        - message_id: 平台原消息 ID, 不接受正文替代

        返回:
        - 平台完整消息, 身份或归属无法确认时抛出 ForwardError
        """
        if not isinstance(message_id, str) or len(message_id) > 32 or not message_id.lstrip('-').isascii() or not message_id.lstrip('-').isdecimal() or int(message_id) == 0:
            raise ForwardError("invalid_parameters", "消息 ID 必须为非零整数字符串")
        self.verify()
        archive = self.adapter.message_archive
        scope = MessageScope(self.origin.adapter_id, self.origin.self_id, source["conversation_kind"], source["conversation_id"])
        saved = await asyncio.to_thread(archive.get, scope, message_id) if archive else None
        if saved and saved["status"] != "active":
            raise ForwardError("source_unavailable", "来源消息已删除, 撤回或超出保留期限")
        payload = await self.adapter.admin._call("get_msg", message_id=int(message_id))
        self.verify()
        if not isinstance(payload, dict) or str(payload.get("message_id", "")) != message_id:
            raise ForwardError("source_unverified", "平台未返回匹配的来源消息")
        if payload.get("self_id") is not None and str(payload["self_id"]) != self.origin.self_id:
            raise ForwardError("source_unverified", "来源消息属于其他机器人账号")
        if source["conversation_kind"] == "group":
            valid = payload.get("message_type") == "group" and str(payload.get("group_id", "")) == source["conversation_id"]
        else:
            raw_sender = payload.get("sender")
            sender = raw_sender if isinstance(raw_sender, dict) else {}
            peer = str(payload.get("target_id") or payload.get("peer_id") or payload.get("user_id") or "")
            inbound = str(sender.get("user_id", "")) == source["conversation_id"]
            peer_matches = not peer or peer in {source["conversation_id"], self.origin.self_id}
            outbound = str(sender.get("user_id", "")) == self.origin.self_id and (peer == source["conversation_id"] or bool(saved and saved.get("verified")))
            valid = payload.get("message_type") == "private" and not payload.get("group_id") and peer_matches and (inbound or outbound)
        if not valid:
            raise ForwardError("source_unverified", "无法确认消息属于指定来源对话")
        return payload

    async def read(self, message_id: str, source: dict[str, str]) -> dict[str, Any]:
        """
        返回转发内容预览, 原始发送完全绕过预览和组件截断

        参数:
        - message_id: 包含转发卡片的原消息 ID
        - source: 已校验的来源对话

        返回:
        - 有界预览与截断标识, 平台未返回有效内容时抛出 ForwardError
        """
        payload = await self.source_message(source, message_id)
        segments = payload.get("message")
        forwards = [item for item in segments if isinstance(item, dict) and item.get("type") == "forward"] if isinstance(segments, list) else []
        if len(forwards) != 1 or not isinstance(forwards[0].get("data"), dict) or not forwards[0]["data"].get("id"):
            raise ForwardError("invalid_source", "来源消息必须包含一个有有效 ID 的合并转发")
        forward_id = str(forwards[0]["data"]["id"])
        self.verify()
        content = await self.adapter.admin._call("get_forward_msg", id=forward_id)
        self.verify()
        if isinstance(content, dict) and (content.get("self_id") is not None and str(content["self_id"]) != self.origin.self_id
                or source["conversation_kind"] == "group" and content.get("group_id") is not None and str(content["group_id"]) != source["conversation_id"]):
            raise ForwardError("source_unverified", "转发回源结果与来源账号或群不一致")
        rows = content.get("messages", content.get("message")) if isinstance(content, dict) else None
        if not isinstance(rows, list):
            raise ForwardError("source_unavailable", "平台未返回转发内容")
        preview = json.dumps(rows[:20], ensure_ascii=False)
        return {"source": source, "source_message_id": message_id, "forward_id": forward_id,
                "preview": preview[:12000], "preview_truncated": len(rows) > 20 or len(preview) > 12000,
                "forwarding_uses_original": True}

    async def send(self, ids: list[str], mode: str, source: dict[str, str], target: dict[str, str]) -> dict[str, Any]:
        """
        发送原消息引用或原转发卡片, 不支持时明确失败

        参数:
        - ids: 1 到 30 个原消息 ID, 顺序交由平台保留
        - mode: merge 合并原消息, existing_forward 转发现有卡片
        - source: 已校验的来源对话
        - target: 已校验的目标对话

        返回:
        - 平台发送回执与原文保留信息, 失败或未知结果不会降级为文字重发
        """
        if not isinstance(ids, list) or not 1 <= len(ids) <= 30 or not all(isinstance(item, str) for item in ids) or len(set(ids)) != len(ids) or mode not in {"merge", "existing_forward"}:
            raise ForwardError("invalid_parameters", "请选择 1 到 30 个不重复消息 ID, mode 为 merge 或 existing_forward")
        if mode == "existing_forward" and len(ids) != 1:
            raise ForwardError("invalid_parameters", "转发已有卡片时只接受一个来源消息 ID")
        payloads = [await self.source_message(source, identity) for identity in ids]
        if mode == "existing_forward":
            segments = payloads[0].get("message")
            if not isinstance(segments, list) or not any(isinstance(item, dict) and item.get("type") == "forward" for item in segments):
                raise ForwardError("invalid_source", "来源消息不是合并转发卡片")
        nodes: list[BaseMessageComponent] = [Node([], id=int(identity), relay_forward=mode == "existing_forward", require_forward=True) for identity in ids]
        session_id = group_session_id(target["conversation_id"]) if target["conversation_kind"] == "group" else private_session_id(target["conversation_id"])
        with bind_group_action_preflight(self.verify):
            receipt = await self.adapter.send_message(session_id, MessageChain(nodes), request_id=self.origin.request_id)
        return {"status": receipt.status, "message_ids": list(receipt.message_ids), "reason": receipt.reason,
                "method": "native_card" if mode == "existing_forward" else "message_reference", "content_modified": False,
                "order": "platform_original_order", "source": source, "target": target}

    async def compose(self, nodes: list[dict[str, Any]], target: dict[str, str]) -> dict[str, Any]:
        """
        创建机器人署名的文字合集, 与原文转发明确分开

        参数:
        - nodes: 1 到 30 段新文字及可选显示名称
        - target: 已校验的目标对话

        返回:
        - 平台发送回执, 不将新内容标记为原文转发
        """
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= 30:
            raise ForwardError("invalid_parameters", "文字合集必须包含 1 到 30 段")
        built: list[BaseMessageComponent] = []
        for item in nodes:
            if not isinstance(item, dict) or set(item) - {"content", "name"} or not isinstance(item.get("content"), str) or not 1 <= len(item["content"]) <= 2000:
                raise ForwardError("invalid_parameters", "每段必须填写 1 到 2000 字符文字")
            built.append(Node(Plain(item["content"]), name=str(item.get("name") or "Satrap")[:30], uin=self.origin.self_id, require_forward=True))
        session_id = group_session_id(target["conversation_id"]) if target["conversation_kind"] == "group" else private_session_id(target["conversation_id"])
        with bind_group_action_preflight(self.verify):
            receipt = await self.adapter.send_message(session_id, MessageChain(built), request_id=self.origin.request_id)
        return {"status": receipt.status, "message_ids": list(receipt.message_ids), "reason": receipt.reason, "method": "composed_text"}
