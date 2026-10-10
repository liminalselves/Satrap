"""通过已认证管理入口管理长期记忆, 不依赖模型插件是否安装"""
from __future__ import annotations

from typing import Any

from satrap.core.config.conversation_catalog import platform_catalog
from satrap.core.config.platform_message_data import _archive_store
from satrap.core.config.platform_messages import MessageScope
from satrap.core.memory.scoped import MemoryError, ScopedMemories
from satrap.core.storage.layout import StorageLayout


def memory_management(layout: StorageLayout, document: dict[str, Any], platform_id: str,
                      query: dict[str, str], action: str, identity: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    将人工操作限定到配置中的真实平台和明确对话范围

    参数:
    - layout: 由控制服务确定的存储布局
    - document: 当前后端配置文档
    - platform_id: URL 中的平台注册实例
    - query: 范围身份与可选筛选
    - action: list, get, create, update, delete, proposals 或 decide
    - identity: 记忆或提案 ID
    - payload: 写请求的内容, 修订和幂等键

    返回:
    - 当前范围内的真实业务结果, 失败交给路由捕获与记录
    """
    if platform_id not in {item["id"] for item in platform_catalog(layout, document)}:
        raise MemoryError("not_found", "平台实例不存在")
    if set(query) - {"self_id", "chat_id", "conversation_kind", "kind", "user_id", "keyword", "limit", "cursor"}:
        raise MemoryError("invalid_argument", "记忆管理含未知参数")
    scope = MessageScope(platform_id, query.get("self_id", ""), query.get("conversation_kind", "group"), query.get("chat_id", ""))
    repository = ScopedMemories(_archive_store(layout, document, platform_id), scope)
    if action == "list":
        return repository.list(kind=query.get("kind", ""), user_id=query.get("user_id", ""), keyword=query.get("keyword", ""),
                               limit=int(query.get("limit", "20")), cursor=query.get("cursor", ""))
    if action == "get":
        return repository.get(identity)
    if action == "proposals":
        return repository.proposals()
    values = dict(payload or {})
    if action == "decide":
        if set(values) != {"approve", "expected_revision"}:
            raise MemoryError("invalid_argument", "审批需要明确决定与基准修订")
        return repository.decide(identity, values["approve"], values["expected_revision"])
    operation_id = values.pop("idempotency_key", "")
    allowed = ({"kind", "key", "title", "content", "owner_user_id", "source_message_ids"} if action == "create"
               else {"title", "content", "source_message_ids", "expected_revision"} if action == "update" else {"expected_revision"})
    if action not in {"create", "update", "delete"} or set(values) - allowed:
        raise MemoryError("invalid_argument", "记忆写请求字段无效")
    if identity:
        values["memory_id"] = identity
    return repository.mutate(action, values, actor="authenticated_operator", operator=True, operation_id=operation_id)
