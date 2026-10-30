"""PyYAML 的类型化边界包装

PyYAML 无类型标注, safe_load/safe_dump 的成员访问产生 Unknown,
集中在本模块断言, 调用方获得可核查的类型
"""
from __future__ import annotations

from typing import Any, TextIO, TypedDict, cast
import yaml

_SAFE_LOADER: type[yaml.SafeLoader] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
# libyaml 可用时用 C 解析器, 配置文档较大时快一个量级; 语义与 SafeLoader 一致


class YamlDiagnostic(TypedDict):
    """生产 YAML 解析器或配置校验器给出的诊断, 行列从 1 开始"""

    path: str | None
    line: int | None
    column: int | None
    message: str
    code: str


def parse_yaml_diagnostic(text: str) -> tuple[object, YamlDiagnostic | None]:
    """
    使用生产解析器读取文本并保留真实语法位置

    参数:
    - text: 管理员提交的原始 YAML

    返回:
    - 解析对象及可空诊断, 不根据字符串搜索伪造错误位置
    """
    try:
        return safe_yaml_load(text), None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line, column = getattr(mark, "line", None), getattr(mark, "column", None)
        message = getattr(error, "problem", None)
        return None, YamlDiagnostic(path=None, line=line + 1 if isinstance(line, int) else None,
                                    column=column + 1 if isinstance(column, int) else None,
                                    message=str(message or "YAML 语法无效")[:2000], code="yaml_syntax_error")


def safe_yaml_load(stream: str | TextIO) -> object:
    """
    解析 YAML 文本或文本流

    参数:
    - stream: YAML 文本或文本文件对象

    返回:
    - object: 解析结果, 结构由调用方校验 (根节点可能为 None)
    """
    return cast(object, cast(Any, yaml).load(stream, Loader=_SAFE_LOADER))


def safe_yaml_dump(data: Any) -> str:
    """
    将数据序列化为 YAML 文本

    参数:
    - data: 待序列化数据

    返回:
    - str: YAML 文本 (不换行风格, 允许非 ASCII 字符, 保持键顺序)
    """
    return cast(str, cast(Any, yaml).safe_dump(data, allow_unicode=True, sort_keys=False))
