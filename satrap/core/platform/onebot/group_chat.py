"""OneBot 群聊读取协议, 对成员和消息回包核验身份, 不执行管理写操作"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
import asyncio
import json
import math
import re
import time
from dataclasses import replace

from satrap.core.config.platform_messages import MessageScope
from satrap.core.group_chat.types import GroupChatError, MemberRecord, MemberSnapshot, VerifiedMember, VerifiedMessage
from satrap.core.platform.message_archive import archive_snapshot
from satrap.core.platform.onebot.admin import AdminActionRejected, AdminActionUnconfirmed, UnsupportedAdminAction
from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.core.platform.onebot.adapter import OneBotAdapter


def _numeric_id(value: object, *, message: bool = False) -> str:
    """
    校验 OneBot 的数字身份, 消息 ID 允许一个负号

    参数:
    - value: 原始协议字段或宿主工具参数
    - message: 是否使用允许负值的消息 ID 规则

    返回:
    - 规范化十进制字符串, 非法值抛出 ValueError
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("OneBot 身份必须是十进制 ID")
    text = str(value)
    if not re.fullmatch(r"-?[0-9]{1,20}" if message else r"[0-9]{1,20}", text):
        raise ValueError("OneBot 身份必须是十进制 ID")
    number = int(text)
    if not message and number <= 0:
        raise ValueError("OneBot 成员与群 ID 必须为正数")
    return str(number)


class OneBotGroupChatReader:
    """复用现有协议调用和并发槽位, 读取结果带可信对话归属"""

    def __init__(self, adapter: OneBotAdapter) -> None:
        """
        绑定当前适配器实例

        参数:
        - adapter: 当前实际处理来源消息的实例
        """
        self.adapter = adapter

    def _scope(self, scope: MessageScope) -> None:
        """
        在每次网络调用前后核验账号和群范围

        参数:
        - scope: 宿主固定的群身份
        """
        adapter = self.adapter
        if (scope.adapter_id != adapter.config.id or scope.self_id != adapter.bot_self_id
                or scope.conversation_kind != "group" or not adapter.allows_group(scope.chat_id)):
            raise GroupChatError("stale_call", "来源群或机器人账号已经失效")
        if _numeric_id(scope.chat_id) != scope.chat_id:
            raise GroupChatError("wrong_conversation", "来源群身份不符合协议")

    async def _read(self, scope: MessageScope, action: str, *, max_bytes: int, **params: Any) -> Any:
        """
        有界读取协议响应并拒绝连接切换期间的迟到结果

        参数:
        - scope: 当前可信群身份
        - action: 只读协议动作
        - max_bytes: 允许解析的最大 UTF-8 JSON 字节数
        - params: 已规范化的协议参数

        返回:
        - 在同一账号和连接上读取的协议结果
        """
        self._scope(scope)
        adapter = self.adapter
        client, generation = adapter.get_client(), adapter.connection_generation()
        if client is None:
            raise GroupChatError("unavailable", "平台客户端未连接", retryable=True)

        async def lookup() -> Any:
            """将等待并发槽位计入整次读取的超时"""
            async with adapter._message_lookup_slots:
                self._scope(scope)
                if adapter.get_client() is not client or adapter.connection_generation() != generation:
                    raise GroupChatError("stale_call", "平台连接已切换, 取消旧读取请求")
                return await adapter.admin._call(action, timeout=5, **params)

        try:
            result = await asyncio.wait_for(lookup(), timeout=5)
        except UnsupportedAdminAction as exc:
            raise GroupChatError("unsupported", "当前平台实现不支持该读取接口") from exc
        except (AdminActionUnconfirmed, asyncio.TimeoutError) as exc:
            logger.warning(f"[群聊读取] 平台读取暂不可用, 平台={adapter.config.id}, 动作={action}, 原因={type(exc).__name__}")
            raise GroupChatError("unavailable", "平台读取超时或连接暂不可用", retryable=True) from exc
        except AdminActionRejected as exc:
            raise GroupChatError("unavailable", "平台拒绝该读取请求") from exc
        self._scope(scope)
        if adapter.get_client() is not client or adapter.connection_generation() != generation:
            raise GroupChatError("stale_call", "平台连接已切换, 丢弃旧读取结果")
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > max_bytes:
            raise GroupChatError("unavailable", "平台读取响应超出大小限制")
        return result

    @staticmethod
    def _member(data: dict[str, Any]) -> MemberRecord:
        """
        收窄成员字段, 保留昵称与名片并限制长度

        参数:
        - data: 已核验群归属的成员响应

        返回:
        - 不包含管理权限的成员资料
        """
        user_id = _numeric_id(data.get("user_id"))
        nickname, card = data.get("nickname", ""), data.get("card", "")
        if not isinstance(nickname, str) or not isinstance(card, str):
            raise ValueError("成员昵称或名片格式不符")
        return MemberRecord(user_id, nickname[:512], card[:512])

    async def members(self, scope: MessageScope) -> MemberSnapshot:
        """
        读取完整成员列表, 无效条目和限制均显式标为不完整

        参数:
        - scope: 当前可信群身份

        返回:
        - 最多 10000 项成员及完整性说明, 不沿用管理工具的静默 2048 项限制
        """
        result = await self._read(scope, "get_group_member_list", max_bytes=2 * 1024 * 1024, group_id=int(scope.chat_id))
        if not isinstance(result, list):
            raise GroupChatError("unavailable", "平台成员列表格式不符")
        members: list[MemberRecord] = []
        seen: set[str] = set()
        incomplete = len(result) > 10000
        for data in result[:10000]:
            try:
                if not isinstance(data, dict):
                    raise ValueError("成员条目不是对象")
                if "group_id" in data and _numeric_id(data["group_id"]) != scope.chat_id:
                    raise ValueError("成员条目属于其他群")
                if "self_id" in data and _numeric_id(data["self_id"]) != scope.self_id:
                    raise ValueError("成员条目属于其他账号")
                member = self._member(data)
                if member.user_id in seen:
                    raise ValueError("成员列表包含重复身份")
                seen.add(member.user_id)
                members.append(member)
            except ValueError:
                incomplete = True
        if incomplete:
            logger.warning(f"[群聊读取] 成员列表不完整, 平台={self.adapter.config.id}, 群={scope.chat_id}")
        return MemberSnapshot(scope, tuple(members), time.time(), not incomplete, incomplete,
                              "invalid_or_limited_entries" if incomplete else None)

    async def member(self, scope: MessageScope, user_id: str) -> VerifiedMember:
        """
        核验单个成员回包的群和成员身份

        参数:
        - scope: 当前可信群身份
        - user_id: 待核验的成员 ID

        返回:
        - 带群归属证明的成员资料, 身份缺失或矛盾时拒绝
        """
        if _numeric_id(user_id) != user_id:
            raise ValueError("成员 ID 必须使用平台返回的规范形式")
        result = await self._read(scope, "get_group_member_info", max_bytes=16384,
                                  group_id=int(scope.chat_id), user_id=int(user_id), no_cache=True)
        try:
            if (not isinstance(result, dict) or _numeric_id(result.get("group_id")) != scope.chat_id
                    or _numeric_id(result.get("user_id")) != user_id
                    or ("self_id" in result and _numeric_id(result["self_id"]) != scope.self_id)):
                raise ValueError("成员回包身份与请求不一致")
            member = self._member(result)
        except ValueError as exc:
            raise GroupChatError("unverified_target", "平台未返回可核验的当前群成员身份") from exc
        return VerifiedMember(scope, member, time.time())

    async def message(self, scope: MessageScope, message_id: str) -> VerifiedMessage:
        """
        回源当前群消息并核验 ID, 群, 账号, 发送者和原始时间

        参数:
        - scope: 当前可信群身份
        - message_id: 平台返回的规范消息 ID

        返回:
        - 已核验的消息快照, 不下载附件或引用消息
        """
        if _numeric_id(message_id, message=True) != message_id:
            raise ValueError("消息 ID 必须使用平台返回的规范形式")
        result = await self._read(scope, "get_msg", max_bytes=65536, message_id=int(message_id))
        try:
            if (not isinstance(result, dict) or result.get("message_type") != "group"
                    or _numeric_id(result.get("group_id")) != scope.chat_id
                    or _numeric_id(result.get("message_id"), message=True) != message_id
                    or ("self_id" in result and _numeric_id(result["self_id"]) != scope.self_id)):
                raise ValueError("消息回包身份与请求不一致")
            sender = result.get("sender")
            if not isinstance(sender, dict):
                raise ValueError("消息缺少发送者")
            sender_id = _numeric_id(sender.get("user_id"))
            if "user_id" in result and _numeric_id(result["user_id"]) != sender_id:
                raise ValueError("消息发送者字段矛盾")
            timestamp = result.get("time")
            if (not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
                    or not math.isfinite(timestamp) or timestamp < 0):
                raise ValueError("消息缺少原始平台时间")
        except ValueError as exc:
            raise GroupChatError("unverified_target", "平台未返回可核验的当前群消息身份与时间") from exc
        message = await self.adapter.convert_message({**result, "self_id": scope.self_id, "user_id": sender_id})
        message.timestamp_source = "platform"
        snapshot = archive_snapshot(message, direction="outbound" if sender_id == scope.self_id else "inbound")
        return VerifiedMessage(scope, replace(snapshot, message_time=float(timestamp), source="adapter_lookup"))
