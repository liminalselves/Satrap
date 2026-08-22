"""
base_take 插件命令: /memory (同步 + 异步)

约定:
- build_commands(session) 工厂返回 (同步命令映射, 异步命令映射)
- 命令共享插件状态 (state.py), 与记忆工具/注入处理器使用同一份 MemoryStore
"""
from __future__ import annotations

from typing import Any, Callable

from satrap.edictum import AsyncSimpleSession, SimpleSession

from satrap.expend.plugins.base_take.state import get_plugin_state
from satrap.expend.tools.memory_store import MemoryStore

_MEMORY_MODES = ("disabled", "base", "full")


def _parse_args(args: list[str], default: str = "") -> str:
    """
    命令参数列表 -> 单字符串 (保留空格)

    参数:
    - args: 额外位置参数
    - default: 默认值

    返回:
    - str: 命令参数列表 -> 单字符串 (保留空格)
    """
    if not args:
        return default
    return " ".join(str(a) for a in args).strip()


SessionType = SimpleSession | AsyncSimpleSession
"""插件支持的会话类型"""


def _cmd_memory_impl(state: dict[str, Any], args: list[str]) -> str:
    """
    记忆命令: list / add / del / clear / mode

    参数:
    - state: 状态
    - args: 额外位置参数

    返回:
    - str: 记忆命令: list / add / del / clear / mode
    """
    store = state["store"]
    assert isinstance(store, MemoryStore)
    sub = args[0] if args else "list"
    if sub in ("list", "查看"):
        memories = store.list_all()
        if not memories:
            return "当前没有长期记忆"
        global_scope = store.scopes[0] if store.scopes else store.scope
        layered = len([s for s in store.scopes if s]) > 1
        lines = [f"共 {len(memories)} 条记忆:"]
        for m in memories:
            tags = f" [{', '.join(m['tags'])}]" if m["tags"] else ""
            layer = ""
            if layered:
                layer = " (全局层)" if m["scope"] == global_scope else " (项目层)"
            lines.append(f"- {m['id']}{layer} [{m['title']}] {m['content']}{tags} (重要度 {m['importance']})")
        return "\n".join(lines)
    if sub in ("add", "添加"):
        rest = _parse_args(args[1:])
        if not rest:
            return "用法: /memory add <标题> <内容>"
        parts = rest.split(" ", 1)
        result = store.add(parts[0], parts[1] if len(parts) > 1 else "")
        return f"记忆已添加: [{result['title']}] {result['content']}" if result.get("ok") else f"添加失败: {result.get('error')}"
    if sub in ("del", "delete", "删除"):
        if len(args) < 2:
            return "用法: /memory del <记忆 ID>"
        result = store.delete(args[1])
        return f"记忆已删除: {args[1]}" if result.get("ok") else f"删除失败: {result.get('error')}"
    if sub in ("clear", "清空"):
        if not store.can_write():
            return store.write_denied_reason("清空")
        return f"已清空 {store.clear()} 条记忆"
    if sub in ("mode", "模式"):
        if len(args) < 2 or args[1] not in _MEMORY_MODES:
            return f"用法: /memory mode <{'|'.join(_MEMORY_MODES)}>"
        store.set_mode(args[1])
        return f"记忆模式已切换: {args[1]}"
    return "用法: /memory list | add <标题> <内容> | del <ID> | clear | mode <disabled|base|full>"


def build_commands(session: SessionType) -> tuple[dict[str, Callable[..., Any]], dict[str, Callable[..., Any]]]:
    """
    构建插件命令: 返回 (同步命令, 异步命令) 映射

    参数:
    - session: 会话

    返回:
    - tuple[dict[str, Callable[..., Any]], dict[str, Callable[..., Any]]]:  (同步命令, 异步命令) 映射
    """
    state = get_plugin_state(session)

    def cmd_memory(*args: str) -> str:
        """
        管理长期记忆 (list/add/del/clear/mode)

        参数:
        - args: 额外位置参数

        返回:
        - str: 管理长期记忆 (list/add/del/clear/mode)
        """
        return _cmd_memory_impl(state, list(args))

    async def cmd_memory_async(*args: str) -> str:
        return _cmd_memory_impl(state, list(args))

    sync_map: dict[str, Callable[..., Any]] = {
        "memory": cmd_memory,
    }
    async_map: dict[str, Callable[..., Any]] = {
        "memory": cmd_memory_async,
    }
    return sync_map, async_map
