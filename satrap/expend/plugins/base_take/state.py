"""
base_take 插件级共享状态: 记忆库按会话隔离单例

memory 工具与 inject handler 通过本模块获取同一份 MemoryStore (按会话隔离),
scope 默认 web_chat (网页聊天统一), 可被插件配置 memory_scope 覆盖

记忆分层 (项目功能): 状态携带 global_scope / project_scope 两个键;
项目会话由 display.service 在建会话后调用 store 分层绑定
(store.scopes = [全局, 项目], store.scope = 项目层为写入默认),
无项目会话保持单层 (scopes = [global_scope])
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from satrap.expend.tools.memory_store import DEFAULT_MEMORY_DB, MemoryStore

if TYPE_CHECKING:
    from satrap.edictum import AsyncSimpleSession, SimpleSession

    SessionType = SimpleSession | AsyncSimpleSession
    """插件支持的会话类型"""

_registry: dict[str, dict[str, Any]] = {}
_registry_lock = threading.Lock()


def _build_state(session: SessionType, config: dict[str, Any]) -> dict[str, Any]:
    """
    构建一份插件状态 (记忆库); global_scope 为全局层, project_scope 初始为空 (未绑项目)

    参数:
    - session: 会话
    - config: 配置信息

    返回:
    - dict[str, Any]: 构建一份插件状态 (记忆库); global_scope 为全局层, project_scope 初始为空 (未绑项目)
    """
    scope = str(config.get("memory_scope") or "web_chat")
    mode = str(config.get("memory_mode") or "full")
    store = MemoryStore(db_path=DEFAULT_MEMORY_DB, scope=scope, mode=mode)
    return {"store": store, "global_scope": scope, "project_scope": None}


def get_plugin_state(session: SessionType, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    获取会话的插件共享状态 (同 ID 不同对象时重建, 防测试/重建污染)

    参数:
    - session: 会话
    - config: 配置信息

    返回:
    - dict[str, Any]: 会话的插件共享状态 (同 ID 不同对象时重建, 防测试/重建污染)
    """
    sid = session.session_id
    with _registry_lock:
        state = _registry.get(sid)
        if state is None or state.get("_owner") is not session:
            state = _build_state(session, config or {})
            state["_owner"] = session
            _registry[sid] = state
    return state


def reset_plugin_state(session: SessionType) -> None:
    """
    卸载插件时重置会话状态 (下次安装重建)

    参数:
    - session: 会话
    """
    with _registry_lock:
        state = _registry.get(session.session_id)
        if state is not None and state.get("_owner") is session:
            _registry.pop(session.session_id, None)
