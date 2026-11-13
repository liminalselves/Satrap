"""插件配置字段与宿主状态契约。"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, TypedDict, cast
import math
import re

_TIME_RE = re.compile(r"^(?:(?:[01]\d|2[0-3]):[0-5]\d|24:00)$")

class PluginHostState(TypedDict):
    """宿主管理状态, 与插件作者的参数结构分开保存"""

    execution_enabled: bool
    """宿主是否允许插件执行, 状态损坏时按禁用处理"""
    revision: int
    """宿主状态的递增修订号, 尚无显式状态时为 0"""
    updated_at: float | None
    """插件配置更新的 Unix 时间戳, 单位秒, 未记录时为 None"""
    execution_updated_at: float | None
    """执行开关更新的 Unix 时间戳, 单位秒, 未设置时为 None"""


@dataclass
class ConfigField:
    """单个配置项声明 (来自 meta.yaml config_schema)"""

    name: str
    """meta.yaml 中配置项的键, 用于合成配置和定位校验错误"""
    type: str = "string"
    """配置值的声明类型, 默认 string, 决定表单与校验方式"""
    default: Any = None
    """未配置或宽松校验失败时回退的值, 未声明时为 None"""
    description: str = ""
    """供配置界面展示的用途说明, 未声明时为空串"""
    options: list[str] = field(default_factory=lambda: list[str]())
    """select 类型的可选值, 空列表表示不限制选项"""
    required: bool = False
    """是否要求提供非空配置, 默认 False, 构造插件资源时检查"""
    nullable: bool = False
    """是否允许显式 null, 默认 False, 宽松校验时 null 回退默认值"""
    session_overridable: bool = True
    """是否允许会话级配置覆盖该项, 默认允许"""
    minimum: float | None = None
    """严格数字校验的包含性下限, None 表示不限制下限"""
    maximum: float | None = None
    """严格数字校验的包含性上限, None 表示不限制上限"""
    integer: bool = False
    """严格数字校验是否要求整数并归一化为 int, 默认 False"""
    scope: str = ""
    """元数据声明的配置作用域提示, 空串表示未指定"""
    label: str = ""
    """配置界面的显示标签, 空串表示未指定"""
    option_labels: dict[str, str] = field(default_factory=dict[str, str])
    """选项值到显示标签的映射, 空映射表示没有自定义标签"""
    strict: bool = False
    """是否对显式值启用严格校验而非对非法输入回退默认值, 默认 False"""
    group: str = ""
    """属性面板分组名, 空串表示不分组"""
    visible_when: dict[str, Any | list[Any]] | None = None
    """显示条件 {字段: 值 或 [值, ...]}: 全部满足才显示, 只影响前端展示, 后端照常校验"""

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
            if self.type == "time" and _TIME_RE.fullmatch(value) is None:
                raise ValueError(f"{self.name} 必须是 HH:MM 格式 (00:00-24:00)")
        return value

    def validate(self, value: Any) -> Any:
        """
        校验并归一化一个值, 非法时回退默认值并警告

        参数:
        - value: 输入值

        返回:
        - Any: 校验并归一化一个值, 非法时回退默认值并警告
        """
        if self.strict:
            return self.validate_strict(value)
        if value is None:
            if self.nullable:
                return None
            return self.default
        if self.type in {"llm", "embed", "rerank", "asr", "img", "knowledge_base", "knowledge_bases"}:
            return self.validate_strict(value)
        from satrap.core.log import logger

        try:
            if self.type == "bool":
                # 显式映射, 避免 bool("false") == True 的 fail-open
                if isinstance(value, bool):
                    return value
                if isinstance(value, (int, float)) and value in (0, 1):
                    return bool(value)
                if isinstance(value, str):
                    mapped = {"true": True, "1": True, "yes": True, "on": True,
                              "false": False, "0": False, "no": False, "off": False}.get(value.strip().lower())
                    if mapped is not None:
                        return mapped
                logger.warning(f"[插件配置] {self.name} 布尔值 {value!r} 无法识别, 回退默认 {self.default!r}")
                return self.default
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
            if self.type == "time":
                text = str(value).strip()
                if _TIME_RE.fullmatch(text) is None:
                    logger.warning(f"[插件配置] {self.name} 值 {value!r} 不是 HH:MM, 回退默认 {self.default!r}")
                    return self.default
                return text
            # string / path / textarea 统一按字符串
            return str(value)
        except (TypeError, ValueError):
            logger.warning(f"[插件配置] {self.name} 值 {value!r} 非法, 回退默认 {self.default!r}")
            return self.default


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
            **({"label": fld.label} if fld.label else {}),
            **({"option_labels": fld.option_labels} if fld.option_labels else {}),
            **({"group": fld.group} if fld.group else {}),
            **({"visible_when": dict(fld.visible_when)} if fld.visible_when else {}),
        }
        for key, fld in schema.items()
    }
