"""独立记忆插件的当前安装状态与同步协程桥接"""
from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

from satrap.core.platform.loop_bridge import PlatformLoopUnavailable, run_on_platform_loop
from satrap.core.plugin_authorization import plugin_tool_live
from satrap.core.group_chat.reply import current_reply_turn
from satrap.core.memory.scoped import MemoryError
from satrap.core.call_context import current_call_origin, is_group_origin
from satrap.core.platform import current_adapter_manager
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
    if tool is not None:
        reason = plugin_tool_live(session, "memory", name, tool)
        if reason == "entry_disabled":
            raise MemoryError("stale_call", "记忆插件或该能力已停用")
        if reason is not None:
            raise MemoryError("stale_call", "记忆工具已从原会话移除或停用")
        return dict(config)
    plugin = next((item for item in session.list_plugins() if item.name == "memory"), None)
    if plugin is None or not plugin.enabled or not getattr(plugin, kind).get(name, False):
        raise MemoryError("stale_call", "记忆插件或该能力已停用")
    return dict(config)


def group_call() -> bool:
    """
    判断当前可信来源是否属于群聊

    返回:
    - 当前来源为群聊时返回 True
    """
    return is_group_origin(current_call_origin())


def wait_host(coroutine: Coroutine[Any, Any, Any]) -> Any:
    """
    从同步会话的工作线程调用原平台事件循环

    参数:
    - coroutine: 需要在原平台循环执行的宿主操作

    返回:
    - 宿主结果; 循环不可用或超时返回 ok 为 False 的失败结果, 且已取消提交
    """
    origin = current_call_origin()
    manager = current_adapter_manager()
    adapter = manager.get_adapter(origin.adapter_id) if manager and origin else None
    turn = current_reply_turn()
    loop = turn.loop if turn is not None else getattr(adapter, "_loop", None)
    try:
        try:
            return run_on_platform_loop(coroutine, loop, 30)
        except PlatformLoopUnavailable as error:
            raise MemoryError("unavailable", "同步记忆操作需要有效的平台工作线程") from error
        except TimeoutError:
            raise MemoryError("unavailable", "记忆操作超时, 已取消等待")
    except Exception as exc:
        logger.warning(f"[长期记忆] 同步桥接失败: {exc}")
        return {"ok": False, "code": getattr(exc, "code", "unavailable"), "error": str(exc)}
