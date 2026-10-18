"""
宿主环境的版本化绑定

环境快照只保存目录和作用域资料, 不包含当前发话者;
宿主统一绑定版本化目录, 插件不再读取 coding 路径属性
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from pathlib import Path


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """单次操作使用的不可变宿主环境"""

    workspace_root: Path | None = None
    upload_root: Path | None = None
    sandbox_root: Path | None = None
    scope: str = ""
    project: str = ""
    revision: int = 0
    session_root: Path | None = None
    artifacts_root: Path | None = None


_LOCK = RLock()


def bind_environment(session: object, snapshot: EnvironmentSnapshot) -> EnvironmentSnapshot:
    """
    更新会话宿主绑定并递增版本

    参数:
    - session: 实际宿主会话
    - snapshot: 新的环境资料, 输入 revision 不作为版本来源

    返回:
    - 已绑定的快照; 内容未变化时沿用原版本
    """
    with _LOCK:
        previous = environment_snapshot(session)
        roots = {"workspace_root": snapshot.workspace_root, "upload_root": snapshot.upload_root,
                 "sandbox_root": snapshot.sandbox_root, "session_root": snapshot.session_root,
                 "artifacts_root": snapshot.artifacts_root}
        candidate = replace(snapshot, revision=previous.revision,
                            **{name: value.resolve() if value is not None else None for name, value in roots.items()})
        if previous == candidate:
            return previous
        candidate = replace(candidate, revision=previous.revision + 1)
        setattr(session, "_plugin_environment", candidate)
        return candidate


def environment_snapshot(session: object | None) -> EnvironmentSnapshot:
    """
    取得宿主环境, 未绑定时返回空快照

    参数:
    - session: 实际宿主会话, 独立工具可传 None

    返回:
    - 冻结环境资料, 尚未绑定的目录为 None
    """
    with _LOCK:
        snapshot: object = getattr(session, "_plugin_environment", None)
        return snapshot if isinstance(snapshot, EnvironmentSnapshot) else EnvironmentSnapshot()
