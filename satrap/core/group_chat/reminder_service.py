"""当前可信发言者的提醒工具业务, 模型不能填写账号或目标群"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
import asyncio
import hashlib
import json
import traceback

from satrap.core.call_context import current_call_origin, current_tool_workflow
from satrap.core.group_chat.reminders import ReminderStore, ReminderError
from satrap.core.group_chat.service import group_chat_service
from satrap.core.group_chat.types import GroupChatError
from satrap.core.group_chat.reminder_management import annotate_reminder_sources
from satrap.core.platform import current_adapter_manager
from satrap.core.log import logger


REMINDER_ARGUMENTS = {
    "group_chat_create_reminder": {"text", "due_at", "after_seconds", "mention_user_ids"},
    "group_chat_list_reminders": {"state", "limit", "cursor"},
    "group_chat_get_reminder": {"reminder_id"},
    "group_chat_cancel_reminder": {"reminder_id", "expected_revision"},
}


async def execute_reminder_tool(
    operation: str, arguments: Mapping[str, object], *, session: Any,
    config: Mapping[str, object], authorize: Callable[[str], None],
) -> dict[str, Any]:
    """
    在当前群执行本人提醒操作, 等待和数据库写入前再次复核来源

    参数:
    - operation: 四个已注册提醒工具之一
    - arguments: 不含范围和所有者的模型参数
    - session: 当前来源主会话
    - config: 当前工具配置
    - authorize: 当前工具仍存在且可调用的权限复核

    返回:
    - 真实提醒结果或稳定错误, 创建成功不表示已经送达
    """
    try:
        if operation not in REMINDER_ARGUMENTS or not isinstance(arguments, Mapping) or set(arguments) - REMINDER_ARGUMENTS[operation]:
            raise ReminderError("invalid_argument", "提醒工具包含未知参数, 只能管理当前群的本人任务")
        context = await group_chat_service._resolve(authorize=authorize)
        manager = current_adapter_manager()
        host = manager.reminder_host if manager else None
        if context.store is None:
            raise ReminderError("unavailable", "当前平台没有提醒存储")
        writing = operation in {"group_chat_create_reminder", "group_chat_cancel_reminder"}
        if writing and (current_tool_workflow() is None or current_tool_workflow() is not getattr(session, "_wf", None)):
            raise ReminderError("read_only_workflow", "只有当前主工作流可以创建或取消提醒")
        captured_config = dict(config)
        connection = context.adapter.group_chat_connection_token()
        repository = await asyncio.to_thread(ReminderStore, context.store.database)

        def check() -> None:
            """在写事务取得执行权后拒绝已结束的轮次或撤销的工具"""
            live_manager = current_adapter_manager()
            origin = current_call_origin()
            if (origin is not context.origin or live_manager is not manager or live_manager is None
                    or live_manager.get_adapter(context.scope.adapter_id) is not context.adapter
                    or context.adapter.group_chat_connection_token() != connection
                    or context.adapter.group_chat_self_id() != context.scope.self_id
                    or not context.adapter.config.enable or not context.adapter.group_chat_group_visible(context.scope.chat_id)
                    or dict(config) != captured_config):
                raise ReminderError("stale_call", "提醒调用来源或配置已经变化")
            authorize(context.scope.chat_id)
            if context.adapter.agent_route_store is not None and context.origin.conversation_kind:
                if context.adapter.agent_route_store.current_revision(context.scope.self_id, "group", context.scope.chat_id) != context.origin.agent_route_generation:
                    raise ReminderError("stale_call", "提醒来源 Agent 路由已经变化")
            group_route = getattr(context.adapter, "group_route", None)
            if callable(group_route) and context.origin.conversation_kind:
                route = group_route(context.scope.chat_id)
                if not isinstance(route, tuple) or len(route) != 2 or route[1] != context.origin.group_route_generation:
                    raise ReminderError("stale_call", "提醒来源群配置已经变化")

        await group_chat_service._revalidate(context)
        check()
        actor = context.origin.actor_id
        if operation == "group_chat_list_reminders":
            state, cursor, limit = arguments.get("state", ""), arguments.get("cursor", ""), arguments.get("limit", 20)
            if not isinstance(state, str) or not isinstance(cursor, str) or type(limit) is not int:
                raise ReminderError("invalid_argument", "提醒状态和游标需要是文本, 条数需要是整数")
            result = await asyncio.to_thread(repository.list, context.scope, actor=actor, state=state, cursor=cursor, limit=limit)
        elif operation == "group_chat_get_reminder":
            identity = arguments.get("reminder_id")
            if not isinstance(identity, str) or not identity or len(identity) > 256:
                raise ReminderError("invalid_argument", "请填写查询结果中的提醒 ID")
            result = await asyncio.to_thread(repository.get, context.scope, identity, actor=actor)
        elif operation == "group_chat_cancel_reminder":
            identity = arguments.get("reminder_id")
            revision = arguments.get("expected_revision")
            if not isinstance(identity, str) or not identity or len(identity) > 256 or type(revision) is not int:
                raise ReminderError("invalid_argument", "取消提醒需要提醒 ID 和最近查询到的 revision")
            result = await asyncio.to_thread(repository.change, context.scope, identity, "cancel", revision, actor=actor, authorize=check)
        else:
            text = arguments.get("text")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                raise ReminderError("invalid_argument", "提醒正文需要 1 至 2000 字")
            if host is None or not context.adapter.supports_scheduled_group_send:
                raise ReminderError("unsupported", "当前平台没有可用的后台提醒发送能力")
            if config.get("reminders_enabled") is not True:
                raise ReminderError("write_disabled", "请先在群聊插件配置中开启提醒")
            source_message = await asyncio.to_thread(context.store.get, context.scope, context.origin.source_message_id)
            if (source_message is None or source_message.get("state", "active") != "active"
                    or source_message.get("direction") != "inbound" or source_message.get("sender_id") != actor
                    or source_message.get("verified") is not True):
                raise ReminderError("invalid_source", "创建提醒需要当前成员本轮的真实来源消息")
            mentions = arguments.get("mention_user_ids", [])
            if (not isinstance(mentions, list) or len(mentions) > 10
                    or any(not isinstance(item, str) or not item or item == "all" for item in mentions) or len(set(mentions)) != len(mentions)):
                raise ReminderError("invalid_argument", "提及目标需要最多 10 个不同的成员 ID, 不能 @ 全体")
            for identity in dict.fromkeys([actor, *mentions]):
                verified = await context.adapter.group_chat_member(context.scope, identity)
                if verified.scope != context.scope or verified.member.user_id != identity:
                    raise ReminderError("invalid_member", "提醒创建者或提及对象不属于当前群")
            origin = context.origin
            runtime = host.backend.get_platform_runtime(context.scope.adapter_id)
            source_cfg = runtime[0].store.get(session.session_id) if runtime else None
            source = {"session_id": session.session_id, "config_name": (source_cfg.session_type_name or "") if source_cfg else "",
                      "route_generation": origin.agent_route_generation, "instance_id": context.adapter.config.instance_id}
            metadata = {"scope": {"adapter_id": context.scope.adapter_id, "self_id": context.scope.self_id,
                                  "conversation_kind": "group", "chat_id": context.scope.chat_id}, "source_agent": source,
                        "creator_kind": "model", "creator_user_id": actor, "mention_user_ids": mentions}
            policy = await asyncio.to_thread(host.policy, metadata)
            if policy.state != "ready":
                raise ReminderError("permission_changed", "当前配置不允许创建提醒: " + policy.reason)

            def create_check() -> None:
                """取得写事务后再检查来源和当前后台发送授权"""
                check()
                latest = host.policy(metadata)
                if latest.state != "ready" or latest.revision != policy.revision:
                    raise ReminderError("stale_call", "创建提醒前配置或权限已经变化")

            await group_chat_service._revalidate(context)
            operation_key = origin.request_id + ":reminder:" + hashlib.sha256(json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            values = policy.config or {}
            result = await asyncio.to_thread(repository.create, context.scope, actor=actor, text=text, mentions=mentions,
                                             source_message_id=origin.source_message_id, operation_id=operation_key,
                                             time_spec={key: arguments[key] for key in ("due_at", "after_seconds") if key in arguments},
                                             member_limit=values.get("active_reminders_per_member", 20), group_limit=values.get("active_reminders_per_group", 200),
                                             source_agent=source, authorize=create_check)
        await asyncio.to_thread(annotate_reminder_sources, result.get("items", [result["reminder"]] if "reminder" in result else []), context.store, context.scope)
        await group_chat_service._revalidate(context)
        check()
        return result
    except (ReminderError, GroupChatError, ValueError) as exc:
        code = getattr(exc, "code", "invalid_argument")
        logger.warning(f"[提醒工具] 操作拒绝, 工具={operation}, 错误={code}: {exc}")
        return {"ok": False, "error": {"code": code, "message": str(exc), "retryable": getattr(exc, "retryable", False)}}
    except Exception:
        logger.error(f"[提醒工具] 执行失败, 工具={operation}" + "\n" + traceback.format_exc())
        return {"ok": False, "error": {"code": "unavailable", "message": "提醒暂不可用", "retryable": True}}
