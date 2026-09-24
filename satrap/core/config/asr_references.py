"""ASR 命名配置的引用扫描: 平台 asr_model 绑定, 插件全局配置与会话覆盖

供删除/重命名前检查 (ModelConfigManager 经 asr_in_use_checker 装配),
每条引用带 summary 供错误消息直接展示, kind/字段供结构化返回

扫描完整性: 只有明确不存在且在契约上允许不存在的来源才当作空集合; 已存在但读不出,
解析失败, 数据库出错或结构版本不支持时抛 AsrReferenceScanError, 不能返回"无引用";
扫描与配置写入共用 REFERENCE_SCAN_LOCK, 避免扫描期间新增引用后仍删掉配置
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast
import json
import sqlite3
import threading

from satrap.core.config.document import load_config_document
from satrap.core.log import logger
from satrap.core.storage import CHAT_PLATFORM_ID, LOCAL_PLATFORM_ID, StorageLayout

REFERENCE_SCAN_LOCK = threading.RLock()
"""引用扫描与相关配置写入共用的锁: 扫描期间不允许新增或改写引用来源"""
OVERRIDE_TABLE = "session_config_overrides"
"""会话覆盖表名, 与 session_overrides.ensure_override_tables 保持一致"""


class AsrReferenceScanError(RuntimeError):
    """引用扫描不完整: 调用方必须按"无法确认无引用"处理, 不能据此删除或重命名配置"""

    def __init__(self, reason: str, origin: str) -> None:
        """
        参数:
        - reason: 脱敏原因码 (plugin_meta/plugin_config/override_db/override_json/override_schema)
        - origin: 脱敏定位说明, 只含来源类别与标识, 不含路径全文与正文
        """
        super().__init__(f"引用扫描不完整 ({reason}): {origin}")
        self.reason = reason
        self.origin = origin


def _ref(summary: str, **fields: str) -> dict[str, str]:
    return {"summary": summary, **fields}


def _platform_references(platforms: list[object], config_name: str) -> list[dict[str, str]]:
    """平台实例 settings.asr_model 绑定"""
    references: list[dict[str, str]] = []
    for item in platforms:
        if not isinstance(item, dict):
            continue
        entry = cast(dict[str, Any], item)
        settings = entry.get("settings")
        if not isinstance(settings, dict):
            continue
        if cast(dict[str, Any], settings).get("asr_model") == config_name:
            platform_id = str(entry.get("id") or "")
            references.append(_ref(
                f"平台 {platform_id} 的语音转写绑定 (asr_model)",
                kind="platform", platform_id=platform_id, field="asr_model",
            ))
    return references


def _plugin_dirs(plugins_dir: Path | None) -> list[Path]:
    """枚举插件目录: 不依赖 scan_plugin_dirs 的静默过滤, 元数据损坏的目录必须参与扫描"""
    from satrap.edictum.plugin import PLUGINS_PRESET_DIR, USER_PLUGINS_DIR

    bases = [PLUGINS_PRESET_DIR, Path(plugins_dir) if plugins_dir is not None else USER_PLUGINS_DIR]
    found: list[Path] = []
    for base in bases:
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.is_dir() and (entry / "meta.yaml").is_file():
                found.append(entry)
    return found


def _asr_plugin_fields(plugins_dir: Path | None) -> dict[str, list[str]]:
    """全部已安装插件中 asr 类型的配置字段 (插件名 -> 字段列表); 元数据损坏即扫描不完整"""
    from satrap.edictum.plugin import load_plugin_meta
    from satrap.edictum.plugin_config import parse_config_schema

    fields: dict[str, list[str]] = {}
    for plugin_dir in _plugin_dirs(plugins_dir):
        try:
            meta = load_plugin_meta(plugin_dir)
            schema = parse_config_schema(meta)
        except Exception as error:
            # 读不出的插件声明可能含 asr 字段, 按扫描不完整处理而不是跳过
            raise AsrReferenceScanError(
                "plugin_meta", f"插件目录 {plugin_dir.name} 元数据无法解析 ({type(error).__name__})",
            ) from error
        name = str(meta.get("name") or "").strip()
        if not name:
            raise AsrReferenceScanError("plugin_meta", f"插件目录 {plugin_dir.name} 缺少合法 name")
        matched = [key for key, definition in schema.items() if definition.type == "asr"]
        if matched:
            fields[name] = matched
    return fields


def _plugin_global_references(
    plugin_config_dir: Path | None,
    plugin_fields: dict[str, list[str]],
    config_name: str,
) -> list[dict[str, str]]:
    """插件全局 JSON 配置中 asr 字段的显式引用"""
    from satrap.edictum.plugin_config import CONFIG_DIR

    base = plugin_config_dir or CONFIG_DIR
    references: list[dict[str, str]] = []
    for plugin, fields in plugin_fields.items():
        path = base / f"{plugin}.json"
        if not path.is_file():
            continue
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            # 已存在的全局配置读不出来: 可能含 asr 引用, 不能当没有
            raise AsrReferenceScanError(
                "plugin_config", f"插件 {plugin} 全局配置无法读取 ({type(error).__name__})",
            ) from error
        if not isinstance(raw, dict):
            raise AsrReferenceScanError("plugin_config", f"插件 {plugin} 全局配置不是对象")
        values = cast(dict[str, Any], raw)
        for field in fields:
            if values.get(field) == config_name:
                references.append(_ref(
                    f"插件 {plugin} 全局配置字段 {field}",
                    kind="plugin_global", plugin=plugin, field=field,
                ))
    return references


def _session_override_references(
    layout: StorageLayout,
    platform_ids: list[str],
    plugin_fields: dict[str, list[str]],
    config_name: str,
) -> list[dict[str, str]]:
    """各平台数据库中会话级插件覆盖的 asr 字段引用"""
    if not plugin_fields:
        return []
    references: list[dict[str, str]] = []
    for platform_id in platform_ids:
        database = layout.platform_db(platform_id)
        if not database.is_file():
            continue
        try:
            rows = _read_override_rows(database, platform_id)
        except AsrReferenceScanError:
            raise
        except sqlite3.Error as error:
            # 库存在但读不出: 锁定, 损坏或结构异常都不允许当作"没配置"
            raise AsrReferenceScanError(
                "override_db", f"平台 {platform_id} 覆盖库读取失败 ({type(error).__name__})",
            ) from error
        for session_id, namespace, config_json in rows:
            plugin = str(namespace)[len("plugins."):]
            fields = plugin_fields.get(plugin)
            if not fields:
                continue
            try:
                values: object = json.loads(str(config_json))
            except ValueError as error:
                # 覆盖 JSON 非法: 无法排除它声明了参考的 asr 字段
                raise AsrReferenceScanError(
                    "override_json", f"平台 {platform_id} 会话 {session_id} 的插件 {plugin} 覆盖 JSON 非法",
                ) from error
            if not isinstance(values, dict):
                raise AsrReferenceScanError(
                    "override_json", f"平台 {platform_id} 会话 {session_id} 的插件 {plugin} 覆盖不是对象",
                )
            for field in fields:
                if cast(dict[str, Any], values).get(field) == config_name:
                    references.append(_ref(
                        f"平台 {platform_id} 会话 {session_id} 的插件 {plugin} 覆盖字段 {field}",
                        kind="session_override", platform_id=platform_id, session_id=str(session_id),
                        plugin=plugin, field=field,
                    ))
    return references


def _read_override_rows(database: Path, platform_id: str) -> list[tuple[Any, ...]]:
    """
    读取单个平台库的插件覆盖行

    参数:
    - database: 平台数据库路径
    - platform_id: 平台实例 ID, 仅用于错误定位

    返回:
    - list[tuple]: (session_id, namespace, config_json) 行

    异常:
    - AsrReferenceScanError: 结构版本表明覆盖表应存在却缺失
    - sqlite3.Error: 其余数据库错误由调用方包装
    """
    from satrap.core.config.session_overrides import OVERRIDE_SCHEMA_VERSION

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
    try:
        try:
            return connection.execute(
                f"SELECT session_id, namespace, config_json FROM {OVERRIDE_TABLE} "
                "WHERE substr(namespace, 1, 8) = 'plugins.'",
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error).lower():
                raise
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version >= OVERRIDE_SCHEMA_VERSION:
                # 结构版本已声明覆盖表存在: 缺表属于损坏, 不能解释为没有配置
                raise AsrReferenceScanError(
                    "override_schema", f"平台 {platform_id} 声明覆盖表版本 {version} 但缺少 {OVERRIDE_TABLE}",
                ) from error
            logger.info(f"[ASR引用扫描] 平台 {platform_id} 属版本 {version} 的旧库, 无覆盖表按空集合处理")
            return []
    finally:
        connection.close()


def list_asr_config_references(
    config_name: str,
    *,
    config_path: Path | None = None,
    platforms: list[object] | None = None,
    layout: StorageLayout | None = None,
    plugin_config_dir: Path | None = None,
    plugins_dir: Path | None = None,
) -> list[dict[str, str]]:
    """
    汇总 ASR 配置的全部引用, 供删除/重命名前检查

    参数:
    - config_name: ASR 配置名称
    - config_path: 主配置文档路径 (与 platforms 二选一)
    - platforms: 已加载的平台配置列表, 优先于 config_path
    - layout: 数据布局, 提供时扫描会话覆盖
    - plugin_config_dir: 插件全局配置目录, 缺省为工作目录下默认位置
    - plugins_dir: 用户插件目录, 缺省扫描预设与默认用户目录

    返回:
    - list[dict[str, str]]: 引用清单, 每项含 summary 与 kind/定位字段
    """
    with REFERENCE_SCAN_LOCK:
        if platforms is None:
            document = load_config_document(config_path) if config_path is not None else {}
            raw_platforms = document.get("platforms", [])
            platforms = cast(list[object], raw_platforms) if isinstance(raw_platforms, list) else []
        references = _platform_references(platforms, config_name)
        plugin_fields = _asr_plugin_fields(plugins_dir)
        references.extend(_plugin_global_references(plugin_config_dir, plugin_fields, config_name))
        if layout is not None:
            platform_ids = [
                str(cast(dict[str, Any], item).get("id") or "")
                for item in platforms if isinstance(item, dict)
            ]
            for internal in (CHAT_PLATFORM_ID, LOCAL_PLATFORM_ID):
                if internal not in platform_ids:
                    platform_ids.append(internal)
            references.extend(_session_override_references(layout, platform_ids, plugin_fields, config_name))
        return references
