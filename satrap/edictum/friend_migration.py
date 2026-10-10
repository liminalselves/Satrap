"""好友写开关迁移为工具状态, 旧配置只用于一次性迁移"""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from pathlib import Path
from typing import Any, cast
import json
import tempfile
import time
import traceback

from satrap.core.log import logger
from satrap.core.storage.file_lock import FileLock, database_session_lock

FRIEND_SWITCHES = {
    "request_handling_enabled": "friend_manager_handle_request",
    "delete_friend_enabled": "friend_manager_delete_friend",
    "send_request_enabled": "friend_manager_send_request",
}
GLOBAL_RECEIPT = "__capability_migration"


def legacy_bool(value: Any) -> bool:
    """
    沿用旧布尔字段的解析规则, 无效值按旧默认关闭

    参数:
    - value: 旧显式配置值

    返回:
    - 旧功能开关的实际布尔状态
    """
    from satrap.edictum.plugin_config import ConfigField
    return ConfigField("好友旧写开关", type="bool", default=False).validate(value) is True


def migrate_friend_globals(manager: Any) -> dict[str, bool]:
    """
    原子清除全局旧字段, 保存尚未加载安装项需要的迁移基准

    参数:
    - manager: 插件全局配置管理器

    返回:
    - 以工具名标识的旧全局默认值, 仅供旧安装项转换
    """
    path = manager._global_path("friend_manager")
    defaults = dict.fromkeys(FRIEND_SWITCHES.values(), False)
    if not path.is_file():
        return defaults
    from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK
    try:
        with REFERENCE_SCAN_LOCK, FileLock(path.with_name(f".{path.name}.lock")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("好友插件全局配置必须是对象")
            receipt = raw.get(GLOBAL_RECEIPT)
            if receipt is not None:
                if not isinstance(receipt, dict) or receipt.get("version") != 1 or set(receipt.get("legacy_defaults", {})) != set(defaults) or any(type(value) is not bool for value in receipt["legacy_defaults"].values()):
                    raise ValueError("好友插件配置迁移记录无效")
                defaults.update(receipt["legacy_defaults"])
            if receipt is not None and not set(FRIEND_SWITCHES) & raw.keys():
                return defaults
            for field, tool in FRIEND_SWITCHES.items():
                if field in raw:
                    defaults[tool] = legacy_bool(raw.pop(field))
            raw[GLOBAL_RECEIPT] = {"version": 1, "legacy_defaults": defaults}
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", suffix=".tmp", delete=False) as output:
                    temporary = Path(output.name)
                    json.dump(raw, output, ensure_ascii=False, indent=2, allow_nan=False)
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            logger.info("[插件迁移] 已清除好友全局重复开关, 保留旧安装项迁移基准")
            return defaults
    except Exception:
        logger.error("[插件迁移] 好友全局配置迁移失败, 原配置保留以便重试\n" + traceback.format_exc())
        raise


def migrate_friend_switch_specs(value: list[Any], manager: Any) -> list[Any]:
    """
    合并旧工具状态与最终功能开关, 写入版本避免重复折叠

    参数:
    - value: Chat 或平台 Agent 的插件配置
    - manager: 全局配置管理器

    返回:
    - 清除废弃字段的配置副本, 原工具状态保留为会话覆盖迁移凭据
    """
    result = deepcopy(value)
    defaults: dict[str, bool] | None = None
    for index, raw in enumerate(result):
        if raw == "friend_manager":
            raw = {"name": "friend_manager"}
            result[index] = raw
        if not isinstance(raw, dict) or raw.get("name") != "friend_manager":
            continue
        item = cast(dict[str, Any], raw)
        version = item.get("config_version", 0)
        if type(version) is not int or version not in {0, 1}:
            raise ValueError("好友插件配置版本无效")
        config = item.setdefault("config", {})
        caps = item.setdefault("capabilities", {})
        if not isinstance(config, dict) or not isinstance(caps, dict) or not isinstance(caps.setdefault("tools", {}), dict):
            raise ValueError("好友插件配置或工具状态无效")
        if version == 1:
            if set(FRIEND_SWITCHES) & config.keys():
                raise ValueError("新版好友配置不能再使用已删除的功能开关")
            continue
        if defaults is None:
            defaults = migrate_friend_globals(manager)
        tools = caps["tools"]
        originals = {}
        for field, tool in FRIEND_SWITCHES.items():
            state = tools.get(tool, True)
            if type(state) is not bool:
                raise ValueError("好友工具状态必须为布尔值")
            originals[tool] = state
            flag_enabled = legacy_bool(config.pop(field)) if field in config else defaults[tool]
            tools[tool] = state and flag_enabled
        item["config_version"] = 1
        item["migration_state"] = {"friend_tools": originals}
    return result


def migrate_friend_overrides(store: Any, session_id: str, spec: Any) -> dict[str, bool]:
    """
    在同一事务中将实例旧开关转换为工具覆盖, 保留原来最高层优先级

    参数:
    - store: 会话配置覆盖存储
    - session_id: 当前会话 ID
    - spec: 已迁移安装规格, 含旧工具状态的迁移凭据

    返回:
    - 当前实例显式工具状态, 旧业务字段不再参与运行授权
    """
    from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK
    namespace = "plugin_capabilities.friend_manager"
    try:
        with REFERENCE_SCAN_LOCK, database_session_lock(store.database, session_id), closing(store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            old_row = connection.execute("SELECT * FROM session_config_overrides WHERE session_id=? AND namespace='plugins.friend_manager'", (session_id,)).fetchone()
            old = json.loads(old_row["config_json"]) if old_row else {}
            new_row = connection.execute("SELECT * FROM session_config_overrides WHERE session_id=? AND namespace=?", (session_id, namespace)).fetchone()
            new = json.loads(new_row["config_json"]) if new_row else {}
            if not isinstance(old, dict) or not isinstance(new, dict) or set(new) - set(FRIEND_SWITCHES.values()) or any(type(state) is not bool for state in new.values()):
                raise ValueError("好友实例配置或工具覆盖无效")
            moved = set(old) & set(FRIEND_SWITCHES)
            if moved:
                originals = spec.migration_state.get("friend_tools", spec.capabilities.get("tools", {}))
                for field in moved:
                    tool = FRIEND_SWITCHES[field]
                    flag_enabled = legacy_bool(old.pop(field))
                    new.setdefault(tool, originals.get(tool, True) and flag_enabled)
                now = time.time()
                connection.execute("INSERT INTO session_config_overrides(session_id,namespace,config_json,schema_version,revision,updated_at) VALUES(?,?,?,1,?,?) "
                                   "ON CONFLICT(session_id,namespace) DO UPDATE SET config_json=excluded.config_json,revision=excluded.revision,updated_at=excluded.updated_at",
                                   (session_id, namespace, json.dumps(new, ensure_ascii=False), int(new_row["revision"]) + 1 if new_row else 1, now))
                connection.execute("UPDATE session_config_overrides SET config_json=?,revision=revision+1,updated_at=? WHERE session_id=? AND namespace='plugins.friend_manager'",
                                   (json.dumps(old, ensure_ascii=False), now, session_id))
            return new
    except Exception:
        logger.error("[插件迁移] 好友实例覆盖迁移失败, 事务已回滚\n" + traceback.format_exc())
        raise
