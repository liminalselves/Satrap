"""
插件配置机制: YAML 声明, 全局 JSON 与命名配置, SQLite 会话覆盖

插件在 meta.yaml 用 config_schema 声明可配项 (键 -> type/default/description):
    config_schema:
      sandbox_root:
        type: path            # string/path/textarea/number/bool/select
        default: ""
        description: "沙箱根目录"

配置存储:
- 全局默认: .satrap/plugin_config/<plugin_name>.json (所有会话共享)
- 会话覆盖: platform.db 按会话和配置域保存显式字段, 安装时读取

合成顺序: schema.default < 全局 JSON < Edictum 命名配置 < 会话覆盖
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
import json
import tempfile
import math
import re

from satrap.core.log import logger

CONFIG_DIR = Path(".satrap") / "plugin_config"
"""插件全局配置目录 (相对工作目录)"""

_FIELD_TYPES = ("string", "path", "textarea", "number", "bool", "select", "llm", "embed", "rerank", "knowledge_base", "knowledge_bases")
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
    required: bool = False
    nullable: bool = False
    session_overridable: bool = True
    minimum: float | None = None
    maximum: float | None = None
    integer: bool = False
    scope: str = ""

    def validate_strict(self, value: Any) -> Any:
        """管理接口严格校验显式值, 不以默认值吞掉非法输入"""
        if value is None:
            if self.nullable:
                return None
            raise ValueError(f"{self.name} 不允许 null")
        if self.type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{self.name} 必须是布尔值")
        elif self.type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{self.name} 必须是有限数字")
            if self.integer and int(value) != value:
                raise ValueError(f"{self.name} 必须是整数")
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"{self.name} 不能小于 {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"{self.name} 不能大于 {self.maximum}")
            if self.integer:
                value = int(value)
        elif self.type == "knowledge_bases":
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in cast(list[object], value)):
                raise ValueError(f"{self.name} 必须是知识库 ID 数组")
            value = list(dict.fromkeys(cast(list[str], value)))
        else:
            if not isinstance(value, str):
                raise ValueError(f"{self.name} 必须是字符串")
            if self.type == "select" and self.options and value not in self.options:
                raise ValueError(f"{self.name} 不在可选值中")
        return value

    def validate(self, value: Any) -> Any:
        """
        校验并归一化一个值, 非法时回退默认值并警告

        参数:
        - value: 输入值

        返回:
        - Any: 校验并归一化一个值, 非法时回退默认值并警告
        """
        if value is None:
            if self.nullable:
                return None
            return self.default
        if self.type in {"llm", "embed", "rerank", "knowledge_base", "knowledge_bases"}:
            return self.validate_strict(value)
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
                required=bool(spec_dict.get("required", False)),
                nullable=bool(spec_dict.get("nullable", False)),
                session_overridable=bool(spec_dict.get("session_overridable", True)),
                minimum=spec_dict.get("minimum"),
                maximum=spec_dict.get("maximum"),
                integer=bool(spec_dict.get("integer", False)),
                scope=str(spec_dict.get("scope") or ""),
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

    def load_global_explicit(self, name: str, schema: dict[str, ConfigField]) -> dict[str, Any]:
        """仅读取全局显式字段, 用于区分默认值和全局值来源"""
        path = self._global_path(name)
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"插件 {name} 全局配置必须是对象")
        return {key: schema[key].validate(value) for key, value in cast(dict[str, Any], raw).items() if key in schema}

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
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self._dir, suffix=".tmp", encoding="utf-8", delete=False) as file:
                temporary = Path(file.name)
                json.dump(cleaned, file, ensure_ascii=False, indent=2, allow_nan=False)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
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
            "required": fld.required,
            "nullable": fld.nullable,
            "session_overridable": fld.session_overridable,
            "minimum": fld.minimum,
            "maximum": fld.maximum,
            "integer": fld.integer,
            "scope": fld.scope,
        }
        for key, fld in schema.items()
    }


def validate_config_values(
    schema: dict[str, ConfigField], values: dict[str, Any], *, session_override: bool = False,
) -> dict[str, Any]:
    """校验声明字段, 显式覆盖不能修改禁止会话修改的字段"""
    if not isinstance(values, dict):
        raise ValueError("插件配置必须是对象")
    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        definition = schema.get(key)
        if definition is None:
            raise ValueError(f"未声明配置项: {key}")
        if session_override and not definition.session_overridable:
            raise ValueError(f"配置项不允许会话覆盖: {key}")
        cleaned[key] = definition.validate_strict(value)
    return cleaned
