"""
satrap_coding 插件级共享状态: 工具/命令/处理器共享同一实例

关键: /plan 设置的计划模式必须作用于工具审批引擎, /goal 注入必须与命令写入的目标一致,
因此 tools.py / commands.py / handlers.py 通过本模块获取同一份状态 (按会话隔离)

注: 长期记忆已移交 base_take 插件, 本插件不再持有 MemoryStore
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from satrap.core.utils.paths import get_project_root
from satrap.core.type import safe_getattr
from satrap.core.storage import storage_key
from satrap.expend.plugins.satrap_coding.core.goal_state import GoalState
from satrap.expend.plugins.satrap_coding.core.permission import PermissionEngine

if TYPE_CHECKING:
    from satrap.edictum import AsyncSimpleSession, SimpleSession

    SessionType = SimpleSession | AsyncSimpleSession
    """插件支持的会话类型"""

_registry: dict[int, dict[str, Any]] = {}
_registry_lock = threading.Lock()


def _build_state(session: SessionType, data_root: Path) -> dict[str, Any]:
    """
    构建一份插件状态 (权限引擎 / 目标状态 / 任务清单)

    参数:
    - session: 会话
    - data_root: 插件数据目录

    返回:
    - dict[str, Any]: 构建一份插件状态 (权限引擎 / 目标状态 / 任务清单)
    """
    engine = PermissionEngine(
        rules_file=data_root / "permissions.json",
        log_file=data_root / "approval_log.jsonl",
    )
    goals = GoalState(file_path=data_root / "goal.json")
    return {
        "engine": engine,
        "goals": goals,
        "todos": {"items": []},
        "_data_root": str(data_root),
    }


def get_plugin_state(session: SessionType, data_root: Path | None = None) -> dict[str, Any]:
    """
    获取会话的插件共享状态 (同 ID 不同对象时重建, 防测试/重建污染)

    参数:
    - session: 会话
    - data_root: 插件数据目录

    返回:
    - dict[str, Any]: 会话的插件共享状态 (同 ID 不同对象时重建, 防测试/重建污染)
    """
    session_cache = safe_getattr(session, "coding_cache_root")
    session_context = safe_getattr(session, "session_ctx")
    context_database = safe_getattr(session_context, "db_path")
    if session_cache:
        default_root = Path(str(session_cache)) / "satrap_coding"
    elif context_database:
        default_root = (
            Path(str(context_database)).resolve().parent
            / "sessions"
            / storage_key(session.session_id, fallback="session")
            / "cache"
            / "satrap_coding"
        )
    else:
        default_root = get_project_root() / ".satrap" / "data" / "unscoped" / storage_key(
            session.session_id,
            fallback="session",
        )
    resolved_data_root = (data_root or default_root).resolve()
    registry_key = id(session)
    with _registry_lock:
        state = _registry.get(registry_key)
        if (
            state is None
            or state.get("_owner") is not session
            or (data_root is not None and state.get("_data_root") != str(resolved_data_root))
        ):
            state = _build_state(session, resolved_data_root)
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
