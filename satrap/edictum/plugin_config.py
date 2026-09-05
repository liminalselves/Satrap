"""
插件配置机制: meta.yaml config_schema 声明 + 实例配置 json 存储 + 两级合成

插件在 meta.yaml 用 config_schema 声明可配项 (键 -> type/default/description):
    config_schema:
      sandbox_root:
        type: path            # string/path/textarea/number/bool/select
        default: ""
        description: "沙箱根目录"

配置存储:
- 全局默认: .satrap/plugin_config/<plugin_name>.json (所有会话共享)
- 会话覆盖: 由调用方 (ChatService) 持有, install_plugin 时经 config 参数传入

合成顺序: schema.default < 全局 json < 会话覆盖
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
import json
import re

from satrap.core.log import logger

CONFIG_DIR = Path(".satrap") / "plugin_config"
"""插件全局配置目录 (相对工作目录)"""

_FIELD_TYPES = ("string", "path", "textarea", "number", "bool", "select")
"""支持的配置字段类型"""

_PLUGIN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
"""插件配置文件使用的稳定名称格式"""


@dataclass
class ConfigField:
    """单个配置项声明 (来自 meta.yaml config_schema)"""

    name: str
    type: str = "string"
    default: Any = None
    description: str = ""
    options: list[str] = field(default_factory=lambda: list[str]())
    """select 类型的可选值"""

    def validate(self, value: Any) -> Any:
        """
        校验并归一化一个值, 非法时回退默认值并警告

        参数:
        - value: 输入值

        返回:
        - Any: 校验并归一化一个值, 非法时回退默认值并警告
        """
        if value is None:
            return self.default
        try:
            if self.type == "bool":
                return bool(value)
            if self.type == "number":
                if isinstance(value, bool):
                    return self.default
                if isinstance(value, (int, float)):
                    return value
                return float(str(value))
            if self.type == "select":
                text = str(value)
                if self.options and text not in self.options:
                    logger.warning(f"[插件配置] {self.name} 值 {text} 不在可选 {self.options}, 回退默认")
                    return self.default
                return text
            # string / path / textarea 统一按字符串
            return str(value)
        except (TypeError, ValueError):
            logger.warning(f"[插件配置] {self.name} 值 {value!r} 非法, 回退默认 {self.default!r}")
            return self.default


def parse_config_schema(meta: dict[str, Any]) -> dict[str, ConfigField]:
    """
    从 meta.yaml 解析 config_schema (键 -> ConfigField)

    参数:
    - meta: 元数据

    非字典的 config_schema 跳过并警告; 单个字段非字典用默认 string 类型

    返回:
    - dict[str, ConfigField]: 从 meta.yaml 解析 config_schema (键 -> ConfigField)
    """
    raw = meta.get("config_schema")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning(f"[插件配置] config_schema 应为字典, 已跳过: {raw!r}")
        return {}
    fields: dict[str, ConfigField] = {}
    for key, spec in cast(dict[Any, Any], raw).items():
        name = str(key)
        if isinstance(spec, dict):
            spec_dict = cast(dict[str, Any], spec)
            ftype = str(spec_dict.get("type") or "string")
            if ftype not in _FIELD_TYPES:
                logger.warning(f"[插件配置] {name} 类型 {ftype} 非法, 按 string 处理")
                ftype = "string"
            options_raw = spec_dict.get("options")
            options = [str(o) for o in cast(list[Any], options_raw)] if isinstance(options_raw, list) else []
            fields[name] = ConfigField(
                name=name,
                type=ftype,
                default=spec_dict.get("default"),
                description=str(spec_dict.get("description") or ""),
                options=options,
            )
        else:
            fields[name] = ConfigField(name=name, type="string", default=spec)
            # 简写: key: 默认值 (类型按 string)
    return fields


class PluginConfigManager:
    """
    插件配置管理器: 全局 json 读写 + 两级合成

    全局配置存 .satrap/plugin_config/<name>.json; 会话覆盖由调用方传入
    """

    def __init__(self, config_dir: str | Path | None = None) -> None:
        """
        初始化 PluginConfigManager

        参数:
        - config_dir: 配置dir
        """
        self._dir = Path(config_dir) if config_dir is not None else CONFIG_DIR

    def _global_path(self, name: str) -> Path:
        """
        校验插件名称并返回配置目录内的真实路径

        参数:
        - name: 插件稳定名称

        返回:
        - Path: 严格位于全局配置目录内的 JSON 路径
        """
        if _PLUGIN_NAME_RE.fullmatch(name) is None:
            raise ValueError(f"非法插件名称: {name}")
        root = self._dir.resolve()
        path = (root / f"{name}.json").resolve()
        if path.parent != root:
            raise ValueError(f"非法插件配置路径: {name}")
        return path

    def load_global(self, name: str, schema: dict[str, ConfigField]) -> dict[str, Any]:
        """
        读全局 json 并按 schema 校验 + 补默认 (无文件时全默认)

        参数:
        - name: 名称
        - schema: 数据结构定义

        返回:
        - dict[str, Any]: 读全局 json 并按 schema 校验 + 补默认 (无文件时全默认)
        """
        merged = {key: fld.default for key, fld in schema.items()}
        path = self._global_path(name)
        if not path.is_file():
            return merged
        try:
            with open(path, encoding="utf-8") as f:
                raw: Any = json.load(f)
        except Exception as e:
            logger.error(f"[插件配置] 读取 {name} 全局配置失败: {e}")
            return merged
        if not isinstance(raw, dict):
            logger.warning(f"[插件配置] {name} 全局配置应为字典, 已忽略")
            return merged
        for key, value in cast(dict[str, Any], raw).items():
            fld = schema.get(key)
            if fld is None:
                logger.warning(f"[插件配置] {name} 配置键 {key} 未在 schema 声明, 忽略")
                continue
            merged[key] = fld.validate(value)
        return merged

    def save_global(self, name: str, schema: dict[str, ConfigField], config: dict[str, Any]) -> dict[str, Any]:
        """
        校验并写全局 json, 返回合成后的生效配置

        参数:
        - name: 名称
        - schema: 数据结构定义
        - config: 配置信息

        返回:
        - dict[str, Any]: 合成后的生效配置
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        cleaned: dict[str, Any] = {}
        for key, value in config.items():
            fld = schema.get(key)
            if fld is None:
                logger.warning(f"[插件配置] {name} 配置键 {key} 未在 schema 声明, 忽略")
                continue
            cleaned[key] = fld.validate(value)
        path = self._global_path(name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
        return self.load_global(name, schema)

    def resolve(
        self,
        name: str,
        schema: dict[str, ConfigField],
        session_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        合成最终配置: 全局 (含默认) + 会话覆盖

        参数:
        - name: 名称
        - schema: 数据结构定义
        - session_override: 会话override

        返回:
        - dict[str, Any]: 合成最终配置: 全局 (含默认) + 会话覆盖
        """
        resolved = self.load_global(name, schema)
        if session_override:
            for key, value in session_override.items():
                fld = schema.get(key)
                if fld is None:
                    logger.warning(f"[插件配置] {name} 会话覆盖键 {key} 未在 schema 声明, 忽略")
                    continue
                resolved[key] = fld.validate(value)
        return resolved


def schema_to_payload(schema: dict[str, ConfigField]) -> dict[str, dict[str, Any]]:
    """
    把 schema 转为可序列化 payload (供前端渲染表单)

    参数:
    - schema: 数据结构定义

    返回:
    - dict[str, dict[str, Any]]: 把 schema 转为可序列化 payload (供前端渲染表单)
    """
    return {
        key: {
            "type": fld.type,
            "default": fld.default,
            "description": fld.description,
            "options": fld.options,
        }
        for key, fld in schema.items()
    }
