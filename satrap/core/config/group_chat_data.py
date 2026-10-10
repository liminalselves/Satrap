"""
群摘要与媒体的认证管理入口

从控制服务的平台目录解析数据库, 不接受任意路径;
冷平台可查看已保存摘要, 管理操作不修改模型上下文或发送平台消息
"""
from __future__ import annotations

from typing import Any
from email.parser import BytesParser
from email.policy import default as email_policy
import json

from satrap.core.config.platform_message_data import _archive_store
from satrap.core.config.conversation_catalog import platform_catalog
from satrap.core.config.platform_messages import MessageScope
from satrap.core.group_chat.summaries import SummaryStore
from satrap.core.group_chat.types import GroupChatError
from satrap.core.group_chat.stickers import StickerStore
from satrap.core.storage.layout import StorageLayout

from satrap.core.platform import registry


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


def parse_sticker_upload(content_type: str, body: bytes) -> tuple[dict[str, Any], bytes]:
    """
    解析有界 multipart 上传, 文件名不参与服务器路径计算

    参数:
    - content_type: 认证请求的类型和边界
    - body: 已按 10 MiB 加表单开销限制读取的正文

    返回:
    - 名称, 标签, 集合, 幂等键与图片字节
    """
    if not content_type.lower().startswith("multipart/form-data;") or len(content_type) > 256 or "\r" in content_type or "\n" in content_type:
        raise ValueError("表情上传必须使用 multipart/form-data")
    message = BytesParser(policy=email_policy).parsebytes(b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + body)
    if not message.is_multipart() or message.defects:
        raise ValueError("上传表单格式损坏")
    fields: dict[str, Any] = {}
    file: bytes | None = None
    parts = list(message.iter_parts())
    if len(parts) != 5:
        raise ValueError("上传需要一张图片及名称, 标签, 集合, 幂等键")
    for part in parts:
        name = part.get_param("name", header="content-disposition")
        value = part.get_payload(decode=True)
        if part.is_multipart() or part.defects or not isinstance(value, bytes):
            raise ValueError("上传字段格式无效")
        if name == "file" and file is None:
            file = value
        elif name in {"name", "tags", "collection", "idempotency_key"} and name not in fields and len(value) <= 4096:
            text = value.decode("utf-8", errors="strict")
            fields[name] = json.loads(text) if name == "tags" else text
        else:
            raise ValueError("上传含有未知或重复字段")
    if file is None or set(fields) != {"name", "tags", "collection", "idempotency_key"}:
        raise ValueError("上传缺少必需字段")
    return fields, file


def native_sticker_catalog(layout: StorageLayout, document: dict[str, Any], platform_id: str) -> dict[str, Any]:
    """
    经通用适配器目录读取已确认的原生表情, 无需启动平台

    参数:
    - layout: 控制服务数据目录
    - document: 已保存平台配置
    - platform_id: 用户选中的平台实例

    返回:
    - 适配器协议和有限原生目录
    """
    from satrap.core.platform.catalog import adapter_catalog
    from satrap.core.config.document import validate_platforms

    platform = next((item for item in validate_platforms(document.get("platforms", [])) if item["id"] == platform_id), None)
    if platform is None:
        raise GroupChatError("not_found", "平台实例不存在")
    adapter_catalog()
    adapter = registry.get(platform["type"])
    if adapter is None:
        raise GroupChatError("unsupported", "平台适配器无法加载")
    return {"ok": True, "adapter_type": platform["type"], "items": adapter.group_chat_native_sticker_catalog(_archive_store(layout, document, platform_id))}


def sticker_settings_management(layout: StorageLayout, document: dict[str, Any], platform_id: str,
                                query: dict[str, str], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    在认证管理入口编辑真实平台对话的表情集合

    参数:
    - layout: 控制服务数据目录
    - document: 当前配置
    - platform_id: 真实平台实例
    - query: 完整对话身份
    - payload: 可选的新设置与修订

    返回:
    - 当前群授权
    """
    if platform_id not in {item["id"] for item in platform_catalog(layout, document)}:
        raise GroupChatError("not_found", "平台实例不存在")
    if set(query) != {"self_id", "chat_id", "conversation_kind"}:
        raise ValueError("群表情设置需要完整对话身份")
    scope = MessageScope(platform_id, query["self_id"], query["conversation_kind"], query["chat_id"])
    archive = _archive_store(layout, document, platform_id)
    connection = archive._connect()
    if connection is None:
        raise GroupChatError("not_found", "对话档案不存在")
    try:
        if connection.execute("SELECT 1 FROM platform_message_chats WHERE scope_key=?", (scope.key,)).fetchone() is None:
            raise GroupChatError("not_found", "对话档案不存在")
    finally:
        connection.close()
    return StickerStore(layout).settings(scope, payload)
