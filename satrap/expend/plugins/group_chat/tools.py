"""
group_chat 同步与异步工具

工具只把参数交给当前群宿主, 不直接访问平台客户端;
同步会话将协程提交到入站事件循环, 保留可信轮次和主工作流身份
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import asyncio
import copy
import traceback

from satrap.core.group_chat.service import group_chat_service
from satrap.core.group_chat.reply import current_reply_turn
from satrap.core.group_chat.types import GroupChatLimits
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.call_context import current_call_origin
from satrap.core.platform import current_adapter_manager

from satrap.core.log import logger


def _string(description: str) -> dict[str, object]:
    """
    创建长度有限的字符串参数声明

    参数:
    - description: 参数用途

    返回:
    - 字符串 JSON schema
    """
    return {"type": "string", "description": description, "minLength": 1, "maxLength": 256}


_MESSAGE_ID = _string("当前群的消息 ID, 不得捏造或使用其他对话的 ID")
_MEMBER_ID = _string("已确认的当前群成员 ID")
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "description": "查询条数, 默认 20, 受插件上限约束"}
_CURSOR = _string("上次查询返回的 next_cursor, 只能用于相同对话及筛选条件")
_CURSOR["maxLength"] = 4096
_COMPONENTS = {
    "type": "array", "minItems": 1, "maxItems": 64,
    "description": "最终回复组件, 可组合文本, 一个引用与多个提及; prepared 仅表示暂存",
    "items": {"oneOf": [
        {"type": "object", "properties": {"type": {"const": "text"}, "text": {"type": "string", "minLength": 1, "maxLength": 100000}}, "required": ["type", "text"], "additionalProperties": False},
        {"type": "object", "properties": {"type": {"const": "quote"}, "message_id": _MESSAGE_ID}, "required": ["type", "message_id"], "additionalProperties": False},
        {"type": "object", "properties": {"type": {"const": "mention"}, "source_message_id": _MESSAGE_ID}, "required": ["type", "source_message_id"], "additionalProperties": False},
        {"type": "object", "properties": {"type": {"const": "mention"}, "user_id": _MEMBER_ID}, "required": ["type", "user_id"], "additionalProperties": False},
    ]},
}
DEFINITIONS: dict[str, tuple[str, dict[str, object], list[str]]] = {
    "group_chat_reply": ("准备本轮唯一的结构化最终回复; 成功结束后由宿主发送, 不代表已送达", {"components": _COMPONENTS}, ["components"]),
    "group_chat_find_members": ("按当前群昵称或名片查找成员, 重名返回候选, 不自动选择", {"query": _string("昵称或群名片关键词"), "limit": {**_LIMIT, "maximum": 50, "description": "返回条数, 默认 10"}, "cursor": _CURSOR}, ["query"]),
    "group_chat_get_member": ("核实当前群指定成员的身份资料", {"user_id": _MEMBER_ID}, ["user_id"]),
    "group_chat_get_message": ("读取当前群消息及发送者, 缺失时核验回源, 尊重本地删除", {"message_id": _MESSAGE_ID}, ["message_id"]),
    "group_chat_recent_messages": ("补取当前群最近讨论, 返回消息 ID, 发送者及采集覆盖范围", {"limit": _LIMIT, "before_message_id": _MESSAGE_ID, "cursor": _CURSOR}, []),
    "group_chat_search_messages": ("按关键词, 成员与时间的交集搜索当前群消息档案", {
        "keyword": _string("普通文本关键词"), "sender_id": _MEMBER_ID,
        "start_time": _string("起始时间, 含时区的 ISO 8601"), "end_time": _string("结束时间, 含时区的 ISO 8601"),
        "limit": _LIMIT, "cursor": _CURSOR,
    }, []),
}


class _GroupChatMixin:
    """在工具边界处理配置与来源, 不把读取失败包装为空成功"""

    tool_name: str | None
    config: Mapping[str, object]
    deferred_platform_reply = False

    def get_tool_defined(self) -> dict[str, Any]:
        """
        返回完整组件和可选参数 schema

        返回:
        - 六个首批工具的声明之一
        """
        name = str(self.tool_name)
        description, properties, required = DEFINITIONS[name]
        return {"type": "function", "function": {"name": name, "description": description,
                "parameters": {"type": "object", "properties": copy.deepcopy(properties),
                               "required": list(required), "additionalProperties": False}}}

    async def _run(self, kwargs: Mapping[str, object]) -> dict[str, Any]:
        """
        使用安装配置提供的查询预算执行宿主操作

        参数:
        - kwargs: 模型参数

        返回:
        - 宿主成功结果或明确配置失败结果
        """
        try:
            values = {key: self.config.get(key, default) for key, default in (
                ("message_limit", 100), ("member_limit", 50), ("text_budget", 12000), ("member_cache_ttl", 60),
            )}
            if any(type(value) is not int for value in values.values()):
                raise ValueError("群聊查询配置必须是整数")
            from typing import cast
            limits = GroupChatLimits(**cast(dict[str, int], values))
            return await group_chat_service.execute(str(self.tool_name), kwargs, limits=limits)
        except Exception:
            logger.error(f"[group_chat] 工具执行异常, 工具={self.tool_name}: {traceback.format_exc()}")
            return _failure("invalid_configuration", "群聊插件配置或运行状态无效")


def _failure(code: str, message: str) -> dict[str, Any]:
    """
    返回已记录日志的工具错误

    参数:
    - code: 稳定错误码
    - message: 错误说明

    返回:
    - 统一失败结果
    """
    logger.warning(f"[group_chat] 调用失败, 错误={code}, 原因={message}")
    return {"ok": False, "error": {"code": code, "message": message, "retryable": code == "unavailable"}}


def _group_available() -> bool:
    """
    判断本轮是否是可信群来源, 私聊不展示这些工具

    返回:
    - 当前入站身份属于群聊时为 True
    """
    origin = current_call_origin()
    return origin is not None and (origin.conversation_kind == "group" or not origin.conversation_kind and origin.chat_type == "GroupMessage")


class GroupChatTool(_GroupChatMixin, Tool):
    """同步会话工具, 在平台循环上执行当前群宿主"""

    def is_available_for_call(self) -> bool:
        """
        过滤非群聊请求中的工具声明

        返回:
        - 工具定义完整且属于群来源时为 True
        """
        return _group_available()

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """
        跨线程提交带当前上下文的宿主调用, 超时后取消并禁止后台继续准备

        参数:
        - kwargs: 模型参数

        返回:
        - 宿主结果或明确不可用结果
        """
        turn = current_reply_turn()
        origin = current_call_origin()
        manager = current_adapter_manager()
        adapter = manager.get_adapter(origin.adapter_id) if manager and origin else None
        loop = turn.loop if turn else getattr(adapter, "_loop", None)
        if loop is None or not loop.is_running():
            return _failure("unavailable", "来源事件循环不可用")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            return _failure("unavailable", "同步工具不能在平台事件循环内阻塞调用")
        try:
            future = asyncio.run_coroutine_threadsafe(self._run(kwargs), loop)
            try:
                return future.result(timeout=30)
            except TimeoutError:
                future.cancel()
                return _failure("unavailable", "群聊宿主调用超时, 已取消")
        except Exception:
            logger.error(f"[group_chat] 同步桥接失败, 工具={self.tool_name}: {traceback.format_exc()}")
            return _failure("unavailable", "来源宿主暂不可用")


class AsyncGroupChatTool(_GroupChatMixin, AsyncTool):
    """异步会话工具, 直接等待当前群宿主"""

    def is_available_for_call(self) -> bool:
        """
        过滤非群聊请求中的工具声明

        返回:
        - 工具定义完整且属于群来源时为 True
        """
        return _group_available()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """
        异步执行当前群工具

        参数:
        - kwargs: 模型参数

        返回:
        - 宿主结果或明确失败结果
        """
        return await self._run(kwargs)


def get_tools(session: Session | AsyncSession, config: dict[str, Any], resources: object = None) -> list[Tool] | list[AsyncTool]:
    """
    为同步或异步会话构造同一组六个工具

    参数:
    - session: 当前主会话
    - config: 已解析的插件配置
    - resources: 插件资源对象, 首批不使用外部资源

    返回:
    - 与会话执行方式一致的六个工具实例
    """
    result = []
    kind = AsyncGroupChatTool if isinstance(session, AsyncSession) else GroupChatTool
    for name, (description, _, _) in DEFINITIONS.items():
        tool = kind(name, description, {})
        tool.config = dict(config)
        tool.deferred_platform_reply = name == "group_chat_reply"
        tool.recovery_policy = "manual" if name == "group_chat_reply" else "retry"
        result.append(tool)
    return result
