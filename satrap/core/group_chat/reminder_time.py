"""提醒时间解析, 无时区输入采用后端本地规则并拒绝夏令时歧义"""
from __future__ import annotations

from datetime import datetime
from typing import Any
import math
import time


def resolve_reminder_time(values: dict[str, Any], accepted_at: float) -> float:
    """
    在接受请求时冻结一次性提醒的 UTC 截止时间

    参数:
    - values: due_at 或 after_seconds 二选一
    - accepted_at: 后端接受请求的 UTC 秒数, 不使用平台消息时间

    返回:
    - UTC 秒数, 格式错误, 夏令时歧义或不存在的本地时间均拒绝
    """
    if set(values) not in ({"due_at"}, {"after_seconds"}):
        raise ValueError("请填写具体日期时间或等待秒数, 两者只能选一个")
    if "after_seconds" in values:
        seconds = values["after_seconds"]
        if type(seconds) is not int or not 10 <= seconds <= 31536000:
            raise ValueError("等待秒数需要是 10 至 31536000 的整数")
        return accepted_at + seconds
    value = values["due_at"]
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or "T" not in value and " " not in value:
        raise ValueError("请填写日期和时间, 例如 2026-10-04T09:00:00")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        timestamp = parsed.timestamp()
    else:
        fields = (parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute, parsed.second)
        candidates: set[float] = set()
        for is_dst in (-1, 0, 1):
            try:
                possible = time.mktime((*fields, 0, 0, is_dst))
                if tuple(time.localtime(possible)[:6]) == fields:
                    candidates.add(possible + parsed.microsecond / 1000000)
            except (OverflowError, OSError, ValueError):
                continue
        if not candidates:
            raise ValueError("该本地时间不存在, 请重新选择时间")
        if len(candidates) != 1:
            choices = ", ".join(datetime.fromtimestamp(item).astimezone().isoformat() for item in sorted(candidates))
            raise ValueError(f"该本地时间有夏令时歧义, 请明确选择其中一个带偏移的时间: {choices}")
        timestamp = candidates.pop()
    if not math.isfinite(timestamp) or not accepted_at + 10 <= timestamp <= accepted_at + 31536000:
        raise ValueError("执行时间需要在至少 10 秒后, 且不超过一年")
    return timestamp
