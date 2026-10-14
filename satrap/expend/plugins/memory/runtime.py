"""
长期记忆插件的状态复核与宿主等待边界

基于可信调用来源和当前安装状态复核记忆能力,
同步操作桥接到原平台循环, 异步操作直接等待宿主,
统一约束等待期限并转换失败结果, 保留外部任务取消信号
"""
from __future__ import annotations

from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, TypeVar
import asyncio

from satrap.core.platform.loop_bridge import PlatformLoopUnavailable, run_on_platform_loop
from satrap.core.plugin_authorization import BoundPluginTool, plugin_tool_live
from satrap.core.group_chat.reply import current_reply_turn
from satrap.core.memory.scoped import MemoryError
from satrap.core.call_context import current_call_origin, is_group_origin
from satrap.core.platform import current_adapter_manager
from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.edictum import AsyncSimpleSession, SimpleSession

    SessionType = SimpleSession | AsyncSimpleSession


def active_config(session: SessionType | None, config: dict[str, Any], kind: str, name: str, tool: BoundPluginTool | None = None) -> dict[str, Any]:
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


T = TypeVar("T")
HOST_TIMEOUT_SECONDS = 30


async def await_host(coroutine: Coroutine[Any, Any, T]) -> T | dict[str, Any]:
    """
    异步等待当前循环的记忆宿主操作, 超时取消协程并转换失败结果

    参数:
    - coroutine: 需要在当前来源平台循环执行的宿主操作, 等待期限为 HOST_TIMEOUT_SECONDS

    返回:
    - 宿主结果原样返回; 超时或其他操作异常返回 ok 为 False 的失败结果, 外部任务取消继续向上传播
    """
    # Step.1 限制等待期限, 超时取消宿主协程并保留外部任务取消信号
    try:
        try:
            return await asyncio.wait_for(coroutine, timeout=HOST_TIMEOUT_SECONDS)
        except TimeoutError:
            raise MemoryError("unavailable", "记忆操作超时, 已取消等待")
    except Exception as exc:
        # Step.2 记录已隔离的操作失败, 以稳定错误结构交给命令入口转换
        logger.warning(f"[长期记忆] 异步等待失败: {exc}")
        return {"ok": False, "error": str(exc), "error_type": getattr(exc, "code", "unavailable")}


def wait_host(coroutine: Coroutine[Any, Any, T]) -> T | dict[str, Any]:
    """
    同步等待记忆宿主操作, 从工作线程桥接到原平台事件循环

    参数:
    - coroutine: 需要在原平台循环执行的宿主操作, 等待期限为 HOST_TIMEOUT_SECONDS

    返回:
    - 宿主结果原样返回; 循环不可用, 同循环阻塞, 超时或其他操作异常返回 ok 为 False 的失败结果
    - 拒绝桥接时关闭未提交的协程, 超时取消已提交的协程
    """
    # Step.1 解析来源循环, 回复轮次持有的循环优先于适配器循环
    origin = current_call_origin()
    manager = current_adapter_manager()
    adapter = manager.get_adapter(origin.adapter_id) if manager and origin else None
    turn = current_reply_turn()
    loop = turn.loop if turn is not None else getattr(adapter, "_loop", None)
    # Step.2 在线程中等待宿主结果, 区分来源循环不可用与同循环阻塞
    try:
        try:
            return run_on_platform_loop(coroutine, loop, HOST_TIMEOUT_SECONDS)
        except PlatformLoopUnavailable as error:
            raise MemoryError("unavailable", str(error)) from error
        except TimeoutError:
            raise MemoryError("unavailable", "记忆操作超时, 已取消等待")
    except Exception as exc:
        # Step.3 记录已隔离的操作失败, 以稳定错误结构交给命令或工具入口转换
        logger.warning(f"[长期记忆] 同步桥接失败: {exc}")
        return {"ok": False, "error": str(exc), "error_type": getattr(exc, "code", "unavailable")}
