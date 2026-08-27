"""
base_take 插件级共享状态: 记忆库按会话隔离单例

memory 工具与 inject handler 通过本模块获取同一份 MemoryStore (按会话隔离),
scope 使用会话注入的 `session:<session_id>`, 不提供跨会话共享层
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from satrap.core.type import safe_getattr
from satrap.expend.tools.memory_store import DEFAULT_MEMORY_DB, MemoryStore

if TYPE_CHECKING:
    from satrap.edictum import AsyncSimpleSession, SimpleSession

    SessionType = SimpleSession | AsyncSimpleSession
    """插件支持的会话类型"""

_registry: dict[int, dict[str, Any]] = {}
_registry_lock = threading.Lock()


def _build_state(session: SessionType, config: dict[str, Any]) -> dict[str, Any]:
    """
    构建当前会话的插件记忆状态

    参数:
    - session: 会话
    - config: 配置信息

    返回:
    - dict[str, Any]: 当前会话的插件记忆状态
    """
    scope = str(
        safe_getattr(session, "coding_memory_scope")
        or config.get("memory_scope")
        or f"session:{session.session_id}"
    )
    mode = str(config.get("memory_mode") or "full")
    db_path = safe_getattr(session, "coding_memory_db") or DEFAULT_MEMORY_DB
    store = MemoryStore(db_path=db_path, scope=scope, mode=mode)
    return {
        "store": store,
        "session_scope": scope,
    }


def get_plugin_state(session: SessionType, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    获取会话的插件共享状态 (同 ID 不同对象时重建, 防测试/重建污染)

    参数:
    - session: 会话
    - config: 配置信息

    返回:
    - dict[str, Any]: 会话的插件共享状态 (同 ID 不同对象时重建, 防测试/重建污染)
    """
    registry_key = id(session)
    with _registry_lock:
        state = _registry.get(registry_key)
        if state is None or state.get("_owner") is not session:
            state = _build_state(session, config or {})
            state["_owner"] = session
            _registry[registry_key] = state
    return state


def reset_plugin_state(session: SessionType) -> None:
    """
    卸载插件时重置会话状态 (下次安装重建)

    参数:
    - session: 会话
    """
    with _registry_lock:
        registry_key = id(session)
        state = _registry.get(registry_key)
        if state is not None and state.get("_owner") is session:
            _registry.pop(registry_key, None)
