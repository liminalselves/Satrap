"""已认证人工提醒管理, 冷数据库可查看和取消, 创建与恢复需当前后台授权"""
from __future__ import annotations

from typing import Any
import traceback

from satrap.core.config.conversation_catalog import platform_catalog
from satrap.core.config.platform_message_data import _archive_store
from satrap.core.config.platform_messages import MessageScope, MessageArchiveError, PlatformMessageStore
from satrap.core.group_chat.reminders import ReminderStore, ReminderError
from satrap.core.storage.layout import StorageLayout
from satrap.core.log import logger


def reminder_scope(platform_id: str, query: dict[str, str]) -> MessageScope:
    """
    从人工选择的完整对话范围解析目标

    参数:
    - platform_id: 已解码平台注册实例 ID
    - query: 单值身份与可选分页参数

    返回:
    - 完整群身份, 未知参数和非群范围拒绝
    """
    if set(query) - {"self_id", "chat_id", "conversation_kind", "state", "limit", "cursor"}:
        raise ReminderError("invalid_argument", "提醒查询包含未知字段")
    scope = MessageScope(platform_id, query.get("self_id", ""), query.get("conversation_kind", "group"), query.get("chat_id", ""))
    if scope.conversation_kind != "group":
        raise ReminderError("invalid_argument", "当前只支持群内一次性提醒")
    return scope


def reminder_management(layout: StorageLayout, document: dict[str, Any], platform_id: str, query: dict[str, str],
                        action: str, identity: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    已认证入口查看或取消保留任务, 不自动绑定已删除平台的新实例

    参数:
    - layout: 控制服务持有的存储布局
    - document: 当前后端配置
    - platform_id: URL 中的平台注册实例
    - query: 完整对话身份和分页
    - action: list, get 或 cancel
    - identity: 单个提醒 ID
    - payload: 取消时填写 expected_revision

    返回:
    - 有来源状态的提醒数据或实际取消结果
    """
    if platform_id not in {item["id"] for item in platform_catalog(layout, document)}:
        raise ReminderError("not_found", "平台实例不存在")
    scope = reminder_scope(platform_id, query)
    database = layout.platform_db(platform_id)
    if not database.is_file():
        raise ReminderError("not_found", "平台没有保留的提醒数据")
    archive = _archive_store(layout, document, platform_id)
    repository = ReminderStore(database)
    if action == "list":
        result = repository.list(scope, state=query.get("state", ""), limit=int(query.get("limit", "20")), cursor=query.get("cursor", ""))
        records = result["items"]
    elif action == "get":
        result = repository.get(scope, identity)
        records = [result["reminder"]]
    elif action == "cancel":
        values = dict(payload or {})
        if set(values) != {"expected_revision"}:
            raise ReminderError("invalid_argument", "取消需要最近读到的 expected_revision")
        result = repository.change(scope, identity, "cancel", values["expected_revision"])
        records = [result["reminder"]]
    else:
        raise ReminderError("invalid_argument", "此入口仅支持查看和取消提醒")
    annotate_reminder_sources(records, archive, scope)
    return result


def annotate_reminder_sources(records: list[dict[str, Any]], archive: PlatformMessageStore, scope: MessageScope) -> None:
    """
    附加当前来源可用状态, 不保存原始消息副本也不改变提醒期限

    参数:
    - records: 已通过范围授权的提醒记录
    - archive: 同一平台的真实档案
    - scope: 当前完整群身份
    """
    for record in records:
        source = record["source_message_id"]
        record["source_status"] = "operator" if not source else "unavailable"
        if source:
            try:
                message = archive.get(scope, source)
                if message is not None and message.get("status") == "active":
                    record["source_status"] = "available"
            except MessageArchiveError as exc:
                logger.warning(f"[提醒管理] 来源暂不可用, 任务={record['reminder_id']}, 原因={exc.code}")
            except Exception:
                logger.error(f"[提醒管理] 来源状态读取失败, 任务={record['reminder_id']}" + "\n" + traceback.format_exc())
