"""独立记忆插件的当前安装状态与同步协程桥接"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from satrap.core.call_context import current_call_origin
from satrap.core.platform import current_adapter_manager
from satrap.core.group_chat.reply import current_reply_turn
from satrap.core.memory.scoped import MemoryError
from satrap.core.log import logger


def active_config(session: Any, config: dict[str, Any], kind: str, name: str, tool: Any = None) -> dict[str, Any]:
    """
    每次读取当前插件及独立能力状态, 不依赖安装时的旧权限结论

    参数:
    - session: 原始主会话, 独立工具调用可为 None
    - config: 安装配置快照
    - kind: tools, commands 或 handlers
    - name: 固定能力名称
    - tool: 工具调用时的原实例

    返回:
    - 配置副本, 插件或能力失效时拒绝
    """
    if session is None:
        return dict(config)
    plugin = next((item for item in session.list_plugins() if item.name == "memory"), None)
    if plugin is None or not plugin.enabled or not getattr(plugin, kind).get(name, False):
        raise MemoryError("stale_call", "记忆插件或该能力已停用")
    if tool is not None:
        manager = session.tools_manager
        if manager.tools.get(name) is not tool or not manager.is_tool_enabled(name) or not tool.is_enabled():
            raise MemoryError("stale_call", "记忆工具已从原会话移除或停用")
    return dict(config)


def group_call() -> bool:
    """
    判断当前可信来源是否属于群聊

    返回:
    - 当前来源为群聊时返回 True
    """
    origin = current_call_origin()
    return origin is not None and (origin.conversation_kind == "group" or not origin.conversation_kind and origin.chat_type == "GroupMessage")


def wait_host(coroutine: Coroutine[Any, Any, Any]) -> Any:
    """
    从同步会话的工作线程调用原平台事件循环

    参数:
    - coroutine: 需要在原平台循环执行的宿主操作

    返回:
    - 宿主结果, 超时或桥接失败返回明确错误
    """
    origin = current_call_origin()
    manager = current_adapter_manager()
    adapter = manager.get_adapter(origin.adapter_id) if manager and origin else None
    turn = current_reply_turn()
    loop = turn.loop if turn is not None else getattr(adapter, "_loop", None)
    try:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or not loop.is_running() or running is loop:
            coroutine.close()
            raise MemoryError("unavailable", "同步记忆操作需要有效的平台工作线程")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=30)
        except TimeoutError:
            future.cancel()
            raise MemoryError("unavailable", "记忆操作超时, 已取消等待")
    except Exception as exc:
        logger.warning(f"[长期记忆] 同步桥接失败: {exc}")
        return {"ok": False, "code": getattr(exc, "code", "unavailable"), "error": str(exc)}
