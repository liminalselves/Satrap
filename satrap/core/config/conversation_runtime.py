"""
对话数据与活动上下文协调

数据库编辑与内存替换在运行实例的操作锁内完成,
同步调用由有界工作线程持有线程锁, 请求取消时等待写入结束再释放实例
"""
from __future__ import annotations

from typing import Any
import copy

from satrap.core.config.conversation_data import ConversationDataService, ConversationDataConflict
from satrap.core.utils.async_worker import RAG_WORKERS
from satrap.core.utils.context import ContextManager, AsyncContextManager


def perform_data_operation(database: str, conversation: str, layer: str, payload: dict[str, Any], context: ContextManager | AsyncContextManager | None = None) -> dict[str, Any]:
    """
    在调用方持有实例锁时编辑数据库并同步内存

    参数:
    - database: 数据库路径
    - conversation: 对话 ID
    - layer: 编辑数据层
    - payload: 读取分页或写入操作
    - context: 可选的活动上下文

    返回:
    - 当前分页快照, 修改时包含备份 ID
    """
    service = ConversationDataService(database)
    messages = copy.deepcopy(context.get_context()) if context is not None else None
    saved: dict[str, Any] = {}
    if payload.get("action"):
        saved = service.mutate(conversation, layer, payload, live_messages=messages)
        if context is not None and layer == "context":
            context._replace_messages_content(saved["messages"])
            context._saved_count = len(saved["messages"])
            context._runtime_state = type(context._runtime_state)()
            context._runtime_state_dirty = False
            messages = copy.deepcopy(context.get_context())
        # 内存替换在线程结束前完成, 请求取消不会留下已写磁盘但仍使用旧内存的窗口
    result = service.read(conversation, layer, int(payload.get("offset", 0)), int(payload.get("limit", 50)), live_messages=messages)
    return {**result, **{key: saved[key] for key in ("saved", "backup_id") if key in saved}}


async def manage_platform_data(database: str, manager: Any, conversation: str, layer: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    协调平台活动实例, 不实例化未激活的会话

    参数:
    - database: 平台数据库路径
    - manager: 已存在的 SessionManager, None 表示该平台未激活
    - conversation: 主上下文或工作流上下文 ID
    - layer: 数据层
    - payload: 操作参数

    返回:
    - 数据快照, 忙碌或多实例共享同一上下文时拒绝编辑
    """
    matches = []
    if manager is not None:
        for entry in manager.pool.list_entries().values():
            for context in entry.session._all_contexts().values():
                if context.conversation_id == conversation:
                    matches.append((entry, context))
    if not matches:
        if manager is None:
            return await RAG_WORKERS.run(perform_data_operation, database, conversation, layer, payload)

        def operate_cold() -> dict[str, Any]:
            """持有创建锁时重新检查归属, 防止实例在查找后激活并缓存旧数据"""
            with manager._entry_creation_lock:
                for identity, entry in manager.pool.list_entries().items():
                    if conversation == identity or conversation.startswith(identity + "_") or any(context.conversation_id == conversation for context in entry.session._all_contexts().values()):
                        raise ConversationDataConflict("实例正在激活或上下文归属已变化, 请重新读取")
                return perform_data_operation(database, conversation, layer, payload)

        return await RAG_WORKERS.run(operate_cold)
    if len(matches) != 1:
        raise ConversationDataConflict("多个活动实例共享此上下文, 请先停止相关实例")
    entry, context = matches[0]
    assert manager is not None
    if entry.retiring or entry.active_calls or entry.async_operation_lock.locked():
        raise ConversationDataConflict("此对话正在处理消息, 请等待本轮完成")

    def operate_locked() -> dict[str, Any]:
        """同步锁在线程内获取并释放, 只编辑仍属于当前实例的上下文"""
        if not entry.sync_operation_lock.acquire(blocking=False):
            raise ConversationDataConflict("此对话正在处理消息, 请等待本轮完成")
        try:
            if entry not in manager.pool.list_entries().values() or entry.retiring:
                raise ConversationDataConflict("活动实例已变化, 请重新读取")
            return perform_data_operation(database, conversation, layer, payload, context)
        finally:
            entry.sync_operation_lock.release()

    async with entry.async_operation_lock:
        return await RAG_WORKERS.run(operate_locked)
