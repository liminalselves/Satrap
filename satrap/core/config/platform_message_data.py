"""平台消息档案的管理契约, 不修改模型上下文或调用平台撤回"""
from __future__ import annotations

from typing import Any, cast
import json

from satrap.core.config.platform_messages import MessageArchiveError, MessageScope, PlatformMessageStore
from satrap.core.config.conversation_catalog import platform_catalog
from satrap.core.platform.catalog import adapter_catalog
from satrap.core.storage.layout import StorageLayout
from satrap.core.log import logger


def _archive_store(layout: StorageLayout, document: dict[str, Any], platform_id: str) -> PlatformMessageStore:
    """
    根据可信平台实例定位存储和最后生效的保留期

    参数:
    - layout: 控制服务配置的存储布局
    - document: 后端配置文档
    - platform_id: 已核验的平台实例 ID

    返回:
    - 不创建冷数据库的档案存储
    """
    database = layout.platform_db(platform_id)
    if database.is_symlink() or layout.platform_root(platform_id).is_symlink():
        raise ValueError("平台档案路径不能是符号链接")
    store = PlatformMessageStore(database, platform_id)
    days = store.saved_retention_days()
    if days is None:
        configured = next((item for item in document.get("platforms", []) if item.get("id") == platform_id), {})
        days = configured.get("settings", {}).get("message_archive_retention_days", 30)
    return PlatformMessageStore(database, platform_id, retention_days=days)


def platform_archive_catalog(layout: StorageLayout, document: dict[str, Any], query: dict[str, str]) -> dict[str, Any]:
    """
    按动态平台目录汇总档案分页, 跨平台查询保留明确的部分失败信息

    参数:
    - layout: 控制服务配置的存储布局
    - document: 后端配置文档
    - query: 平台类型/实例, 账号/对话类型, 名称和分页筛选

    返回:
    - 稳定按平台与档案身份排序的目录和动态筛选项
    """
    allowed = {"platform_id", "platform_type", "conversation_kind", "self_id", "q", "offset", "limit"}
    if set(query) - allowed or any(not isinstance(value, str) or len(value) > 256 for value in query.values()):
        raise ValueError("档案目录参数无效")
    offset, limit = int(query.get("offset", "0")), int(query.get("limit", "40"))
    if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100:
        raise ValueError("档案目录分页参数无效")
    declarations = {item["type"]: item for item in adapter_catalog()}
    descriptors = platform_catalog(layout, document)
    platform_id = query.get("platform_id", "")
    if platform_id:
        descriptors = [item for item in descriptors if item["id"] == platform_id]
        if not descriptors:
            raise MessageArchiveError("not_found", "平台实例不存在")
    if query.get("platform_type"):
        descriptors = [item for item in descriptors if item["type"] == query["platform_type"]]
    items: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    kinds: dict[str, str] = {}
    accounts: set[str] = set()
    total, remaining_offset, remaining_limit = 0, offset, limit
    for descriptor in descriptors:
        try:
            store = _archive_store(layout, document, descriptor["id"])
            data = store.catalog(conversation_kind=query.get("conversation_kind", ""), self_id=query.get("self_id", ""),
                                 keyword=query.get("q", ""), offset=remaining_offset, limit=max(1, remaining_limit))
            labels = declarations.get(descriptor["type"], {}).get("conversation_kinds", {})
            kinds.update({kind: labels.get(kind, kind) for kind in data["conversation_kinds"]})
            accounts.update(data["self_ids"])
            if remaining_limit:
                items.extend({**item, "platform_id": descriptor["id"], "platform_type": descriptor["type"],
                              "type_label": descriptor.get("type_label", descriptor["type"]),
                              "conversation_kind_label": labels.get(item["conversation_kind"], item["conversation_kind"])}
                             for item in data["items"])
                remaining_limit -= len(data["items"])
            remaining_offset = max(0, remaining_offset - data["total"])
            total += data["total"]
        except Exception as exc:
            logger.error(f"[消息档案] 目录读取失败, 平台={descriptor['id']}, 原因={type(exc).__name__}: {exc}")
            if platform_id:
                raise
            warnings.append({"platform_id": descriptor["id"], "error": "archive_unavailable"})
    return {"items": items, "total": total, "warnings": warnings,
            "conversation_kinds": [{"value": key, "label": value} for key, value in sorted(kinds.items())],
            "self_ids": sorted(accounts)}


def platform_archive_operation(layout: StorageLayout, document: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """
    将已认证的管理操作限定到动态目录中的平台实例

    参数:
    - layout: 控制服务配置的存储布局
    - document: 后端配置文档
    - payload: 平台 ID 与档案管理参数, 禁止直接提交数据库路径

    返回:
    - 档案管理结果
    """
    platform_id = payload.get("platform_id")
    if not isinstance(platform_id, str) or platform_id not in {item["id"] for item in platform_catalog(layout, document)}:
        raise MessageArchiveError("not_found", "平台实例不存在")
    service = PlatformMessageDataService(_archive_store(layout, document, platform_id))
    return service.operate({key: value for key, value in payload.items() if key != "platform_id"})


class PlatformMessageDataService:
    """为已授权平台实例提供消息档案管理, 不接受数据库路径或原文编辑"""

    def __init__(self, store: PlatformMessageStore) -> None:
        """
        使用控制服务已解析的平台档案存储

        参数:
        - store: 可信平台实例对应的现有数据库
        """
        self.store = store

    def operate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        校验管理动作并访问唯一档案范围, 存储错误交给请求边界捕获

        参数:
        - payload: 账号, 对话类型, 对话 ID 和动作参数

        返回:
        - 查询结果或删除/恢复回执, 不含任何模型上下文
        """
        try:
            return self._operate(payload)
        except json.JSONDecodeError as exc:
            raise MessageArchiveError("archive_unavailable", "档案恢复数据损坏") from exc

    def _operate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        执行已认证的管理动作, JSON 解码失败由公开边界转换为存储错误

        参数:
        - payload: 已认证请求携带的管理参数

        返回:
        - 查询结果或管理回执
        """
        common = {"self_id", "conversation_kind", "chat_id", "action"}
        options = {"read": {"keyword", "sender_id", "start_time", "end_time", "cursor", "limit", "text_budget"},
                   "message": {"message_id"}, "delete": {"message_ids", "expected_revision"},
                   "clear": {"expected_revision"}, "restore": {"backup_id", "expected_revision"}}
        action = payload.get("action", "read")
        if not isinstance(action, str) or action not in options or set(payload) - common - options[action]:
            raise ValueError("档案操作或参数无效, 原始平台消息不支持编辑")
        identities = [payload.get(key) for key in ("self_id", "conversation_kind", "chat_id")]
        if not all(isinstance(value, str) and value.strip() for value in identities):
            raise ValueError("缺少档案账号, 对话类型或对话 ID")
        scope = MessageScope(self.store.adapter_id, *(cast(str, value) for value in identities))
        state = self.store.management_state(scope)
        if state is None:
            raise MessageArchiveError("not_found", "平台消息档案不存在")
        arguments = {key: value for key, value in payload.items() if key not in common}
        if action == "read":
            result = self.store.query(scope, **arguments)
            if result["revision"] != state["revision"]:
                raise MessageArchiveError("revision_conflict", "档案管理状态发生变化, 请刷新")
            return {**result, **state}
        if action == "message":
            message_id = arguments.get("message_id")
            if not isinstance(message_id, str):
                raise ValueError("查看消息需要有效消息 ID")
            item = self.store.get(scope, message_id)
            if item is None:
                raise MessageArchiveError("not_found", "消息未被本地档案采集")
            return {"ok": True, "item": item, **state}
        if "expected_revision" not in arguments:
            raise ValueError("删除和恢复需要已读取的档案修订")
        if action == "restore":
            backup_id = arguments.get("backup_id")
            if not isinstance(backup_id, str):
                raise ValueError("恢复需要有效备份 ID")
            result = self.store.restore(scope, backup_id, expected_revision=arguments["expected_revision"])
        elif action == "delete":
            if not isinstance(arguments.get("message_ids"), list) or not arguments["message_ids"]:
                raise ValueError("单条或批量删除需要明确的消息 ID")
            result = self.store.delete(scope, message_ids=arguments["message_ids"], expected_revision=arguments["expected_revision"])
        else:
            result = self.store.delete(scope, expected_revision=arguments["expected_revision"])
        return {"ok": True, **result}
