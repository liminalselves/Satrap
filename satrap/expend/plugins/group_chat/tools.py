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
from satrap.core.group_chat.reminder_service import execute_reminder_tool, REMINDER_ARGUMENTS
from satrap.core.group_chat.reply import current_reply_turn
from satrap.core.group_chat.types import GroupChatLimits
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.call_context import current_call_origin
from satrap.core.platform import current_adapter_manager

from satrap.core.log import logger
from satrap.core.config.model_tool_authorization import config_ids, bind_tool_session, model_tool_authorization
from satrap.core.config.group_action_origin import bind_model_action_authorization
from satrap.core.group_chat.types import GroupChatError


def _string(description: str) -> dict[str, object]:
    """
    创建长度有限的字符串参数声明

    参数:
    - description: 参数用途

    返回:
    - 字符串 JSON schema
    """
    return {"type": "string", "description": description, "minLength": 1, "maxLength": 256}


_MESSAGE_ID = _string("当前群的一条消息 ID, 从聊天上下文或消息查询结果中取得")
_MEMBER_ID = _string("当前群的成员 ID, 从成员查询或已确认的群消息中取得")
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "description": "最多返回多少条消息, 不填默认 20; 实际数量不超过插件配置的上限"}
_CURSOR = _string("继续查看上一页之后的结果时, 填写上次返回的 next_cursor; 沿用相同查询条件")
_CURSOR["maxLength"] = 4096
_COMPONENTS = {
    "type": "array", "minItems": 1, "maxItems": 64,
    "description": "按顺序填写: text 写文字, quote 引用消息, mention @ 成员, image 发送查询得到的图片, sticker 发送表情目录中的表情; 最多引用一条消息",
    "items": {"oneOf": [
        {"type": "object", "properties": {"type": {"const": "text"}, "text": {"type": "string", "description": "要发到群里的回复文字", "minLength": 1, "maxLength": 100000}}, "required": ["type", "text"], "additionalProperties": False},
        {"type": "object", "properties": {"type": {"const": "quote"}, "message_id": {**_MESSAGE_ID, "description": "要引用的原消息 ID, 例如正在回应的那条发言"}}, "required": ["type", "message_id"], "additionalProperties": False},
        {"type": "object", "properties": {"type": {"const": "mention"}, "source_message_id": {**_MESSAGE_ID, "description": "要 @ 的人所发消息的 ID; 工具会 @ 该消息的发送者, 而不是消息里被 @ 的人"}}, "required": ["type", "source_message_id"], "additionalProperties": False},
        {"type": "object", "properties": {"type": {"const": "mention"}, "user_id": {**_MEMBER_ID, "description": "要 @ 的成员 ID; 已确认对方身份时填写, 与 source_message_id 二选一"}}, "required": ["type", "user_id"], "additionalProperties": False},
        {"type": "object", "properties": {"type": {"const": "image"}, "asset_id": _string("消息取图工具或可信产物工具返回的 asset_id; 不能填写 URL 或文件路径")}, "required": ["type", "asset_id"], "additionalProperties": False},
        {"type": "object", "properties": {"type": {"const": "sticker"}, "sticker_id": _string("表情查询结果的 sticker_id, 不要猜测平台编号")}, "required": ["type", "sticker_id"], "additionalProperties": False},
    ]},
}

DEFINITIONS: dict[str, tuple[str, dict[str, object], list[str]]] = {
    "group_chat_list_groups": ("在获授权的管理者私聊中查看机器人加入的群, 返回群号, 群名及人数; 结果不完整时不能当成全部群", {}, []),
    "group_chat_get_group_info": ("查看当前群的群名, 群号及人数, 不读取其它群", {}, []),
    "group_chat_list_members": ("分页查看当前群成员的 ID, 昵称, 群昵称及角色; has_more=true 时可用 next_cursor 继续读取", {"limit": {**_LIMIT, "maximum": 50, "description": "最多返回多少位成员, 不填默认 10; 实际数量不超过插件配置的上限"}, "cursor": _CURSOR}, []),
    "group_chat_set_group_nickname": ("只修改机器人自己在当前群的群昵称, 不能修改其他成员或账号全局昵称; 空字符串清空群昵称. pending 表示待审批, succeeded 才是已执行, 失败或结果未知时不要重复执行", {"nickname": {"type": "string", "maxLength": 60, "description": "机器人自己的新群昵称, 空字符串表示清空"}}, ["nickname"]),
    "group_chat_reply": ("给当前群回复一条消息, 可组合文字, 引用, 多个 @, 图片和表情; 回复在本轮成功结束后发送. 返回 prepared 表示待发送, 此后不要再次调用本工具或重复提交正文", {"components": _COMPONENTS}, ["components"]),
    "group_chat_get_message_assets": ("取出当前群某条消息中的图片, 返回可发送的 asset_id; 图片已经失效, 撤回或无法下载时会注明原因. 只按需读取这条消息, 不读取其它群", {"message_id": _MESSAGE_ID}, ["message_id"]),
    "group_chat_list_stickers": ("查看当前群已启用的表情, 返回名称, 标签和 sticker_id; 选择符合语境的表情后放进回复组件. 未在该群启用的表情不会出现在结果中", {"keyword": _string("想找的表情名称或标签, 不填则查看可用目录"), "limit": _LIMIT, "cursor": _CURSOR}, []),
    "group_chat_find_members": ("根据昵称或群名片查找当前群的成员, 返回成员 ID, 昵称和名片. 找到多个同名成员时, 先确认目标再操作", {"query": _string("要查找的昵称或群名片, 可以填写其中一部分"), "limit": {**_LIMIT, "maximum": 50, "description": "最多返回多少位成员, 不填默认 10; 实际数量不超过插件配置的上限"}, "cursor": _CURSOR}, ["query"]),
    "group_chat_get_member": ("查看当前群某位成员的 ID, 昵称, 群昵称和角色; 需要确认成员 ID 对应谁时使用. 角色只表示平台资料, 不代表对方有权让机器人执行管理操作", {"user_id": _MEMBER_ID}, ["user_id"]),
    "group_chat_get_message": ("根据消息 ID 查看当前群的一条消息, 返回原文和发送者; 需要确认某句话是谁说的, 或查看引用消息时使用. 本地没有记录时会尝试向平台查询, 已删除的记录不会重新取回", {"message_id": _MESSAGE_ID}, ["message_id"]),
    "group_chat_recent_messages": ("查看当前群最近保存的聊天记录, 返回消息 ID, 发送者, 时间和正文; 需要了解大家刚才在聊什么, 或补充当前上下文时使用. 结果只涵盖机器人已保存的消息", {"limit": _LIMIT, "before_message_id": {**_MESSAGE_ID, "description": "只查看这条消息之前的记录; 不填则从最新消息开始"}, "cursor": _CURSOR}, []),
    "group_chat_search_messages": ("搜索当前群保存的聊天记录, 可按关键词, 发送者和时间筛选; 用户提到之前的讨论, 或需要查找某人的发言时使用. 同时填写多个条件时, 返回符合全部条件的消息", {
        "keyword": _string("要查找的文字, 例如昨天讨论过的项目名; 按普通文本匹配, 不是正则表达式"),
        "sender_id": {**_MEMBER_ID, "description": "只查这位成员发送的消息, 填写成员 ID; 不填则查询所有人的消息"},
        "start_time": _string("只查这个时间及之后的消息, 填写日期和时间, 例如 2026-10-04T09:00:00"),
        "end_time": _string("只查这个时间及之前的消息, 填写日期和时间, 例如 2026-10-04T18:00:00"),
        "limit": _LIMIT, "cursor": _CURSOR,
    }, []),
    "group_chat_prepare_summary": ("读取当前群指定时段的讨论, 供你生成摘要; 结果会注明保存范围和省略情况, 不能把不完整记录说成全部讨论", {
        "start_time": _string("讨论的开始日期和时间, 例如 2026-10-04T09:00:00; 自动使用后端本地时区"),
        "end_time": _string("讨论的结束日期和时间, 例如 2026-10-04T18:00:00; 自动使用后端本地时区"),
        "keyword": _string("只总结包含这段文字的消息; 不填则读取该时段的所有已保存讨论"),
        "include_bot": {"type": "boolean", "description": "是否包含机器人自己的发言, 默认不包含"},
    }, ["start_time", "end_time"]),
    "group_chat_read_summary_sources": ("继续读取本次摘要的消息来源; 沿用准备摘要时返回的 snapshot_id 和上一页 next_cursor, 读完后再保存摘要", {
        "snapshot_id": _string("本轮 prepare_summary 返回的快照 ID"), "cursor": _CURSOR,
    }, ["snapshot_id", "cursor"]),
    "group_chat_save_summary": ("保存你根据本次消息快照写出的摘要; 每一条结论都要列出来源消息 ID. 返回 saved 只表示保存成功, 可再用回复工具发到群里", {
        "snapshot_id": _string("本轮已经读完全部分页的摘要快照 ID"),
        "title": {**_string("概括这段讨论的标题"), "maxLength": 120},
        "points": {"type": "array", "minItems": 1, "maxItems": 20, "description": "逐条填写讨论结论, 区分提议, 决定和分歧, 总正文不超过 12000 字符",
                   "items": {"type": "object", "properties": {
                       "text": {"type": "string", "minLength": 1, "maxLength": 2000, "description": "根据已读来源写出的摘要条目"},
                       "source_message_ids": {"type": "array", "minItems": 1, "maxItems": 10, "items": _MESSAGE_ID,
                                              "description": "支持本条结论的来源消息 ID, 必须从本次快照中取得"},
                   }, "required": ["text", "source_message_ids"], "additionalProperties": False}},
    }, ["snapshot_id", "title", "points"]),
    "group_chat_get_summary": ("查看当前群一份已保存的摘要及出处; 来源已删除或过期时会注明失效, 不将旧摘要继续当成当前依据", {
        "summary_id": _string("摘要保存或列表工具返回的摘要 ID"),
    }, ["summary_id"]),
    "group_chat_list_summaries": ("查找当前群已保存的摘要, 返回摘要 ID, 时间范围和有效状态", {
        "keyword": _string("要查找的标题或摘要文字"), "limit": _LIMIT, "cursor": _CURSOR,
    }, []),
    "group_chat_create_reminder": ("在当前群创建一次性提醒, 可填具体日期时间或等待秒数; 返回 created 表示已安排, 到期是否送达要查看任务记录. 只在用户明确要求提醒时创建", {
        "text": {"type": "string", "minLength": 1, "maxLength": 2000, "description": "到期直接发送的提醒正文, 到时不会再调用模型生成内容"},
        "due_at": {**_string("提醒的日期和时间, 例如 2026-10-05T09:00:00; 自动使用后端本地时区, 与 after_seconds 二选一"), "maxLength": 64},
        "after_seconds": {"type": "integer", "minimum": 10, "maximum": 31536000, "description": "从后端接受请求起等待多少秒, 例如半小时填 1800; 与 due_at 二选一"},
        "mention_user_ids": {"type": "array", "maxItems": 10, "uniqueItems": True, "items": _MEMBER_ID,
                             "description": "到期需要 @ 的已确认成员 ID, 不填则只发文字, 不能 @ 全体"},
    }, ["text"]),
    "group_chat_list_reminders": ("查看你在当前群创建的提醒, 返回执行时间, 状态和提醒 ID; 不会显示其他成员的提醒正文", {
        "state": _string("只看这个状态: scheduled 待执行, paused 已暂停, sent 已发送, unknown 无法确认是否送达; 不填则查看全部本人任务"),
        "limit": {**_LIMIT, "maximum": 50}, "cursor": _CURSOR,
    }, []),
    "group_chat_get_reminder": ("查看你的一条提醒的时间, 状态, revision 和实际发送结果; created 不代表已发送, unknown 时不要自动重新创建以免重复", {
        "reminder_id": _string("创建或列表结果返回的提醒 ID"),
    }, ["reminder_id"]),
    "group_chat_cancel_reminder": ("取消你创建且还没开始发送的提醒; 先查询取得最新 revision. 已经开始发送时可能无法撤回, 工具会明确返回当前状态", {
        "reminder_id": _string("要取消的本人提醒 ID"),
        "expected_revision": {"type": "integer", "minimum": 1, "description": "最近一次查询这条提醒返回的 revision, 不要猜测"},
    }, ["reminder_id", "expected_revision"]),
}


class _GroupChatMixin:
    """在工具边界处理配置与来源, 不把读取失败包装为空成功"""

    tool_name: str | None
    config: Mapping[str, object]
    session: Session | AsyncSession
    deferred_platform_reply = False

    def _authorize(self, group_id: str) -> None:
        """
        按当前工具配置复核查询或机器人自身修改权限

        参数:
        - group_id: 宿主固定的对话 ID
        """
        origin = current_call_origin()
        if origin is None or not getattr(self, "is_enabled")():
            raise GroupChatError("stale_call", "来源工具或轮次已停用")
        if self.tool_name == "group_chat_list_groups":
            if not _private_available() or origin.actor_id not in config_ids(self.config.get("cross_group_query_callers")):
                raise GroupChatError("forbidden", "仅配置的管理者可在私聊查询群列表")
            return
        allowed = config_ids(self.config.get("allowed_groups"))
        if not _group_available() or group_id != origin.chat_id or allowed and group_id not in allowed:
            raise GroupChatError("forbidden", "工具只能操作获授权的当前群")
        if self.tool_name == "group_chat_set_group_nickname":
            callers = config_ids(self.config.get("nickname_allowed_callers"))
            if self.config.get("self_nickname_enabled") is not True or callers and origin.actor_id not in callers:
                raise GroupChatError("forbidden", "机器人自身群昵称修改未获授权")
        if self.tool_name in REMINDER_ARGUMENTS:
            workflow = getattr(self.session, "_wf", None)
            manager = getattr(workflow, "tools_manager", None)
            getter = getattr(self.session, "list_plugins", None)
            plugins = getter() if callable(getter) else []
            if not isinstance(plugins, list):
                raise GroupChatError("stale_call", "来源 Agent 插件状态无效")
            plugin = next((item for item in plugins if item.name == "group_chat"), None)
            if (manager is None or manager.tools.get(self.tool_name) is not self or not manager.is_tool_enabled(self.tool_name)
                    or plugin is None or not plugin.enabled or not plugin.tools.get(self.tool_name, False)):
                raise GroupChatError("stale_call", "提醒工具已从来源 Agent 移除或停用")
            if self.tool_name == "group_chat_create_reminder" and self.config.get("reminders_enabled") is not True:
                raise GroupChatError("write_disabled", "请先在群聊插件配置中开启提醒")

    def get_tool_defined(self) -> dict[str, Any]:
        """
        返回完整组件和可选参数 schema

        返回:
        - 当前群聊工具的声明之一
        """
        name = str(self.tool_name)
        description, properties, required = DEFINITIONS[name]
        properties = copy.deepcopy(properties)
        if name == "group_chat_reply":
            origin = current_call_origin()
            manager = current_adapter_manager()
            adapter = manager.get_adapter(origin.adapter_id) if manager and origin else None
            if adapter is not None:
                capabilities = adapter.group_chat_capabilities()
                media = self.config.get("media_reply_enabled", True) is True
                schema: Any = properties["components"]
                schema["items"]["oneOf"] = [variant for variant in schema["items"]["oneOf"]
                    if capabilities.get(variant["properties"]["type"]["const"], {}).get("state") == "supported"
                    or variant["properties"]["type"]["const"] == "sticker" and media and capabilities.get("image", {}).get("state") == "supported"]
                if not media:
                    schema["items"]["oneOf"] = [variant for variant in schema["items"]["oneOf"] if variant["properties"]["type"]["const"] not in {"image", "sticker"}]
        parameters: dict[str, Any] = {"type": "object", "properties": copy.deepcopy(properties), "required": list(required), "additionalProperties": False}
        if name == "group_chat_create_reminder":
            parameters["oneOf"] = [{"required": ["due_at"], "not": {"required": ["after_seconds"]}},
                                   {"required": ["after_seconds"], "not": {"required": ["due_at"]}}]
        return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}

    async def _run(self, kwargs: Mapping[str, object]) -> dict[str, Any]:
        """
        使用安装配置提供的查询预算执行宿主操作

        参数:
        - kwargs: 模型参数

        返回:
        - 宿主成功结果或明确配置失败结果
        """
        try:
            if self.tool_name in REMINDER_ARGUMENTS:
                return await execute_reminder_tool(str(self.tool_name), kwargs, session=self.session, config=self.config, authorize=self._authorize)
            values = {key: self.config.get(key, default) for key, default in (
                ("message_limit", 100), ("member_limit", 50), ("text_budget", 12000), ("member_cache_ttl", 60),
            )}
            if any(type(value) is not int for value in values.values()):
                raise ValueError("群聊查询配置必须是整数")
            from typing import cast
            limits = GroupChatLimits(message_limit=cast(int, values["message_limit"]), member_limit=cast(int, values["member_limit"]),
                                     text_budget=cast(int, values["text_budget"]), member_cache_ttl=cast(int, values["member_cache_ttl"]))
            enabled = self.config.get("summary_enabled", True)
            if type(enabled) is not bool:
                raise ValueError("摘要开关必须是布尔值")
            from dataclasses import replace

            input_budget = 24000
            if self.tool_name == "group_chat_prepare_summary":
                workflow = getattr(self.session, "_wf", None)
                context = getattr(workflow, "ctx", None)
                usage = context.get_context_usage(method="experience") if context is not None else None
                if usage is not None:
                    assert workflow is not None
                    definitions = workflow.tools_manager.get_tools_definitions()
                    import json

                    overhead = len(json.dumps(definitions, ensure_ascii=False).encode("utf-8")) + 8192
                    available = usage.history_upper_tokens - usage.history_tokens - overhead
                    input_budget = max(0, min(1000000, available // 2))
                    # 来源按 UTF-8 字节保守限额, 预留工具定义, 摘要正文与工具结果空间
            limits = replace(limits, summary_enabled=enabled, summary_input_budget=input_budget,
                             allowed_groups=tuple(config_ids(self.config.get("allowed_groups"))),
                             cross_group_query_callers=tuple(config_ids(self.config.get("cross_group_query_callers"))),
                             media_reply_enabled=cast(bool, self.config.get("media_reply_enabled", True)),
                             max_reply_images=cast(int, self.config.get("max_reply_images", 4)),
                             max_reply_stickers=cast(int, self.config.get("max_reply_stickers", 4)),
                             summary_message_limit=cast(int, self.config.get("summary_message_limit", 500)),
                             summary_text_budget=cast(int, self.config.get("summary_text_budget", 60000)),
                             summary_retention_days=cast(int, self.config.get("summary_retention_days", 30)))
            def authorize(group_id: str) -> None:
                """
                等待后拒绝使用旧群范围返回查询结果

                参数:
                - group_id: 宿主固定的对话身份
                """
                self._authorize(group_id)
                if tuple(config_ids(self.config.get("allowed_groups"))) != limits.allowed_groups:
                    raise GroupChatError("stale_call", "查询期间插件允许群范围已经变化")
            if self.tool_name == "group_chat_set_group_nickname":
                origin = current_call_origin()
                if origin is None:
                    raise PermissionError("工具缺少当前来源")
                adapter_manager = current_adapter_manager()
                adapter = adapter_manager.get_adapter(origin.adapter_id) if adapter_manager else None
                def permission(live: Any, target_group: str) -> None:
                    """
                    固定账号和平台实例后复核机器人自身修改权限

                    参数:
                    - live: 当前仍有效的工具
                    - target_group: 固定目标群
                    """
                    manager = current_adapter_manager()
                    if (manager is None or manager.get_adapter(origin.adapter_id) is not adapter or adapter is None
                            or adapter.group_chat_self_id() != origin.self_id):
                        raise PermissionError("机器人账号或平台实例已变化")
                    try:
                        live._authorize(target_group)
                    except GroupChatError as exc:
                        raise PermissionError(str(exc)) from exc
                source = model_tool_authorization(self, origin, "group_chat", permission)
                with bind_model_action_authorization(source):
                    return await group_chat_service.execute(str(self.tool_name), kwargs, limits=limits, authorize=authorize)
            return await group_chat_service.execute(str(self.tool_name), kwargs, limits=limits, authorize=authorize)
        except GroupChatError as exc:
            return _failure(exc.code, str(exc))
        except PermissionError:
            return _failure("forbidden", "当前工具无法取得有效的写操作授权")
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


def _private_available() -> bool:
    """
    判断宿主来源是否为私聊

    返回:
    - 当前来源属于私聊时为 True
    """
    origin = current_call_origin()
    return origin is not None and (origin.conversation_kind == "private" or not origin.conversation_kind and origin.chat_type == "FriendMessage")


def _available(tool: _GroupChatMixin) -> bool:
    """
    按本轮来源, 插件开关和适配器能力过滤定义

    参数:
    - tool: 当前工具实例

    返回:
    - 当前 Agent 可调用时为 True
    """
    name = str(tool.tool_name)
    origin = current_call_origin()
    manager = current_adapter_manager()
    adapter = manager.get_adapter(origin.adapter_id) if manager and origin else None
    capabilities = adapter.group_chat_capabilities() if adapter else {}
    if name == "group_chat_list_groups":
        return (_private_available() and origin is not None
                and origin.actor_id in config_ids(tool.config.get("cross_group_query_callers"))
                and capabilities.get("group_list", {}).get("state") == "supported")
    if not _group_available():
        return False
    allowed = config_ids(tool.config.get("allowed_groups"))
    if allowed and (origin is None or origin.chat_id not in allowed):
        return False
    capability = {"group_chat_get_group_info": "group_info", "group_chat_list_members": "member_list"}.get(name)
    if capability is not None:
        return capabilities.get(capability, {}).get("state") == "supported"
    if name == "group_chat_set_group_nickname":
        callers = config_ids(tool.config.get("nickname_allowed_callers"))
        return (tool.config.get("self_nickname_enabled") is True and origin is not None
                and (not callers or origin.actor_id in callers)
                and capabilities.get("self_nickname", {}).get("state") == "supported")
    if "summary" in name and tool.config.get("summary_enabled", True) is not True:
        return False
    if name == "group_chat_create_reminder":
        return tool.config.get("reminders_enabled") is True and adapter is not None and adapter.supports_scheduled_group_send
    if name == "group_chat_reply":
        if adapter is not None:
            kinds = {"text", "quote", "mention"}
            if tool.config.get("media_reply_enabled", True) is True:
                kinds.update({"image", "sticker"})
            return any(capabilities.get(kind, {}).get("state") == "supported" for kind in kinds)
    if name in {"group_chat_get_message_assets", "group_chat_list_stickers"}:
        if tool.config.get("media_reply_enabled", True) is not True:
            return False
        return capabilities.get("image", {}).get("state") == "supported" or name == "group_chat_list_stickers" and capabilities.get("sticker", {}).get("state") == "supported"
    return True


class GroupChatTool(_GroupChatMixin, Tool):
    """同步会话工具, 在平台循环上执行当前群宿主"""

    def is_available_for_call(self) -> bool:
        """
        过滤非群聊请求中的工具声明

        返回:
        - 工具定义完整且属于群来源时为 True
        """
        return _available(self)

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
        return _available(self)

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
    为同步或异步会话构造同一组群聊工具

    参数:
    - session: 当前主会话
    - config: 已解析的插件配置
    - resources: 插件资源对象, 首批不使用外部资源

    返回:
    - 与会话执行方式一致的群聊工具实例
    """
    result = []
    kind = AsyncGroupChatTool if isinstance(session, AsyncSession) else GroupChatTool
    for name, (description, _, _) in DEFINITIONS.items():
        tool = kind(name, description, {})
        tool.config = dict(config)
        tool.session = session
        bind_tool_session(tool, session)
        tool.deferred_platform_reply = name == "group_chat_reply"
        tool.recovery_policy = "manual" if name in {"group_chat_reply", "group_chat_set_group_nickname", "group_chat_create_reminder"} else "retry"
        result.append(tool)
    return result
