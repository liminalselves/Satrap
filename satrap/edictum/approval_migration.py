"""
审批插件拆分的配置迁移

按原配置层级迁移全局参数, 会话覆盖和命令开关, 新配置显式值优先;
动态 YAML/JSON 配置仅在本模块边界使用 Any, 不向审批引擎传递任意运行时对象
"""
from __future__ import annotations

from contextlib import closing
import traceback
from typing import TYPE_CHECKING, Any, cast
from copy import deepcopy
import json
import time

from satrap.core.storage.file_lock import FileLock, database_session_lock
from satrap.core.storage.persist import atomic_write_json
from satrap.core.schema_names import SESSION_OVERRIDE_TABLE

from satrap.core.log import logger


APPROVAL_FIELDS = frozenset({"approval_model"})


if TYPE_CHECKING:
    from satrap.core.config.session_overrides import SessionOverrideStore
    from satrap.edictum.plugin_catalog import PluginCatalog
    from satrap.edictum.plugin_config import PluginConfigManager


def migrate_approval_globals(manager: PluginConfigManager) -> None:
    """
    原子保存新全局配置后清理旧审批字段, 中断后可重复执行

    参数:
    - manager: 插件全局配置管理器
    """
    source = manager.global_path("satrap_coding")
    target = manager.global_path("permissions")
    if not source.is_file():
        return
    from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK

    try:
        with REFERENCE_SCAN_LOCK, FileLock(source.with_name(f".{source.name}.lock")), FileLock(target.with_name(f".{target.name}.lock")):
            old = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(old, dict):
                raise ValueError("编程插件全局配置必须是对象")
            moved = {key: old[key] for key in APPROVAL_FIELDS if key in old}
            if not moved:
                return
            new = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
            if not isinstance(new, dict):
                raise ValueError("审批插件全局配置必须是对象")
            if old.get("data_root") and "data_root" not in new:
                new["data_root"] = old["data_root"]
            for key, value in moved.items():
                if key in new and new[key] != value:
                    logger.warning(f"[插件迁移] 全局审批配置冲突, 保留新插件显式值, 字段={key}")
                new.setdefault(key, value)
            for path, values in ((target, new), (source, {key: value for key, value in old.items() if key not in APPROVAL_FIELDS})):
                atomic_write_json(path, values, indent=2)
            logger.info("[插件迁移] 已迁移审批全局配置")
    except Exception:
        logger.error("[插件迁移] 审批全局配置迁移失败, 原配置保留以便重试" + "\n" + traceback.format_exc())
        raise


def migrate_approval_overrides(store: SessionOverrideStore, session_id: str) -> bool:
    """
    在同一数据库事务中迁移会话覆盖, 新插件显式值优先

    参数:
    - store: 会话覆盖存储
    - session_id: 当前主会话 ID

    返回:
    - 是否迁移了旧审批覆盖字段
    """
    from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK

    try:
        with REFERENCE_SCAN_LOCK, database_session_lock(store.database, session_id), closing(store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(f"SELECT * FROM {SESSION_OVERRIDE_TABLE} WHERE session_id=? AND namespace='plugins.satrap_coding'", (session_id,)).fetchone()
            if row is None:
                return False
            old = json.loads(row["config_json"])
            if not isinstance(old, dict):
                raise ValueError("编程插件会话覆盖必须是对象")
            moved = {key: old[key] for key in APPROVAL_FIELDS if key in old}
            if not moved:
                return False
            current = connection.execute(f"SELECT * FROM {SESSION_OVERRIDE_TABLE} WHERE session_id=? AND namespace='plugins.permissions'", (session_id,)).fetchone()
            new = json.loads(current["config_json"]) if current else {}
            if not isinstance(new, dict):
                raise ValueError("审批插件会话覆盖必须是对象")
            if old.get("data_root") and "data_root" not in new:
                new["data_root"] = old["data_root"]
            for key, value in moved.items():
                if key in new and new[key] != value:
                    logger.warning(f"[插件迁移] 会话审批配置冲突, 保留新插件显式值, 字段={key}")
                new.setdefault(key, value)
                old.pop(key)
            now = time.time()
            connection.execute(f'INSERT INTO {SESSION_OVERRIDE_TABLE} (session_id, namespace, config_json, schema_version, revision, updated_at) '
                               "VALUES (?, 'plugins.permissions', ?, 1, ?, ?) ON CONFLICT(session_id, namespace) DO UPDATE SET "
                               "config_json=excluded.config_json, revision=excluded.revision, updated_at=excluded.updated_at",
                               (session_id, json.dumps(new, ensure_ascii=False), int(current["revision"]) + 1 if current else 1, now))
            connection.execute(f'UPDATE {SESSION_OVERRIDE_TABLE} SET config_json=?, revision=revision+1, updated_at=? '
                               "WHERE session_id=? AND namespace='plugins.satrap_coding'", (json.dumps(old, ensure_ascii=False), now, session_id))
            return True
    except Exception:
        logger.error("[插件迁移] 审批会话覆盖迁移失败, 事务已回滚" + "\n" + traceback.format_exc())
        raise


def migrate_approval_specs(value: list[Any], catalog: PluginCatalog) -> list[Any]:
    """
    将旧 coding 配置中的审批命令与模型迁至独立插件

    参数:
    - value: Chat 或平台插件配置列表
    - catalog: 当前可用插件目录

    返回:
    - 迁移后的独立副本; 新配置及显式关闭优先, 已迁移的配置不再次补回插件
    """
    entry = catalog.get("satrap_coding")
    if entry is None or entry.config_version < 1:
        return deepcopy(value)
    result = deepcopy(value)
    for index, item in enumerate(result):
        if item == "satrap_coding":
            item = {"name": "satrap_coding"}
            result[index] = item
        if not isinstance(item, dict) or item.get("name") != "satrap_coding":
            continue
        item = cast(dict[str, Any], item)
        version = item.get("config_version", 0)
        if type(version) is not int or version < 0:
            raise ValueError("旧 coding 配置版本无效")
        if version >= 1:
            continue
        config = item.setdefault("config", {})
        caps = item.setdefault("capabilities", {})
        if not isinstance(config, dict) or not isinstance(caps, dict) or not isinstance(caps.get("commands", {}), dict):
            raise ValueError("旧 coding 配置或命令开关无效")
        commands = caps.setdefault("commands", {})
        approve = commands.pop("approve", True)
        if type(approve) is not bool:
            raise ValueError("旧审批命令开关必须为布尔值")
        existing = next((candidate for candidate in result
                         if candidate == "permissions" or isinstance(candidate, dict) and candidate.get("name") == "permissions"), None)
        if existing == "permissions":
            existing = {"name": "permissions"}
            result[result.index("permissions")] = existing
        if existing is None:
            if catalog.get("permissions") is None:
                raise ValueError("旧审批命令迁移需要可用的 permissions 插件")
            existing = {"name": "permissions", "enabled": item.get("enabled", True),
                        "capabilities": {"commands": {"approve": approve}}}
            result.append(existing)
        existing = cast(dict[str, Any], existing)
        new_caps = existing.setdefault("capabilities", {})
        if not isinstance(new_caps, dict) or not isinstance(new_caps.get("commands", {}), dict):
            raise ValueError("权限插件命令开关必须是对象")
        new_caps.setdefault("commands", {}).setdefault("approve", approve)
        new_config = existing.setdefault("config", {})
        if not isinstance(new_config, dict):
            raise ValueError("权限插件配置必须是对象")
        for key in APPROVAL_FIELDS:
            if key in config:
                new_config.setdefault(key, config.pop(key))
        if config.get("data_root"):
            new_config.setdefault("data_root", config["data_root"])
        item["config_version"] = 1
    return result
