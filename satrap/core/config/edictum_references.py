"""Edictum 命名配置的会话实例引用查询与迁移 (供控制服务与 CLI 离线路径共用)"""
from __future__ import annotations

from satrap.core.framework.SessionManager import SessionConfigStore
from satrap.core.storage import CHAT_PLATFORM_ID, StorageLayout
from satrap.edictum.registry import EDICTUM_PROVIDER


def _all_platform_ids(layout: StorageLayout, platform_ids: list[str]) -> list[str]:
    """
    合并配置平台与 chat 内部平台

    参数:
    - layout: 数据布局
    - platform_ids: 配置声明的平台实例 ID

    返回:
    - list[str]: 去重后的平台实例 ID (含 chat)
    """
    result = list(platform_ids)
    if CHAT_PLATFORM_ID not in result:
        result.append(CHAT_PLATFORM_ID)
    return result


def list_edictum_config_references(
    layout: StorageLayout,
    platform_ids: list[str],
    config_name: str,
) -> list[dict[str, str]]:
    """
    列出全部可管理平台中对指定 Edictum 配置的会话引用

    参数:
    - layout: 数据布局
    - platform_ids: 配置声明的平台实例 ID
    - config_name: Edictum 配置名称

    返回:
    - list[dict[str, str]]: 平台和会话引用
    """
    references: list[dict[str, str]] = []
    for platform_id in _all_platform_ids(layout, platform_ids):
        store = SessionConfigStore(layout.platform_db(platform_id))
        references.extend(
            {"platform_id": platform_id, "session_id": session_id}
            for session_id in store.list_definition_references(
                EDICTUM_PROVIDER,
                config_name,
            )
        )
    return references


def rename_edictum_config_references(
    layout: StorageLayout,
    platform_ids: list[str],
    old_name: str,
    new_name: str,
) -> list[dict[str, str]]:
    """
    迁移全部可管理平台中的 Edictum 配置引用 (失败时回滚已完成平台)

    参数:
    - layout: 数据布局
    - platform_ids: 配置声明的平台实例 ID
    - old_name: 原配置名称
    - new_name: 新配置名称

    返回:
    - list[dict[str, str]]: 已迁移的平台和会话引用
    """
    completed: list[SessionConfigStore] = []
    migrated: list[dict[str, str]] = []
    try:
        for platform_id in _all_platform_ids(layout, platform_ids):
            store = SessionConfigStore(layout.platform_db(platform_id))
            session_ids = store.rename_definition_references(
                EDICTUM_PROVIDER,
                old_name,
                new_name,
            )
            completed.append(store)
            migrated.extend(
                {"platform_id": platform_id, "session_id": session_id}
                for session_id in session_ids
            )
    except Exception:
        for store in reversed(completed):
            store.rename_definition_references(
                EDICTUM_PROVIDER,
                new_name,
                old_name,
            )
        raise
    return migrated
