"""唤醒策略试算: 隔离临时 WakeWindow + now 注入, 与真实路径共用决策实现

不触碰生产窗口, 不写任何状态, 不调用模型; 缺少必要上下文时返回明确的"无法判断"原因而非猜测
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, cast
import re

from satrap.core.config.platform_policy import POLICY_DEFAULTS, normalize_group_whitelist, validate_wake_policy
from satrap.core.config.wake_overrides import SOURCE_PLATFORM, resolve_wake_policy_sources, resolve_wake_settings
from satrap.core.pipeline.wake_policy import NEVER_TRIGGER_THRESHOLD, WakeDecision, evaluate_wake, resolve_message_threshold
from satrap.core.pipeline.wake_window import PendingText, WakeWindow
from satrap.core.platform import PlatformConfig
from satrap.core.platform.event import MessageEvent
from satrap.core.platform.onebot.adapter import OneBotAdapter

MAX_STEPS = 64
MAX_STEP_TEXT = 1000
MAX_ADVANCE_SECONDS = 3600
_BASE_NOW = 1_000_000.0
"""试算单调时钟起点, 远大于零避免负值歧义"""

SOURCE_BUILTIN_DEFAULT = "builtin_default"
"""字段来源: 未在任何一层设置, 使用运行时默认值"""

POLICY_SOURCE_KEYS: tuple[str, ...] = (*POLICY_DEFAULTS, "wake_talk_value")
"""策略来源解析覆盖的字段: 自动参与参数与输入预算"""


def _threshold_preview(settings: dict[str, Any]) -> dict[str, object]:
    """
    有效阈值与 wake_talk_value 的关系, 供界面说明映射为何生效或被覆盖

    参数:
    - settings: 已合并的有效策略

    返回:
    - dict[str, object]: 阈值, 来源与覆盖提示; 关闭时 value 为 None 且 closed 为 True
    """
    resolution = resolve_message_threshold(settings)
    overridden = resolution.talk_value is not None and not resolution.talk_value_effective
    return {
        "value": None if resolution.threshold == NEVER_TRIGGER_THRESHOLD else resolution.threshold,
        "source": resolution.source, "label": resolution.label,
        "talk_value": resolution.talk_value, "talk_value_effective": resolution.talk_value_effective,
        "closed": resolution.closed, "overridden": overridden,
        "hint": (
            "wake_talk_value 已被显式 wake_message_threshold 覆盖, 不参与频率判断" if overridden
            else ("wake_talk_value=0, 自动参与不触发, 最长等待不补偿" if resolution.closed else "")
        ),
    }


def _source_preview(settings: dict[str, Any], group_id: str, now: datetime) -> dict[str, dict[str, object]]:
    """
    逐字段的有效值与来源, 供试算与配置预览展示

    参数:
    - settings: 平台策略草稿
    - group_id: 目标群, 空字符串表示不应用覆盖
    - now: 注入的本地时间

    返回:
    - dict[str, dict[str, object]]: 字段 -> {value, source, source_index, source_label}
    """
    resolved = resolve_wake_settings(settings, group_id, now)
    sources = resolve_wake_policy_sources(settings, group_id, now)
    preview: dict[str, dict[str, object]] = {}
    for key in POLICY_SOURCE_KEYS:
        origin = sources.get(key)
        if origin is not None:
            preview[key] = {"value": resolved.get(key), **origin}
            continue
        # 未设置: 展示运行时默认值, 与校验使用的同一张默认值表
        preview[key] = {
            "value": POLICY_DEFAULTS.get(key), "source": SOURCE_BUILTIN_DEFAULT,
            "source_index": None, "source_label": "未设置, 使用默认值",
        }
    for key, origin in sources.items():
        if key in preview or key in {"wake_group_overrides", "wake_time_rules"}:
            # 覆盖表与时段表是来源本身而不是单值字段, 不回显整份结构
            continue
        preview[key] = {"value": resolved.get(key), **origin}
    return preview


def _undetermined(reason: str) -> dict[str, object]:
    """无法判断分支的统一形状, triggered 为 None 区别于 true/false"""
    return {"triggered": None, "rule": "undetermined", "reason": reason, "matched": "", "score": None}


def _decision_dict(decision: WakeDecision) -> dict[str, object]:
    """序列化真实决策结果"""
    return {
        "triggered": decision.triggered, "rule": decision.rule, "reason": decision.reason,
        "matched": decision.matched, "score": decision.score,
    }


def _parse_local_time(value: object) -> datetime:
    """解析 HH:MM 并贴到本机今日, 供时段规则确定性匹配"""
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("local_time 必须为 HH:MM")
    hour, minute = (int(part) for part in value.split(":"))
    return datetime.now().astimezone().replace(hour=hour, minute=minute, second=0, microsecond=0)


def _validate_steps(raw: object) -> list[dict[str, Any]]:
    """校验场景步骤: 文本消息或 submit 提交标记"""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(cast(list[object], raw)) > MAX_STEPS:
        raise ValueError(f"steps 必须是最多 {MAX_STEPS} 项的列表")
    steps: list[dict[str, Any]] = []
    for index, item in enumerate(cast(list[object], raw)):
        if not isinstance(item, dict):
            raise ValueError(f"steps[{index}] 必须是对象")
        entry = cast(dict[str, Any], item)
        advance = entry.get("advance_seconds", 0)
        if isinstance(advance, bool) or not isinstance(advance, (int, float)) or not 0 <= advance <= MAX_ADVANCE_SECONDS:
            raise ValueError(f"steps[{index}].advance_seconds 必须为 0 到 {MAX_ADVANCE_SECONDS} 秒")
        if entry.get("submit") is True:
            steps.append({"submit": True, "advance_seconds": float(advance)})
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_STEP_TEXT:
            raise ValueError(f"steps[{index}].text 必须为 1 到 {MAX_STEP_TEXT} 字符")
        actor = entry.get("actor", "30")
        if not isinstance(actor, str) or not actor.isascii() or not actor.isdecimal():
            raise ValueError(f"steps[{index}].actor 必须为数字字符串")
        steps.append({
            "text": text, "actor": actor, "advance_seconds": float(advance),
            "at_self": entry.get("at_self") is True,
        })
    return steps


async def _build_event(
    adapter: OneBotAdapter, *, group_id: str, actor: str, text: str, message_id: str, at_self: bool,
) -> MessageEvent:
    """经真实 OneBot 转换与事件构造生成试算事件, 路由键与组件解析与线上一致"""
    segments: list[dict[str, Any]] = []
    if at_self:
        segments.append({"type": "at", "data": {"qq": str(adapter.bot_self_id)}})
    segments.append({"type": "text", "data": {"text": text}})
    raw: dict[str, Any] = {
        "self_id": adapter.bot_self_id, "group_id": group_id, "user_id": actor,
        "message_id": message_id, "message_type": "group", "post_type": "message", "message": segments,
    }
    message = await adapter.convert_message(raw)
    return MessageEvent(
        message_str=message.message_str,
        platform_message=message,
        platform_meta=adapter.meta(),
        session_id=message.session_id,
        session_provider=adapter.get_session_provider(),
        session_type=adapter.get_session_type(),
        adapter=adapter,
    )


async def dry_run_wake(payload: object) -> dict[str, Any]:
    """
    按草稿策略与样例时间线试算唤醒决策

    参数:
    - payload: {settings, group_id?, local_time?, steps?, probe?}; settings 为完整平台策略草稿

    返回:
    - dict[str, Any]: ok=True 时含 resolved/explicit/automatic 三段; 输入非法时 ValueError 由路由转 400
    """
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    data = cast(dict[str, Any], payload)
    settings = data.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("缺少 settings 平台策略对象")
    draft = cast(dict[str, Any], settings)
    validate_wake_policy(draft)
    group_id = data.get("group_id", "")
    if group_id in (None, ""):
        group_id = ""
    else:
        try:
            normalized = normalize_group_whitelist([group_id]) if isinstance(group_id, str) else []
        except ValueError:
            normalized = []
        if normalized != [group_id]:
            raise ValueError("group_id 必须是规范的正整数群 ID")
    local_time = data.get("local_time")
    moment = _parse_local_time(local_time) if local_time not in (None, "") else datetime.now().astimezone()
    steps = _validate_steps(data.get("steps"))
    probe = data.get("probe")
    if probe is not None and not isinstance(probe, dict):
        raise ValueError("probe 必须是对象")

    resolved = resolve_wake_settings(draft, str(group_id), moment)
    raw_self_id = str(draft.get("self_id") or "10000")
    self_id = raw_self_id if raw_self_id.isdecimal() else "10000"
    adapter = OneBotAdapter(PlatformConfig(
        id="wake-dry-run", type="onebot",
        settings={"self_id": self_id, "enable_group": True},
    ))
    group = str(group_id) or "20"

    async def scenario_event(*, actor: str, text: str, message_id: str, at_self: bool) -> MessageEvent:
        event = await _build_event(adapter, group_id=group, actor=actor, text=text, message_id=message_id, at_self=at_self)
        # 用注入的本地时间重算有效策略, 覆盖构造期的真实时刻解析
        event.policy_settings = resolved
        return event

    # 显式路径: 直接提及/唤醒词/别名评估探测消息
    explicit: dict[str, object]
    if probe is None:
        explicit = _undetermined("未提供探测消息, 无法评估显式唤醒路径")
    else:
        probe_data = cast(dict[str, Any], probe)
        probe_text = probe_data.get("text", "")
        if not isinstance(probe_text, str) or len(probe_text) > MAX_STEP_TEXT:
            raise ValueError(f"probe.text 必须为不超过 {MAX_STEP_TEXT} 字符的字符串")
        if probe_data.get("quote_self") is True:
            explicit = _undetermined("引用回源内容无法在试算中获取, 真实路径将回源后按 wake_on_quote_self 评估")
        elif not probe_text.strip() and probe_data.get("at_self") is not True:
            explicit = _undetermined("探测消息既无正文也未提及机器人, 无显式规则可评估")
        else:
            probe_event = await scenario_event(
                actor=str(probe_data.get("actor") or "30"), text=probe_text, message_id="probe",
                at_self=probe_data.get("at_self") is True,
            )
            explicit = _decision_dict(evaluate_wake(probe_event))

    # 自动路径: 隔离窗口按样例时间推进, submit 步骤经 decide+claim 真实标记提交以覆盖冷却
    window = WakeWindow()
    now = _BASE_NOW
    snapshot: tuple[PendingText, ...] = ()
    last_event: MessageEvent | None = None
    step_results: list[dict[str, object]] = []
    for index, step in enumerate(steps):
        now += float(step["advance_seconds"])
        if step.get("submit") is True:
            if last_event is None:
                step_results.append({"index": index, "kind": "submit", "decision": _undetermined("尚无可评估消息")})
                continue
            decision = window.decide(last_event, snapshot, now)
            claimed = window.claim(last_event, snapshot, True, now)
            if claimed:
                snapshot = ()
            step_results.append({
                "index": index, "kind": "submit", "decision": _decision_dict(decision), "claimed": len(claimed),
            })
            continue
        event = await scenario_event(
            actor=str(step["actor"]), text=str(step["text"]), message_id=str(index + 1),
            at_self=bool(step.get("at_self")),
        )
        snapshot = window.observe(event, now)
        last_event = event
        step_results.append({"index": index, "kind": "message", "observed": len(snapshot)})

    if last_event is None:
        final_decision = _undetermined("场景不含消息, 无法评估自动参与")
        deadline_decision = _undetermined("场景不含消息, 无法评估自动参与")
        cooldown_remaining = 0.0
    else:
        final_decision = _decision_dict(window.decide(last_event, snapshot, now))
        deadline_decision = _decision_dict(window.decide(last_event, snapshot, now, deadline=True))
        cooldown_remaining = window.cooldown_remaining(last_event, now)

    return {
        "ok": True,
        "resolved": {key: resolved[key] for key in sorted(resolved) if key.startswith("wake_")},
        "sources": _source_preview(draft, str(group_id), moment),
        "defaults": dict(POLICY_DEFAULTS),
        "explicit": explicit,
        "automatic": {
            "mode": str(resolved.get("wake_mode", "explicit")),
            "observed": len(snapshot),
            "steps": step_results,
            "decision": final_decision,
            "deadline_decision": deadline_decision,
            "cooldown_remaining": round(cooldown_remaining, 3),
            "threshold": _threshold_preview(resolved),
        },
    }
