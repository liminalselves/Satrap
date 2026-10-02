"""平台入站策略配置校验, 供配置保存与适配器启动共用

`POLICY_FIELD_CONTRACT` 是策略字段的唯一事实来源: 覆盖范围, 校验范围, 热更新能力,
试算展示口径, 显式关闭值与运行时默认值都由它派生, 前端契约 JSON 由同一张表生成;
本模块不在导入期依赖 wake_overrides, 覆盖结构校验在使用点惰性导入
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal, TypedDict, cast
import math
import re


FieldKind = Literal[
    "int", "number", "bool", "enum", "text", "list", "notice_types",
    "words", "group_ids", "scope", "group_map", "time_rules",
]

FieldScope = Literal["platform", "group", "time"]
"""覆盖范围: platform 仅平台级, group 平台与群覆盖, time 平台, 时段与群覆盖"""


class PolicyFieldRequired(TypedDict):
    """策略字段契约的必需项"""

    kind: FieldKind
    scope: FieldScope
    hot_reload: bool
    display_in_preview: bool


class PolicyField(PolicyFieldRequired, total=False):
    """
    策略字段契约的可选项

    - off_value: 覆盖编辑器的"显式关闭"取值, 缺省表示该字段不支持关闭态
    - min / max: 含端点的数值范围; max_exclusive: 排他上界, 与 max 不同时声明
    - max_length / max_items: 文本长度与列表条数上限
    - enum: 允许的字符串取值
    - default: 运行时默认值, 缺省表示没有默认值 (缺失即继承或未设置)
    - nullable: 是否接受显式 null (与"缺失"不同)
    - message: 校验失败文案覆盖, 只在后端使用, 不进入前端契约 JSON
    """

    off_value: object
    min: float
    max: float
    max_exclusive: float
    integer: bool
    max_length: int
    max_items: int
    enum: tuple[str, ...]
    default: object
    nullable: bool
    message: str


POLICY_FIELD_CONTRACT: dict[str, PolicyField] = {
    "message_text_limit": {
        "kind": "int", "scope": "platform", "hot_reload": True, "display_in_preview": False,
        "min": 64, "max": 32000, "integer": True, "default": 2000,
    },
    "input_text_limit": {
        "kind": "int", "scope": "platform", "hot_reload": True, "display_in_preview": False,
        "min": 1, "max": 200000, "integer": True, "default": 20000,
    },
    "input_media_limit": {
        "kind": "int", "scope": "platform", "hot_reload": True, "display_in_preview": False,
        "min": 1, "max": 32, "integer": True, "default": 8,
    },
    "wake_mode": {
        "kind": "enum", "scope": "time", "hot_reload": True, "display_in_preview": True,
        "enum": ("explicit", "frequency", "necessity"), "off_value": "explicit", "default": "explicit",
    },
    "wake_message_threshold": {
        "kind": "int", "scope": "time", "hot_reload": True, "display_in_preview": True,
        "min": 1, "max": 32, "integer": True, "default": 3,
    },
    "wake_cooldown": {
        "kind": "number", "scope": "time", "hot_reload": True, "display_in_preview": True,
        "min": 0, "default": 30, "message": "wake_cooldown 必须是有限的非负数",
    },
    "wake_max_wait": {
        "kind": "number", "scope": "time", "hot_reload": True, "display_in_preview": True,
        "min": 0, "max_exclusive": 120, "default": 0,
        "message": "wake_max_wait 必须为 0 到 120 之间的有限秒数, 不含 120; 0 表示关闭",
    },
    "wake_score_threshold": {
        "kind": "number", "scope": "time", "hot_reload": True, "display_in_preview": True,
        "min": 0, "max": 1, "default": 0.65,
    },
    "wake_question_weight": {
        "kind": "number", "scope": "time", "hot_reload": True, "display_in_preview": False,
        "min": 0, "max": 1, "default": 0.55,
    },
    "wake_address_weight": {
        "kind": "number", "scope": "time", "hot_reload": True, "display_in_preview": False,
        "min": 0, "max": 1, "default": 0.15,
    },
    "wake_backlog_weight": {
        "kind": "number", "scope": "time", "hot_reload": True, "display_in_preview": False,
        "min": 0, "max": 1, "default": 0.30,
    },
    "wake_reply_penalty": {
        "kind": "number", "scope": "time", "hot_reload": True, "display_in_preview": False,
        "min": 0, "max": 1, "default": 0.40,
    },
    "wake_talk_value": {
        "kind": "number", "scope": "time", "hot_reload": True, "display_in_preview": True,
        "min": 0, "max": 1, "off_value": 0, "nullable": True,
    },
    "wake_words": {
        "kind": "words", "scope": "group", "hot_reload": True, "display_in_preview": False,
        "off_value": [],
    },
    "wake_aliases": {
        "kind": "words", "scope": "group", "hot_reload": True, "display_in_preview": False,
        "off_value": [],
    },
    "reply_with_quote": {
        "kind": "bool", "scope": "group", "hot_reload": False, "display_in_preview": False,
        "off_value": False,
    },
    "reply_with_mention": {
        "kind": "bool", "scope": "group", "hot_reload": False, "display_in_preview": False,
        "off_value": False,
    },
    "quote_lookup": {
        "kind": "bool", "scope": "group", "hot_reload": False, "display_in_preview": False,
        "off_value": False,
    },
    "forward_lookup": {
        "kind": "bool", "scope": "group", "hot_reload": False, "display_in_preview": False,
        "off_value": False,
    },
    "wake_on_quote_self": {
        "kind": "bool", "scope": "group", "hot_reload": False, "display_in_preview": False,
        "off_value": False,
    },
    "enable_private": {
        "kind": "bool", "scope": "platform", "hot_reload": True, "display_in_preview": False,
    },
    "enable_group": {
        "kind": "bool", "scope": "platform", "hot_reload": True, "display_in_preview": False,
    },
    "group_whitelist": {
        "kind": "group_ids", "scope": "platform", "hot_reload": True, "display_in_preview": False,
    },
    "context_scope": {
        "kind": "scope", "scope": "platform", "hot_reload": True, "display_in_preview": False,
    },
    "asr_model": {
        "kind": "text", "scope": "platform", "hot_reload": True, "display_in_preview": False,
        "max_length": 128,
        "message": "asr_model 必须是不超过 128 字符的 ASR 配置名称, 留空关闭语音转写",
    },
    "voice_transcribe": {
        "kind": "enum", "scope": "platform", "hot_reload": True, "display_in_preview": False,
        "enum": ("off", "asr", "platform", "asr_then_platform"),
    },
    "attachment_extract": {
        "kind": "bool", "scope": "platform", "hot_reload": True, "display_in_preview": False,
    },
    "media_insecure_tls": {
        "kind": "bool", "scope": "platform", "hot_reload": True, "display_in_preview": False,
    },
    "media_plaintext_http": {
        "kind": "bool", "scope": "platform", "hot_reload": True, "display_in_preview": False,
    },
    "media_trusted_hosts": {
        "kind": "list", "scope": "platform", "hot_reload": True, "display_in_preview": False,
        "max_items": 32, "max_length": 253,
    },
    "command_operators": {
        "kind": "list", "scope": "platform", "hot_reload": True, "display_in_preview": False,
        "max_items": 32, "max_length": 64,
    },
    "notice_types": {
        "kind": "notice_types", "scope": "platform", "hot_reload": False, "display_in_preview": False,
        "max_items": 64, "nullable": True,
    },
    "wake_group_overrides": {
        "kind": "group_map", "scope": "platform", "hot_reload": True, "display_in_preview": False,
        "max_items": 512,
    },
    "wake_time_rules": {
        "kind": "time_rules", "scope": "platform", "hot_reload": True, "display_in_preview": False,
        "max_items": 32,
    },
}
"""策略字段契约: 校验, 覆盖范围, 热更新, 试算展示, 显式关闭值与默认值的唯一事实来源"""

POLICY_DEFAULTS: dict[str, object] = {
    key: field["default"] for key, field in POLICY_FIELD_CONTRACT.items() if "default" in field
}
"""未在配置中出现的策略字段的运行时默认值, 由 POLICY_FIELD_CONTRACT 派生 (试算的来源与默认值也取这一份)"""


def policy_default(key: str) -> object:
    """
    读取字段的运行时默认值

    参数:
    - key: 策略字段名, 必须存在于 POLICY_DEFAULTS

    返回:
    - object: 默认值
    """
    return POLICY_DEFAULTS[key]


def policy_default_int(key: str) -> int:
    """
    读取字段的整数默认值

    参数:
    - key: 策略字段名, 必须存在于 POLICY_DEFAULTS 且默认值为 int

    返回:
    - int: 默认值

    异常:
    - TypeError: 该字段没有默认值或默认值不是整数
    """
    value = POLICY_DEFAULTS[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} 的默认值不是整数")
    return value


def fields_with_scope(*scopes: FieldScope) -> frozenset[str]:
    """
    按覆盖范围取字段集合

    参数:
    - scopes: 需要包含的覆盖范围

    返回:
    - frozenset[str]: 命中范围之一的字段名集合
    """
    return frozenset(
        key for key, field in POLICY_FIELD_CONTRACT.items() if field["scope"] in scopes
    )


def hot_reload_keys() -> frozenset[str]:
    """逐事件生效 (不重建实例) 的字段集合"""
    return frozenset(key for key, field in POLICY_FIELD_CONTRACT.items() if field["hot_reload"])


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
    if "message_archive_retention_days" in settings:
        value = settings["message_archive_retention_days"]
        if type(value) is not int or not 1 <= value <= 3650:
            raise ValueError("message_archive_retention_days 必须是 1 到 3650 的整数")


_SCALAR_KINDS = frozenset({"int", "number", "bool", "enum", "text", "list"})
"""可由契约表统一校验的字段类型; 其余类型由既有专用校验器或读取点处理"""


def _bounded(value: float, minimum: float | None, maximum: float | None, exclusive: float | None) -> bool:
    """含上界, 排他上界与只有下界三种范围判定, 缺省边界表示不限制"""
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    if exclusive is not None and value >= exclusive:
        return False
    return True


def _field_error(key: str, field: PolicyField) -> str:
    """按契约生成校验失败文案, 需要额外说明的字段在表里覆盖"""
    message = field.get("message")
    if message is not None:
        return str(message)
    kind = field["kind"]
    minimum = field.get("min")
    maximum = field.get("max")
    exclusive = field.get("max_exclusive")
    if kind == "int":
        return f"{key} 必须为 {minimum} 到 {maximum} 的整数"
    if kind == "number":
        if exclusive is not None:
            return f"{key} 必须为不小于 {minimum} 且小于 {exclusive} 的有限数值"
        if maximum is not None:
            return f"{key} 必须为 {minimum} 到 {maximum} 的有限数值"
        return f"{key} 必须为不小于 {minimum} 的有限数值"
    if kind == "bool":
        return f"{key} 必须为布尔值"
    if kind == "enum":
        return f"{key} 必须为 {' 或 '.join(field.get('enum') or ())}"
    if kind == "text":
        return f"{key} 必须是不超过 {field.get('max_length')} 字符的文本"
    return f"{key} 必须是最多 {field.get('max_items')} 项的文本列表"


def _validate_field(key: str, field: PolicyField, value: object) -> None:
    """
    按契约校验单个标量或列表字段

    参数:
    - key: 字段名
    - field: 字段契约
    - value: 配置中的显式取值, 调用方已排除缺失与允许的 null
    """
    kind = field["kind"]
    minimum = field.get("min")
    maximum = field.get("max")
    exclusive = field.get("max_exclusive")
    failure = False
    if kind == "int":
        failure = (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not _bounded(value, minimum, maximum, exclusive)
        )
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            failure = True
        else:
            failure = not _bounded(value, minimum, maximum, exclusive)
    elif kind == "bool":
        failure = not isinstance(value, bool)
    elif kind == "enum":
        failure = not isinstance(value, str) or value not in (field.get("enum") or ())
    elif kind == "text":
        max_length = field.get("max_length")
        failure = not isinstance(value, str) or (max_length is not None and len(value) > max_length)
    elif kind == "list":
        items = cast(list[object], value) if isinstance(value, list) else []
        max_items = field.get("max_items")
        max_length = field.get("max_length")
        failure = (
            not isinstance(value, list)
            or (max_items is not None and len(items) > max_items)
            or any(
                not isinstance(item, str)
                or not item.strip()
                or (max_length is not None and len(item) > max_length)
                for item in items
            )
        )
    if failure:
        raise ValueError(_field_error(key, field))


def _validate_notice_types(settings: Mapping[str, object]) -> None:
    """校验通知订阅列表: 显式 null 表示派发全部类型, 与既有语义一致"""
    notice_types = settings.get("notice_types")
    if notice_types is None:
        return
    if not isinstance(notice_types, list) or len(cast(list[object], notice_types)) > 64 or any(
        not isinstance(item, str) or not re.fullmatch(r"(notice|request)(\.[a-z_]+)?", item)
        for item in cast(list[object], notice_types)
    ):
        raise ValueError("notice_types 必须是最多 64 项的 notice/request 或 notice.<类型>/request.<类型> 列表")


def validate_wake_policy(settings: Mapping[str, object]) -> None:
    """
    校验已实现的自动参与参数

    标量检查由 `POLICY_FIELD_CONTRACT` 驱动, 覆盖结构与词表归一化等表无法表达的检查保持手写

    参数:
    - settings: 平台策略配置

    异常:
    - ValueError: 字段类型或范围非法, 或覆盖结构与词表非法
    """
    from satrap.core.config.wake_overrides import validate_wake_overrides

    validate_wake_overrides(settings)
    for key, field in POLICY_FIELD_CONTRACT.items():
        kind = field["kind"]
        if kind == "notice_types":
            _validate_notice_types(settings)
            continue
        if kind not in _SCALAR_KINDS or key not in settings:
            # 词表, 群白名单, 上下文范围与覆盖结构由专用校验器或读取点处理, 行为保持不变
            continue
        value = settings[key]
        if value is None:
            if field.get("nullable", False):
                continue
            raise ValueError(_field_error(key, field))
        _validate_field(key, field, value)


_NORMALIZED_CACHE: dict[tuple[str, int], tuple[object, list[str]]] = {}
_NORMALIZED_CACHE_LIMIT = 256


def _cached_normalize(kind: str, value: object, compute: Callable[[object], list[str]]) -> list[str]:
    """
    按列表对象身份缓存归一化结果, 配置替换后自动失效

    参数:
    - kind: 缓存类别
    - value: 原始配置列表
    - compute: 归一化函数, 非法输入抛 ValueError 且不缓存

    返回:
    - list[str]: 归一化结果的独立副本
    """
    if not isinstance(value, list):
        return compute(value)
    items = cast(list[object], value)
    key = (kind, id(items))
    cached = _NORMALIZED_CACHE.get(key)
    if cached is not None and cached[0] is items:
        return list(cached[1])
    result = compute(items)
    if len(_NORMALIZED_CACHE) >= _NORMALIZED_CACHE_LIMIT:
        _NORMALIZED_CACHE.clear()
    _NORMALIZED_CACHE[key] = (items, result)
    return list(result)


def normalize_group_whitelist(value: object) -> list[str]:
    """
    校验群范围并归一化群 ID, 同一配置列表重复调用命中缓存

    参数:
    - value: 群 ID 列表, 空列表表示不限制群范围

    返回:
    - 去重后的十进制群 ID 列表, 非法输入抛出 ValueError
    """
    return _cached_normalize("group_whitelist", value, _normalize_group_whitelist)


def _normalize_group_whitelist(value: object) -> list[str]:
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
    校验显式唤醒词列表, 同一配置列表重复调用命中缓存

    参数:
    - value: 非空文本组成的列表, 空列表表示仅使用真实提及

    返回:
    - 去重后的唤醒词列表, 非法输入抛出 ValueError
    """
    return _cached_normalize("wake_words", value, _normalize_wake_words)


def _normalize_wake_words(value: object) -> list[str]:
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
