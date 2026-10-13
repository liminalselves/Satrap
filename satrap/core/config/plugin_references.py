"""插件资源字段的严格枚举, 调用方保留各自的扫描错误与名称语义"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from satrap.edictum.plugin_config import parse_config_schema
from satrap.edictum.plugin import PLUGINS_PRESET_DIR, USER_PLUGINS_DIR, load_plugin_meta


def plugin_directories(plugins_dir: Path | None) -> list[Path]:
    """
    枚举带元数据的插件目录, 不静默忽略损坏的元数据

    参数:
    - plugins_dir: 用户插件目录, None 使用默认位置

    返回:
    - 预设目录后接用户目录, 各目录内保持排序
    """
    bases = [PLUGINS_PRESET_DIR, Path(plugins_dir) if plugins_dir is not None else USER_PLUGINS_DIR]
    return [entry for base in bases if base.is_dir() for entry in sorted(base.iterdir())
            if entry.is_dir() and (entry / "meta.yaml").is_file()]


def reference_fields(target: str, plugins_dir: Path | None, *,
                     failure: Callable[[Path, Exception | None], Exception],
                     strip_names: bool, include_empty: bool) -> dict[str, list[str]]:
    """
    读取全部插件声明并按资源类型筛选字段

    参数:
    - target: llm 或 asr 等声明字段类型
    - plugins_dir: 用户插件目录
    - failure: 领域错误工厂, None 原因表示名称缺失
    - strip_names: 是否去除元数据名称的首尾空白
    - include_empty: 是否保留没有匹配字段的插件项

    返回:
    - 插件名称到资源字段名的映射, 来源不完整时抛出领域错误
    """
    result: dict[str, list[str]] = {}
    for directory in plugin_directories(plugins_dir):
        try:
            meta = load_plugin_meta(directory)
            schema = parse_config_schema(meta)
        except Exception as error:
            raise failure(directory, error) from error
        name = str(meta.get("name") or "")
        if strip_names:
            name = name.strip()
        if not name:
            raise failure(directory, None)
        matched = [key for key, field in schema.items() if field.type == target]
        if matched or include_empty:
            result[name] = matched
    return result
