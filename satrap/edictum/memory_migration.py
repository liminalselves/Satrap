"""记忆插件拆分的配置迁移, 不保留旧运行入口"""
from __future__ import annotations
import traceback

from copy import deepcopy
from contextlib import closing
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from satrap.core.log import logger


MEMORY_FIELDS = frozenset({"memory_scope", "memory_mode"})
MEMORY_TOOLS = frozenset({"add_memory", "update_memory", "delete_memory", "list_memories"})

from satrap.core.storage.file_lock import FileLock, database_session_lock


def migrate_memory_globals(manager: Any) -> None:
    """
    原子保存新全局配置后清理旧记忆字段, 中断后可重复执行

    参数:
    - manager: 插件全局配置管理器
    """
    source = manager._global_path("base_take")
    target = manager._global_path("memory")
    if not source.is_file():
        return
    from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK

    try:
        with REFERENCE_SCAN_LOCK, FileLock(source.with_name(f".{source.name}.lock")), FileLock(target.with_name(f".{target.name}.lock")):
            old = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(old, dict):
                raise ValueError("基础插件全局配置必须是对象")
            moved = {key: old[key] for key in MEMORY_FIELDS if key in old}
            if not moved:
                return
            new = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
            if not isinstance(new, dict):
                raise ValueError("记忆插件全局配置必须是对象")
            for key, value in moved.items():
                if key in new and new[key] != value:
                    logger.warning(f"[插件迁移] 全局记忆配置冲突, 保留新插件显式值, 字段={key}")
                new.setdefault(key, value)
            for path, values in ((target, new), (source, {key: value for key, value in old.items() if key not in MEMORY_FIELDS})):
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", suffix=".tmp", delete=False) as output:
                        temporary = Path(output.name)
                        json.dump(values, output, ensure_ascii=False, indent=2, allow_nan=False)
                    temporary.replace(path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            logger.info("[插件迁移] 已迁移记忆全局配置")
    except Exception:
        logger.error("[插件迁移] 记忆全局配置迁移失败, 原配置保留以便重试" + "\n" + traceback.format_exc())
        raise


def migrate_memory_overrides(store: Any, session_id: str) -> bool:
    """
    在同一数据库事务中迁移会话覆盖, 新插件显式值优先

    参数:
    - store: 会话覆盖存储
    - session_id: 当前主会话 ID

    返回:
    - 是否迁移了旧记忆覆盖字段
    """
    from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK

    try:
        with REFERENCE_SCAN_LOCK, database_session_lock(store.database, session_id), closing(store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM session_config_overrides WHERE session_id=? AND namespace='plugins.base_take'", (session_id,)).fetchone()
            if row is None:
                return False
            old = json.loads(row["config_json"])
            if not isinstance(old, dict):
                raise ValueError("基础插件会话覆盖必须是对象")
            moved = {key: old[key] for key in MEMORY_FIELDS if key in old}
            if not moved:
                return False
            current = connection.execute("SELECT * FROM session_config_overrides WHERE session_id=? AND namespace='plugins.memory'", (session_id,)).fetchone()
            new = json.loads(current["config_json"]) if current else {}
            if not isinstance(new, dict):
                raise ValueError("记忆插件会话覆盖必须是对象")
            for key, value in moved.items():
                if key in new and new[key] != value:
                    logger.warning(f"[插件迁移] 会话记忆配置冲突, 保留新插件显式值, 字段={key}")
                new.setdefault(key, value)
                old.pop(key)
            now = time.time()
            connection.execute("INSERT INTO session_config_overrides (session_id, namespace, config_json, schema_version, revision, updated_at) "
                               "VALUES (?, 'plugins.memory', ?, 1, ?, ?) ON CONFLICT(session_id, namespace) DO UPDATE SET "
                               "config_json=excluded.config_json, revision=excluded.revision, updated_at=excluded.updated_at",
                               (session_id, json.dumps(new, ensure_ascii=False), int(current["revision"]) + 1 if current else 1, now))
            connection.execute("UPDATE session_config_overrides SET config_json=?, revision=revision+1, updated_at=? "
                               "WHERE session_id=? AND namespace='plugins.base_take'", (json.dumps(old, ensure_ascii=False), now, session_id))
            return True
    except Exception:
        logger.error("[插件迁移] 记忆会话覆盖迁移失败, 事务已回滚" + "\n" + traceback.format_exc())
        raise


def migrate_memory_specs(value: list[Any], catalog: Any) -> list[Any]:
    """
    将旧基础插件中的记忆配置迁到独立插件

    参数:
    - value: Chat 或平台 Agent 的插件配置列表
    - catalog: 实际可用的插件目录

    返回:
    - 迁移后的配置副本, 旧关闭状态保留, 缺少新插件时明确报错
    """
    result = deepcopy(value)
    base: Any = next((item for item in result if isinstance(item, dict) and item.get("name") == "base_take"), None)
    bare = "base_take" in result
    if base is None and not bare:
        return result
    base = base if base is not None else {"name": "base_take"}
    config = base.setdefault("config", {})
    caps = base.setdefault("capabilities", {})
    if not isinstance(config, dict) or not isinstance(caps, dict):
        return result
    for kind in ("tools", "handlers", "commands"):
        if not isinstance(caps.get(kind, {}), dict):
            return result
    tools = caps.get("tools", {})
    handlers = caps.get("handlers", {})
    commands = caps.get("commands", {})
    explicit = bool(MEMORY_FIELDS & config.keys() or MEMORY_TOOLS & tools.keys()
                    or "base_take.memory_inject" in handlers or "memory" in commands)
    implicit = not caps
    target = next((item for item in result if isinstance(item, dict) and item.get("name") == "memory"), None)
    existing = target is not None or "memory" in result
    if not explicit and (existing or not implicit):
        return result
    entry = catalog.get("memory")
    if entry is None:
        logger.error("[插件迁移] 旧记忆能力迁移需要可用的 memory 插件")
        raise ValueError("旧记忆能力迁移需要可用的 memory 插件")
    if bare:
        result[result.index("base_take")] = base
    if target is None:
        target = {"name": "memory", "enabled": base.get("enabled", True), "config": {}, "capabilities": {}}
        if existing:
            result[result.index("memory")] = target
        else:
            target["capabilities"] = {kind: {name: False for name in names} for kind, names in entry.capabilities.items()}
            result.append(target)
    target_config = target.setdefault("config", {})
    target_caps = target.setdefault("capabilities", {})
    if not isinstance(target_config, dict) or not isinstance(target_caps, dict):
        raise ValueError("记忆插件配置或能力开关无效")
    for key in MEMORY_FIELDS:
        if key in config:
            old = config.pop(key)
            if key in target_config and target_config[key] != old:
                logger.warning(f"[插件迁移] 记忆配置冲突, 保留独立插件的显式值, 字段={key}")
            target_config.setdefault(key, old)
    moves = [("tools", name, name) for name in sorted(MEMORY_TOOLS)]
    moves += [("handlers", "base_take.memory_inject", "memory.memory_inject"), ("commands", "memory", "memory")]
    for kind, old, new in moves:
        old_map = caps.setdefault(kind, {})
        state = old_map.pop(old, True)
        if type(state) is not bool:
            raise ValueError("旧记忆能力开关必须为布尔值")
        state = state and base.get("enabled", True) is True
        new_map = target_caps.setdefault(kind, {})
        if not isinstance(new_map, dict):
            raise ValueError("记忆能力配置必须为对象")
        previous = new_map.get(new, True)
        if type(previous) is not bool:
            raise ValueError("记忆能力开关必须为布尔值")
        new_map[new] = state and previous if existing else state
    base_entry = catalog.get("base_take")
    if base_entry is not None:
        for kind, names in base_entry.capabilities.items():
            values = caps.setdefault(kind, {})
            for name in names:
                values.setdefault(name, True)
    return result
