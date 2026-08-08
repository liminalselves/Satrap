"""satrap_coding 插件级共享状态: 工具/命令/处理器共享同一实例

关键: /plan 设置的计划模式必须作用于工具审批引擎, /goal 注入必须与命令写入的目标一致,
因此 tools.py / commands.py / handlers.py 通过本模块获取同一份状态 (按会话隔离)。
"""
from __future__ import annotations

import threading
from typing import Any

from satrap.expend.plugins.satrap_coding import tools as tools_mod
from satrap.expend.plugins.satrap_coding.core.goal_state import GoalState
from satrap.expend.plugins.satrap_coding.core.memory_store import MemoryStore
from satrap.expend.plugins.satrap_coding.core.permission import PermissionEngine

_registry: dict[str, dict[str, Any]] = {}
_registry_lock = threading.Lock()


def _build_state(session: Any) -> dict[str, Any]:
    """构建一份插件状态 (权限引擎 / 记忆库 / 目标状态 / 任务清单)"""
    scope = tools_mod.user_scope(session.session_id)
    engine = PermissionEngine(
        rules_file=tools_mod.DATA_ROOT / "permissions.json",
        log_file=tools_mod.DATA_ROOT / "approval_log.jsonl",
    )
    store = MemoryStore(db_path=tools_mod.DATA_ROOT / "memory.db", scope=scope)
    goals = GoalState(file_path=tools_mod.DATA_ROOT / "goal.json")
    return {
        "engine": engine,
        "store": store,
        "goals": goals,
        "todos": {"items": []},
    }


def get_plugin_state(session: Any) -> dict[str, Any]:
    """获取会话的插件共享状态 (同 ID 不同对象时重建, 防测试/重建污染)"""
    sid = session.session_id
    with _registry_lock:
        state = _registry.get(sid)
        if state is None or state.get("_owner") is not session:
            state = _build_state(session)
            state["_owner"] = session
            _registry[sid] = state
    return state


def reset_plugin_state(session: Any) -> None:
    """卸载插件时重置会话状态 (下次安装重建)"""
    with _registry_lock:
        state = _registry.get(session.session_id)
        if state is not None and state.get("_owner") is session:
            _registry.pop(session.session_id, None)
