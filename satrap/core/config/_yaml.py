"""PyYAML 的类型化边界包装

PyYAML 无类型标注, safe_load/safe_dump 的成员访问产生 Unknown,
集中在本模块断言, 调用方获得可核查的类型
"""
from __future__ import annotations

from typing import Any, TextIO, cast

import yaml


def safe_yaml_load(stream: str | TextIO) -> object:
    """
    解析 YAML 文本或文本流

    参数:
    - stream: YAML 文本或文本文件对象

    返回:
    - object: 解析结果, 结构由调用方校验 (根节点可能为 None)
    """
    return cast(object, cast(Any, yaml).safe_load(stream))


def safe_yaml_dump(data: Any) -> str:
    """
    将数据序列化为 YAML 文本

    参数:
    - data: 待序列化数据

    返回:
    - str: YAML 文本 (不换行风格, 允许非 ASCII 字符, 保持键顺序)
    """
    return cast(str, cast(Any, yaml).safe_dump(data, allow_unicode=True, sort_keys=False))
