"""逐群绑定与覆盖的严格资源引用扫描"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK, _plugin_dirs
from satrap.core.config.platform_schema import PLATFORM_SCHEMA_VERSION
from satrap.core.storage import StorageLayout

from satrap.edictum.plugin import load_plugin_meta
from satrap.edictum.plugin_config import parse_config_schema


class GroupReferenceScanError(RuntimeError):
    """群配置读取不完整, 调用方不得据此删除资源"""


def _plugin_fields(target: str, plugins_dir: Path | None) -> dict[str, list[str]]:
    """从全部插件声明中严格识别资源引用字段"""
    if target not in {"llm", "asr"}:
        return {}

    result: dict[str, list[str]] = {}
    for directory in _plugin_dirs(plugins_dir):
        try:
            meta = load_plugin_meta(directory)
            schema = parse_config_schema(meta)
            plugin = str(meta.get("name") or "")
            if not plugin:
                raise ValueError("插件缺少名称")
            result[plugin] = [key for key, field in schema.items() if field.type == target]
        except Exception as error:
            raise GroupReferenceScanError(f"插件 {directory.name} 元数据无法解析") from error
    return result


def list_group_references(
    target: str, name: str, *, layout: StorageLayout,
    platform_ids: list[str], plugins_dir: Path | None = None,
) -> list[dict[str, str]]:
    """扫描平台群配置, 数据库损坏时拒绝判定为无引用"""
    if target not in {"session_class", "edictum", "llm", "asr"}:
        raise ValueError("不支持的引用目标")
    references: list[dict[str, str]] = []
    with REFERENCE_SCAN_LOCK:
        fields = _plugin_fields(target, plugins_dir)
        for platform_id in platform_ids:
            database = layout.platform_db(platform_id)
            if not database.is_file():
                continue
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
            try:
                try:
                    rows = connection.execute(
                        "SELECT self_id, group_id, config_json FROM group_configs",
                    ).fetchall()
                except sqlite3.OperationalError as error:
                    if "no such table" not in str(error).lower():
                        raise
                    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if version >= PLATFORM_SCHEMA_VERSION:
                        raise GroupReferenceScanError(f"平台 {platform_id} 群配置表缺失") from error
                    continue
                for self_id, group_id, encoded in rows:
                    location = f"平台 {platform_id} 账号 {self_id} 群 {group_id}"
                    try:
                        payload: object = json.loads(str(encoded))
                        if not isinstance(payload, dict):
                            raise ValueError("群配置不是对象")
                        session = cast(dict[str, Any], payload).get("session", {})
                        if not isinstance(session, dict):
                            raise ValueError("会话配置不是对象")
                    except (ValueError, TypeError) as error:
                        raise GroupReferenceScanError(f"{location} 配置无法解析") from error
                    binding = session.get("binding", {})
                    if (target in {"session_class", "edictum"} and isinstance(binding, dict)
                            and binding.get("mode") == "value" and isinstance(binding.get("value"), dict)
                            and binding["value"].get("provider") == target
                            and binding["value"].get("config_name") == name):
                        references.append({"summary": f"{location} 的会话绑定", "kind": "group_binding",
                                           "platform_id": platform_id, "self_id": str(self_id), "group_id": str(group_id)})
                    model = session.get("model", {})
                    if (target == "llm" and isinstance(model, dict) and model.get("mode") == "value"
                            and model.get("value") == name):
                        references.append({"summary": f"{location} 的模型覆盖", "kind": "group_model",
                                           "platform_id": platform_id, "self_id": str(self_id), "group_id": str(group_id)})
                    plugins = session.get("plugins", {})
                    if not fields or not isinstance(plugins, dict) or plugins.get("mode") != "value":
                        continue
                    items = plugins.get("value")
                    if not isinstance(items, list):
                        raise GroupReferenceScanError(f"{location} 插件覆盖不是数组")
                    for item in items:
                        if not isinstance(item, dict):
                            raise GroupReferenceScanError(f"{location} 插件覆盖项无效")
                        plugin = item.get("name")
                        config = item.get("config", {})
                        if not isinstance(config, dict):
                            raise GroupReferenceScanError(f"{location} 插件配置无效")
                        for field in fields.get(str(plugin), []):
                            if config.get(field) == name:
                                references.append({"summary": f"{location} 插件 {plugin} 的 {field} 字段",
                                                   "kind": "group_plugin", "platform_id": platform_id,
                                                   "self_id": str(self_id), "group_id": str(group_id)})
            except sqlite3.Error as error:
                raise GroupReferenceScanError(f"平台 {platform_id} 群配置数据库无法读取") from error
            finally:
                connection.close()
    return references
