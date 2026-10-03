"""
群摘要与媒体的认证管理入口

从控制服务的平台目录解析数据库, 不接受任意路径;
冷平台可查看已保存摘要, 管理操作不修改模型上下文或发送平台消息
"""
from __future__ import annotations

from typing import Any

from satrap.core.config.platform_message_data import _archive_store
from satrap.core.config.conversation_catalog import platform_catalog
from satrap.core.config.platform_messages import MessageScope
from satrap.core.group_chat.summaries import SummaryStore
from satrap.core.group_chat.types import GroupChatError
from satrap.core.storage.layout import StorageLayout


def summary_management(layout: StorageLayout, document: dict[str, Any], platform_id: str,
                       query: dict[str, str], action: str, summary_id: str = "",
                       payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    将认证请求限定到一个真实平台对话

    参数:
    - layout: 控制服务的存储布局
    - document: 当前后端配置文档
    - platform_id: 路径解析的平台实例
    - query: self_id, chat_id, conversation_kind 与列表筛选
    - action: list, get 或 delete
    - summary_id: 查看/删除时的 ID
    - payload: 删除的修订与幂等键

    返回:
    - 摘要管理结果, 失败由控制路由记录和归一
    """
    if platform_id not in {item["id"] for item in platform_catalog(layout, document)}:
        raise GroupChatError("not_found", "平台实例不存在")
    if set(query) - {"self_id", "chat_id", "conversation_kind", "keyword", "limit", "cursor"}:
        raise ValueError("摘要管理含有未知参数")
    scope = MessageScope(platform_id, query.get("self_id", ""), query.get("conversation_kind", "group"), query.get("chat_id", ""))
    store = SummaryStore(_archive_store(layout, document, platform_id))
    if action == "list":
        return store.list(scope, keyword=query.get("keyword", ""), limit=int(query.get("limit", "20")), cursor=query.get("cursor"))
    if not summary_id or len(summary_id) > 256:
        raise ValueError("摘要 ID 无效")
    if action == "get":
        return store.get(scope, summary_id)
    values = payload or {}
    if action != "delete" or set(values) != {"expected_revision", "idempotency_key"}:
        raise ValueError("摘要删除需要修订和幂等键")
    return store.delete(scope, summary_id, values["expected_revision"], values["idempotency_key"])
