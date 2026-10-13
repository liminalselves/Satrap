"""OneBot 基础参数规则, 协议入口保留各自的归一化与权限边界"""
from __future__ import annotations

import unicodedata

ACTION_TEXT_LIMITS = {"card": 60, "name": 60, "title": 18, "reason": 120, "sub_type": 8, "remark": 60}


def decimal_digits(text: str, *, ascii_only: bool = False) -> bool:
    """
    检查无符号十进制部分, 负号与输入类型由调用方处理

    参数:
    - text: 待核验的数字部分
    - ascii_only: 是否限定 OneBot 协议读取使用的 ASCII 数字

    返回:
    - 非空十进制数字满足当前字符规则时为 True
    """
    return text.isdecimal() and (not ascii_only or text.isascii())


def media_identifier(value: object, label: str) -> str:
    """
    归一化实现提供的媒体标识, 不推断它是路径还是 URL

    参数:
    - value: 图片或语音标识
    - label: 原领域的错误标签

    返回:
    - 有界且不含 Unicode 控制字符的字符串
    """
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label}标识不能为空")
    if len(text) > 512 or any(unicodedata.category(char) == "Cc" for char in text):
        raise ValueError(f"{label}标识超长或含控制字符")
    return text


def normalized_retcode(value: object) -> int | None:
    """
    校验平台错误码, 不把布尔值或数字文字当成整数错误码

    参数:
    - value: 原始错误码

    返回:
    - 32 位有符号整数或 None
    """
    return value if type(value) is int and -(2 ** 31) <= value < 2 ** 31 else None


def boolean_parameter(value: object, error: str) -> bool:
    """
    共用管理动作的严格布尔规则

    参数:
    - value: 原始参数
    - error: 调用边界原有错误文案

    返回:
    - 布尔值, 非布尔类型抛出 ValueError
    """
    if not isinstance(value, bool):
        raise ValueError(error)
    return value


def action_text(text: str, field: str, *, allow_empty: bool = True, error: str | None = None) -> str:
    """
    共用管理动作文字长度, 输入转换和空白处理留在原边界

    参数:
    - text: 已完成当前边界类型检查的文字
    - field: 动作参数名称
    - allow_empty: 是否允许空字符串
    - error: 当前边界的错误文案, 缺省使用参数名称

    返回:
    - 原始文字, 不修改内容
    """
    if len(text) > ACTION_TEXT_LIMITS.get(field, 200) or not allow_empty and not text:
        raise ValueError(error or f"{field} 长度无效")
    return text
