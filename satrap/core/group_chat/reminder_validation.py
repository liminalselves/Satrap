"""提醒正文与提及对象的公共校验, 各业务与存储边界共用同一规则"""
from __future__ import annotations


class ReminderError(ValueError):
    """携带稳定错误码的提醒拒绝"""

    def __init__(self, code: str, message: str) -> None:
        """
        构造提醒业务错误

        参数:
        - code: 稳定错误码
        - message: 对外说明
        """
        super().__init__(message)
        self.code = code


def validate_reminder_text(value: object) -> str:
    """
    校验提醒正文, 不修改已参与幂等比较的原始文字

    参数:
    - value: 外部提供的正文

    返回:
    - 原始正文, 类型或长度无效时抛出 ReminderError
    """
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2000:
        raise ReminderError("invalid_argument", "提醒正文需要 1 至 2000 字")
    return value


def validate_reminder_mentions(value: object) -> list[str]:
    """
    校验提及列表, 不允许重复成员, 控制字符或全体提及

    参数:
    - value: 外部提供的成员 ID 列表

    返回:
    - 保留原顺序的独立列表, 类型或身份无效时抛出 ReminderError
    """
    if not isinstance(value, list) or len(value) > 10:
        raise ReminderError("invalid_argument", "提及成员需要 0 至 10 个不重复的成员 ID, 不能 @ 全体")
    result: list[str] = []
    for item in value:
        if (not isinstance(item, str) or not item or len(item) > 256 or item == "all"
                or any(ord(char) < 32 for char in item) or item in result):
            raise ReminderError("invalid_argument", "提及成员需要 0 至 10 个不重复的成员 ID, 不能 @ 全体")
        result.append(item)
    return result
