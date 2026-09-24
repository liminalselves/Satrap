"""群与本机时段的唤醒策略覆盖, 不改变访问范围或会话归属"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from copy import deepcopy
from typing import Any, cast
import re

from satrap.core.config.platform_policy import (
    fields_with_scope,
    normalize_group_whitelist,
    normalize_wake_words,
    validate_wake_policy,
)

AUTOMATIC_KEYS = fields_with_scope("time")
"""时段与群覆盖都可用的自动参与参数, 由 POLICY_FIELD_CONTRACT 的 scope 派生"""

GROUP_KEYS = fields_with_scope("time", "group")
"""群覆盖可用字段: 自动参与参数加回复装饰与回源开关"""


SOURCE_PLATFORM = "platform"
"""字段来源: 平台配置本身"""
SOURCE_TIME_RULE = "time_rule"
"""字段来源: 本机时段规则"""
SOURCE_GROUP = "group"
"""字段来源: 群覆盖"""


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


def _hhmm(minute: int) -> str:
    """当日分钟数转 HH:MM, 供来源说明展示"""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def validate_wake_overrides(settings: Mapping[str, object]) -> None:
    """
    校验有限的群覆盖与时段列表

    参数:
    - settings: 完整平台策略
    """
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


class _PreparedPolicy:
    """按 settings 对象预解析的策略视图, 配置对象替换后自动失效"""

    __slots__ = ("base", "periods", "groups")

    def __init__(self, settings: Mapping[str, Any]) -> None:
        self.base: dict[str, Any] = dict(settings)
        self.periods: list[tuple[int, int, dict[str, Any], int, str]] = []
        rules = settings.get("wake_time_rules", [])
        for index, period in enumerate(cast(list[Any], rules) if isinstance(rules, list) else []):
            if not isinstance(period, dict):
                continue
            entry = cast(dict[str, Any], period)
            try:
                start, end = _minute(entry["start"]), _minute(entry["end"])
            except (KeyError, ValueError, TypeError):
                continue
            overrides = entry.get("settings")
            if isinstance(overrides, dict):
                label = f"{_hhmm(start)}-{_hhmm(end)} (第 {index + 1} 条时段规则)"
                self.periods.append((start, end, dict(cast(dict[str, Any], overrides)), index, label))
        groups = settings.get("wake_group_overrides", {})
        self.groups: dict[str, dict[str, Any]] = {
            str(group): dict(cast(dict[str, Any], override))
            for group, override in cast(dict[Any, Any], groups).items() if isinstance(override, dict)
        } if isinstance(groups, dict) else {}


_PREPARED: dict[int, tuple[Any, _PreparedPolicy]] = {}
_PREPARED_LIMIT = 64


def _prepared(settings: Mapping[str, Any]) -> _PreparedPolicy:
    """
    取得 settings 对象的预解析视图

    参数:
    - settings: 平台策略, 热更新时整体替换为新对象, 因此按对象身份缓存

    返回:
    - _PreparedPolicy: 缓存或新建的视图; 缓存持有原对象引用以防 id 复用
    """
    key = id(settings)
    cached = _PREPARED.get(key)
    if cached is not None and cached[0] is settings:
        return cached[1]
    if len(_PREPARED) >= _PREPARED_LIMIT:
        _PREPARED.clear()
    prepared = _PreparedPolicy(settings)
    _PREPARED[key] = (settings, prepared)
    return prepared


def _merge(
    settings: Mapping[str, Any], group_id: str, now: datetime | None, sources: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    """
    按平台, 时段, 群的顺序逐字段合并, 可选同时记录每个字段的有效来源

    参数:
    - settings: 已校验的平台策略
    - group_id: 目标群, 空字符串表示不应用覆盖
    - now: 本机当地时间, 默认读取系统时间
    - sources: 传入字典时按字段写入 {source, source_index, source_label}

    返回:
    - dict[str, Any]: 合并后的独立配置副本
    """
    prepared = _prepared(settings)
    resolved = dict(prepared.base)
    if sources is not None:
        for key in resolved:
            sources[key] = {"source": SOURCE_PLATFORM, "source_index": None, "source_label": "平台配置"}
    if group_id:
        now = datetime.now().astimezone() if now is None else now
        minute = now.hour * 60 + now.minute
        for start, end, overrides, index, label in prepared.periods:
            matches = start <= minute < end if start < end else minute >= start or minute < end
            if matches:
                resolved.update(overrides)
                if sources is not None:
                    for key in overrides:
                        sources[key] = {"source": SOURCE_TIME_RULE, "source_index": index, "source_label": label}
        override = prepared.groups.get(group_id)
        if override is not None:
            resolved.update(override)
            if sources is not None:
                for key in override:
                    sources[key] = {
                        "source": SOURCE_GROUP, "source_index": None, "source_label": f"群 {group_id} 覆盖",
                    }
    for key, value in resolved.items():
        if isinstance(value, (list, dict)):
            resolved[key] = deepcopy(cast(object, value))
    # 只对容器值复制, 标量共享; 避免整份配置 deepcopy 的常量开销
    return resolved


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
    return _merge(settings, group_id, now, None)


def resolve_wake_policy_sources(
    settings: Mapping[str, Any], group_id: str = "", now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """
    解析每个字段的有效来源, 供试算与配置预览展示 (不进入持久化 settings)

    参数:
    - settings: 已校验的平台策略
    - group_id: 目标群, 空字符串表示不应用覆盖
    - now: 本机当地时间, 默认读取系统时间

    返回:
    - dict[str, dict[str, Any]]: 字段 -> {source, source_index, source_label};
      与 resolve_wake_settings 共用同一合并实现, 展示结论不会偏离真实生效值
    """
    sources: dict[str, dict[str, Any]] = {}
    _merge(settings, group_id, now, sources)
    return sources
