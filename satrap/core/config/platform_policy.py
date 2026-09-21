"""平台入站策略配置校验, 供配置保存与适配器启动共用"""
from __future__ import annotations

from collections.abc import Mapping
from typing import cast
import math
import re

from satrap.core.config.wake_overrides import validate_wake_overrides


def validate_event_limits(settings: Mapping[str, object]) -> None:
    """
    校验有限的事件执行容量与等待时间

    参数:
    - settings: 平台配置, 缺省字段使用运行时默认值
    """
    for key in ("event_queue_capacity", "event_pending_capacity", "event_concurrency"):
        if key in settings:
            value = settings[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{key} 必须是正整数")
    if "event_queue_ttl" in settings:
        value = settings["event_queue_ttl"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("event_queue_ttl 必须是有限的正数")


def validate_wake_policy(settings: Mapping[str, object]) -> None:
    """
    校验已实现的自动参与参数

    参数:
    - settings: 平台策略配置
    """
    validate_wake_overrides(settings)
    text_limit = settings.get("message_text_limit", 2000)
    if isinstance(text_limit, bool) or not isinstance(text_limit, int) or not 64 <= text_limit <= 32000:
        raise ValueError("message_text_limit 必须为 64 到 32000 的整数")
    for key in ("reply_with_quote", "reply_with_mention", "quote_lookup", "wake_on_quote_self"):
        if key in settings and not isinstance(settings[key], bool):
            raise ValueError(f"{key} 必须为布尔值")
    notice_types = settings.get("notice_types")
    if notice_types is not None:
        if not isinstance(notice_types, list) or len(cast(list[object], notice_types)) > 64 or any(
            not isinstance(item, str) or not re.fullmatch(r"(notice|request)(\.[a-z_]+)?", item) for item in cast(list[object], notice_types)
        ):
            raise ValueError("notice_types 必须是最多 64 项的 notice/request 或 notice.<类型>/request.<类型> 列表")
    mode = settings.get("wake_mode", "explicit")
    if not isinstance(mode, str) or mode not in {"explicit", "frequency", "necessity"}:
        raise ValueError("wake_mode 必须为 explicit, frequency 或 necessity")
    threshold = settings.get("wake_message_threshold", 3)
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 1 <= threshold <= 32:
        raise ValueError("wake_message_threshold 必须为 1 到 32 的整数")
    cooldown = settings.get("wake_cooldown", 30)
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or not math.isfinite(cooldown) or cooldown < 0:
        raise ValueError("wake_cooldown 必须是有限的非负数")
    max_wait = settings.get("wake_max_wait", 0)
    if isinstance(max_wait, bool) or not isinstance(max_wait, (int, float)) or not math.isfinite(max_wait) or not 0 <= max_wait < 120:
        raise ValueError("wake_max_wait 必须为 0 到 120 之间的有限秒数, 不含 120; 0 表示关闭")
    for key, default in (("wake_score_threshold", 0.65), ("wake_question_weight", 0.55),
                         ("wake_address_weight", 0.15), ("wake_backlog_weight", 0.30), ("wake_reply_penalty", 0.40)):
        value = settings.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{key} 必须为 0 到 1 的有限数值")


def normalize_group_whitelist(value: object) -> list[str]:
    """
    校验群范围并归一化群 ID

    参数:
    - value: 群 ID 列表, 空列表表示不限制群范围

    返回:
    - 去重后的十进制群 ID 列表, 非法输入抛出 ValueError
    """
    if not isinstance(value, list):
        raise ValueError("group_whitelist 必须是群 ID 列表")
    result: list[str] = []
    for item in cast(list[object], value):
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise ValueError("group_whitelist 必须包含正整数群 ID")
        text = str(item).strip()
        if not text.isascii() or not text.isdecimal() or int(text) <= 0:
            raise ValueError("group_whitelist 必须包含正整数群 ID")
        normalized = str(int(text))
        if normalized not in result:
            result.append(normalized)
    return result


def normalize_wake_words(value: object) -> list[str]:
    """
    校验显式唤醒词列表

    参数:
    - value: 非空文本组成的列表, 空列表表示仅使用真实提及

    返回:
    - 去重后的唤醒词列表, 非法输入抛出 ValueError
    """
    if not isinstance(value, list):
        raise ValueError("wake_words 必须是非空文本列表")
    result: list[str] = []
    for word in cast(list[object], value):
        if not isinstance(word, str) or not word.strip():
            raise ValueError("wake_words 必须是非空文本列表")
        if word.strip() not in result:
            result.append(word.strip())
    return result


def validate_context_scope(value: object) -> None:
    """
    校验平台会话隔离范围

    参数:
    - value: legacy_user, group_member 或 group
    """
    if not isinstance(value, str) or value not in {"legacy_user", "group_member", "group"}:
        raise ValueError("context_scope 必须为 legacy_user, group_member 或 group")
