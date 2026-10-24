"""平台无关的有限文字与超时数值规则, 领域边界保留自己的空白语义"""
from __future__ import annotations

from typing import TypeGuard
import math


def positive_timeout(value: object, name: str) -> float:
    """
    校验超时为有限正秒数, 不将布尔值当作数字

    参数:
    - value: 配置中的秒数
    - name: 用于错误提示的字段名

    返回:
    - 规范化浮点秒数, 无效值抛出 ValueError
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} 必须是有限正秒数")
    try:
        seconds = float(value)
    except ValueError as error:
        raise ValueError(f"{name} 必须是有限正秒数") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} 必须是有限正秒数")
    return seconds


def is_bounded_text(value: object, *, maximum: int = 256, non_blank: bool = False,
                    trimmed: bool = False, measure_stripped: bool = False) -> TypeGuard[str]:
    """
    检查字符串类型, 长度与 ASCII 控制字符, 不隐式转换身份

    参数:
    - value: 外部提供的字段
    - maximum: 最大字符数
    - non_blank: 是否拒绝只含空白的字段
    - trimmed: 是否要求原值没有首尾空白
    - measure_stripped: 是否按去除首尾空白后的长度计数

    返回:
    - 字段符合当前领域选择的规则时为 True
    """
    if not isinstance(value, str):
        return False
    clean = value.strip() if non_blank or trimmed or measure_stripped else value
    counted = clean if measure_stripped else value
    return (0 < len(counted) <= maximum and not any(ord(char) < 32 for char in value)
            and (not non_blank or bool(clean)) and (not trimmed or value == clean))
