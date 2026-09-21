"""群与本机时段的唤醒策略覆盖, 不改变访问范围或会话归属"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from copy import deepcopy
from typing import Any, cast
import re


AUTOMATIC_KEYS = frozenset({
    "wake_mode", "wake_message_threshold", "wake_cooldown", "wake_score_threshold", "wake_max_wait",
    "wake_question_weight", "wake_address_weight", "wake_backlog_weight", "wake_reply_penalty",
})
GROUP_KEYS = AUTOMATIC_KEYS | {"wake_words", "wake_aliases", "reply_with_quote", "reply_with_mention", "quote_lookup"}


def _minute(value: object) -> int:
    """
    解析严格的本地时分格式

    参数:
    - value: HH:MM 字符串

    返回:
    - int: 当日分钟数, 非法时抛出 ValueError
    """
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("时段边界必须为 HH:MM")
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def validate_wake_overrides(settings: Mapping[str, object]) -> None:
    """
    校验有限的群覆盖与时段列表

    参数:
    - settings: 完整平台策略
    """
    from satrap.core.config.platform_policy import normalize_group_whitelist, normalize_wake_words, validate_wake_policy

    groups = settings.get("wake_group_overrides", {})
    if not isinstance(groups, dict) or len(cast(dict[object, object], groups)) > 512:
        raise ValueError("wake_group_overrides 必须是最多 512 项的群 ID 对象")
    for group, override in cast(dict[object, object], groups).items():
        if not isinstance(group, str) or normalize_group_whitelist([group]) != [group]:
            raise ValueError("群覆盖的键必须是规范的正整数群 ID")
        if not isinstance(override, dict) or not set(cast(dict[object, object], override)) <= GROUP_KEYS:
            raise ValueError("群覆盖只允许唤醒规则字段")
        validate_wake_policy(cast(dict[str, object], override))
        for key in ("wake_words", "wake_aliases"):
            if key in override:
                normalize_wake_words(cast(dict[str, object], override)[key])
    periods = settings.get("wake_time_rules", [])
    if not isinstance(periods, list) or len(cast(list[object], periods)) > 32:
        raise ValueError("wake_time_rules 必须是最多 32 项的时段列表")
    for raw_period in cast(list[object], periods):
        period = cast(dict[str, object], raw_period)
        if not isinstance(period, dict) or set(period) != {"start", "end", "settings"}:
            raise ValueError("时段规则必须包含 start, end, settings")
        if _minute(period["start"]) == _minute(period["end"]):
            raise ValueError("时段起止不能相同")
        override = period["settings"]
        if not isinstance(override, dict) or not set(cast(dict[object, object], override)) <= AUTOMATIC_KEYS:
            raise ValueError("时段只能覆盖自动参与参数")
        validate_wake_policy(cast(dict[str, object], override))


def resolve_wake_settings(settings: Mapping[str, Any], group_id: str, now: datetime | None = None) -> dict[str, Any]:
    """
    按平台, 时段, 群的顺序冻结有效配置

    参数:
    - settings: 已校验的平台策略
    - group_id: 目标群, 空字符串表示不应用覆盖
    - now: 本机当地时间, 默认读取系统时间

    返回:
    - dict[str, Any]: 独立配置副本, 重叠时段以列表后项为准
    """
    resolved = deepcopy(dict(settings))
    if not group_id:
        return resolved
    now = datetime.now().astimezone() if now is None else now
    minute = now.hour * 60 + now.minute
    for period in settings.get("wake_time_rules", []):
        start, end = _minute(period["start"]), _minute(period["end"])
        matches = start <= minute < end if start < end else minute >= start or minute < end
        if matches:
            resolved.update(deepcopy(period["settings"]))
    resolved.update(deepcopy(settings.get("wake_group_overrides", {}).get(group_id, {})))
    return resolved
