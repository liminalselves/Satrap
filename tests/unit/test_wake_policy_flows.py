"""业务链路场景与集中边界契约"""
from __future__ import annotations

from datetime import datetime
import pytest
from satrap.core.config.wake_overrides import resolve_wake_settings
from satrap.core.config.platform_policy import validate_wake_policy
from typing import Any, cast
import asyncio
from satrap.core.pipeline.wake_dry_run import dry_run_wake
from collections.abc import Mapping
from pathlib import Path
import json
import subprocess
import sys
from unittest.mock import AsyncMock
from satrap.core.config.platform_policy import POLICY_DEFAULTS, POLICY_FIELD_CONTRACT, fields_with_scope
from satrap.core.config.platform_policy import hot_reload_keys, policy_default
from satrap.core.config.wake_overrides import AUTOMATIC_KEYS, GROUP_KEYS
from satrap.core.pipeline.input_projection import project_input
from satrap.core.pipeline.wake_window import PendingMessage, WakeWindow
from satrap.core.platform import PlatformConfig
from satrap.core.platform.event import MessageChain, MessageEvent
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.components import Plain

def test_midnight_exclusive_end_and_group_precedence():
    settings = {
        "wake_mode": "frequency", "wake_message_threshold": 3, "wake_words": ["唤醒"],
        "wake_time_rules": [{"start": "23:00", "end": "07:00", "settings": {"wake_mode": "explicit"}}],
        "wake_group_overrides": {"123": {"wake_mode": "necessity", "wake_score_threshold": 0.8}},
    }
    validate_wake_policy(settings)
    for hour in [23, 0, 6]:
        now = datetime(2026, 9, 21, hour, 0)
        assert resolve_wake_settings(settings, "456", now)["wake_mode"] == "explicit"
        assert resolve_wake_settings(settings, "123", now)["wake_mode"] == "necessity"
        assert resolve_wake_settings(settings, "456", now)["wake_words"] == ["唤醒"]
    assert resolve_wake_settings(settings, "456", datetime(2026, 9, 21, 7, 0))["wake_mode"] == "frequency"
    assert resolve_wake_settings(settings, "", datetime(2026, 9, 21, 0, 0))["wake_mode"] == "frequency"
    result = resolve_wake_settings(settings, "123")
    result["wake_words"].append("不能改原配置")
    assert settings["wake_words"] == ["唤醒"]

@pytest.mark.parametrize("settings", [
    {"wake_group_overrides": {"001": {}}},
    {"wake_group_overrides": {"123": {"context_scope": "group"}}},
    {"wake_group_overrides": {"123": {"wake_group_overrides": {}}}},
    {"wake_group_overrides": {"123": {"wake_message_threshold": 0}}},
    {"wake_time_rules": [{"start": "24:00", "end": "07:00", "settings": {}}]},
    {"wake_time_rules": [{"start": "07:00", "end": "07:00", "settings": {}}]},
    {"wake_time_rules": [{"start": "23:00", "end": "07:00", "settings": {"wake_words": []}}]},
])
def test_invalid_overrides_rejected_without_changing_access_or_identity(settings):
    with pytest.raises(ValueError):
        validate_wake_policy(settings)

def test_overlapping_periods_apply_in_list_order():
    settings = {"wake_time_rules": [
        {"start": "09:00", "end": "18:00", "settings": {"wake_cooldown": 10}},
        {"start": "12:00", "end": "13:00", "settings": {"wake_cooldown": 60}},
    ]}
    validate_wake_policy(settings)
    assert resolve_wake_settings(settings, "123", datetime(2026, 9, 21, 12, 30))["wake_cooldown"] == 60

def test_malformed_runtime_structures_are_ignored():
    from satrap.core.config.wake_overrides import resolve_wake_settings
    settings = {"wake_mode": "explicit", "wake_time_rules": "oops", "wake_group_overrides": ["bad"]}
    assert resolve_wake_settings(settings, "123")["wake_mode"] == "explicit"
    settings = {"wake_time_rules": [{"start": "bad", "end": "09:00", "settings": {"wake_mode": "frequency"}}, "x"],
                "wake_group_overrides": {"123": "not-a-dict"}}
    assert "wake_mode" not in resolve_wake_settings(settings, "123")

def test_policy_sources_match_the_merge_order():
    """来源解析与值合并共用同一实现: 重叠时段以列表后项为准, 群覆盖最后生效"""
    from satrap.core.config.wake_overrides import (
        SOURCE_GROUP,
        SOURCE_PLATFORM,
        SOURCE_TIME_RULE,
        resolve_wake_policy_sources,
    )

    settings: dict[str, object] = {
        "wake_mode": "frequency", "wake_cooldown": 30,
        "wake_time_rules": [
            {"start": "08:00", "end": "12:00", "settings": {"wake_max_wait": 10, "wake_cooldown": 60}},
            {"start": "11:00", "end": "14:00", "settings": {"wake_cooldown": 90}},
        ],
        "wake_group_overrides": {"123": {"wake_cooldown": 5}},
    }
    validate_wake_policy(settings)
    noon = datetime(2026, 9, 24, 11, 30)
    """11:30 落在两条重叠时段内, 用于验证后项覆盖前项"""
    sources = resolve_wake_policy_sources(settings, "123", noon)
    resolved = resolve_wake_settings(settings, "123", noon)
    assert sources["wake_mode"] == {"source": SOURCE_PLATFORM, "source_index": None, "source_label": "平台配置"}
    # 两条重叠时段按列表后项生效, 群覆盖再覆盖两者
    time_sources = resolve_wake_policy_sources(settings, "456", noon)
    assert time_sources["wake_cooldown"]["source"] == SOURCE_TIME_RULE
    assert time_sources["wake_cooldown"]["source_index"] == 1
    assert "11:00-14:00" in str(time_sources["wake_cooldown"]["source_label"])
    assert time_sources["wake_max_wait"]["source_index"] == 0
    assert sources["wake_cooldown"]["source"] == SOURCE_GROUP and resolved["wake_cooldown"] == 5
    # 不传群时不应用覆盖, 来源只剩平台与时段
    assert resolve_wake_policy_sources(settings, "", noon)["wake_cooldown"]["source"] == SOURCE_PLATFORM
    # 来源字典与值字典键集合一致, 展示不会指向不存在的字段
    assert set(sources) == set(resolved)

def _automatic(result: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], result["automatic"])

def _decision(result: dict[str, Any], key: str = "decision") -> dict[str, Any]:
    return cast(dict[str, Any], _automatic(result)[key])

def _explicit(result: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], result["explicit"])

class TestAutomaticPath:
    @pytest.mark.asyncio
    async def test_frequency_trigger_with_talk_value_mapping(self):
        settings: dict[str, object] = {"wake_mode": "frequency", "wake_talk_value": 0.75, "self_id": "10000"}
        payload: dict[str, object] = {
            "settings": settings, "group_id": "20",
            "steps": [{"text": "第一条"}, {"text": "第二条", "advance_seconds": 3}],
        }
        result = await dry_run_wake(payload)
        assert result["ok"] is True
        decision = _decision(result)
        assert decision["triggered"] is True and decision["rule"] == "frequency"
        assert "wake_talk_value=0.75" in str(decision["reason"])
        assert _automatic(result)["observed"] == 2
        resolved = result["resolved"]
        assert isinstance(resolved, dict) and resolved["wake_talk_value"] == 0.75

    @pytest.mark.asyncio
    async def test_group_override_and_time_rule_resolution(self):
        settings: dict[str, object] = {
            "wake_mode": "frequency", "wake_message_threshold": 5,
            "wake_time_rules": [{"start": "09:00", "end": "18:00", "settings": {"wake_message_threshold": 2}}],
            "wake_group_overrides": {"20": {"wake_message_threshold": 1}},
        }
        # 群覆盖优先于时段: 阈值为 1
        result = await dry_run_wake({
            "settings": settings, "group_id": "20", "local_time": "14:30", "steps": [{"text": "hi"}],
        })
        decision = _decision(result)
        assert decision["triggered"] is True and "1 (显式" in str(decision["reason"])
        # 无群覆盖时时段生效: 阈值为 2
        other = await dry_run_wake({
            "settings": settings, "group_id": "21", "local_time": "14:30", "steps": [{"text": "hi"}],
        })
        assert _decision(other)["triggered"] is False
        # 时段外回落平台默认阈值 5
        night = await dry_run_wake({
            "settings": settings, "group_id": "21", "local_time": "23:00", "steps": [{"text": "hi"}] * 2,
        })
        assert _decision(night)["triggered"] is False

    @pytest.mark.asyncio
    async def test_necessity_scoring(self):
        settings: dict[str, object] = {"wake_mode": "necessity", "wake_score_threshold": 0.5}
        result = await dry_run_wake({
            "settings": settings, "steps": [{"text": "请问这个怎么配置?"}],
        })
        decision = _decision(result)
        assert decision["rule"] == "necessity" and decision["triggered"] is True
        assert decision["score"] is not None and float(decision["score"]) >= 0.5
        idle = await dry_run_wake({"settings": settings, "steps": [{"text": "哈哈哈哈"}]})
        assert _decision(idle)["triggered"] is False

    @pytest.mark.asyncio
    async def test_submit_step_marks_cooldown(self):
        settings: dict[str, object] = {"wake_mode": "frequency", "wake_message_threshold": 1, "wake_cooldown": 60}
        payload: dict[str, object] = {
            "settings": settings,
            "steps": [{"text": "触发"}, {"submit": True, "advance_seconds": 5}, {"text": "后续", "advance_seconds": 5}],
        }
        result = await dry_run_wake(payload)
        steps = cast(list[Any], _automatic(result)["steps"])
        submit_step = cast(dict[str, Any], steps[1])
        assert submit_step["claimed"] == 1
        decision = _decision(result)
        assert decision["triggered"] is False and decision["rule"] == "cooldown"
        remaining = _automatic(result)["cooldown_remaining"]
        assert isinstance(remaining, (int, float)) and 49 < float(remaining) <= 60

    @pytest.mark.asyncio
    async def test_deadline_evaluation_covers_max_wait(self):
        settings: dict[str, object] = {"wake_mode": "frequency", "wake_message_threshold": 5, "wake_max_wait": 30}
        payload: dict[str, object] = {
            "settings": settings, "steps": [{"text": "积压"}, {"text": "还在", "advance_seconds": 40}],
        }
        result = await dry_run_wake(payload)
        decision = _decision(result)
        assert decision["triggered"] is False and decision["rule"] == "frequency"
        deadline = _decision(result, "deadline_decision")
        assert deadline["triggered"] is True and deadline["rule"] == "max_wait"

    @pytest.mark.asyncio
    async def test_window_ttl_expires_old_messages(self):
        settings: dict[str, object] = {"wake_mode": "frequency", "wake_message_threshold": 2}
        payload: dict[str, object] = {
            "settings": settings, "steps": [{"text": "旧消息"}, {"text": "新消息", "advance_seconds": 200}],
        }
        result = await dry_run_wake(payload)
        assert _automatic(result)["observed"] == 1
        assert _decision(result)["triggered"] is False

    @pytest.mark.asyncio
    async def test_explicit_mode_never_triggers_automatic(self):
        result = await dry_run_wake({"settings": {"wake_mode": "explicit"}, "steps": [{"text": "a"}, {"text": "b"}]})
        decision = _decision(result)
        assert decision["triggered"] is False and decision["rule"] == "explicit_only"

    @pytest.mark.asyncio
    async def test_member_routes_isolated_in_legacy_scope(self):
        settings: dict[str, object] = {"wake_mode": "frequency", "wake_message_threshold": 2}
        result = await dry_run_wake({
            "settings": settings,
            "steps": [{"text": "甲说", "actor": "30"}, {"text": "乙说", "actor": "31"}],
        })
        # legacy_user 范围按成员隔离路由, 两名成员各一条, 不合并计数
        assert _automatic(result)["observed"] == 1
        assert _decision(result)["triggered"] is False

class TestPolicySources:
    """B6/B10: 有效阈值与字段来源必须在试算里可解释, 且与真实生效值一致"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("extra, closed, value, source, overridden", [
        ({"wake_talk_value": 0}, True, None, "talk_value", None),
        ({"wake_talk_value": 0, "wake_message_threshold": 2}, False, 2, "explicit", True),
    ], ids=["closed_by_zero", "explicit_over_zero"])
    async def test_threshold_reporting_matches_effective_state(
        self, extra: dict[str, object], closed: bool, value: object, source: str, overridden: object,
    ):
        """反例: talk_value=0 关闭时不得显示为可触发, 被显式阈值覆盖时必须标出被覆盖"""
        settings: dict[str, object] = {"wake_mode": "frequency", "wake_max_wait": 30, **extra}
        payload: dict[str, object] = {
            "settings": settings, "steps": [{"text": "积压"}, {"text": "还在", "advance_seconds": 40}],
        }
        result = await dry_run_wake(payload)
        threshold = cast(dict[str, Any], _automatic(result)["threshold"])
        assert threshold["closed"] is closed and threshold["value"] == value
        assert threshold["source"] == source
        if closed:
            assert "不补偿" in str(threshold["hint"])
            for key in ("decision", "deadline_decision"):
                decision = _decision(result, key)
                assert decision["triggered"] is False and "wake_talk_value=0" in str(decision["reason"])
        else:
            assert threshold["overridden"] is overridden
            assert "被显式 wake_message_threshold 覆盖" in str(threshold["hint"])
            assert _decision(result, "deadline_decision")["rule"] == "max_wait"

    @pytest.mark.asyncio
    async def test_sources_report_layer_and_match_resolved_values(self):
        """来源解析覆盖平台/时段/群与默认值, 数值与 resolved 完全一致"""
        settings: dict[str, object] = {
            "wake_mode": "frequency", "input_text_limit": 500, "wake_talk_value": 0.5,
            "wake_time_rules": [{"start": "09:00", "end": "18:00", "settings": {"wake_cooldown": 90}}],
            "wake_group_overrides": {"20": {"wake_message_threshold": 4}},
        }
        result = await dry_run_wake({"settings": settings, "group_id": "20", "local_time": "14:30",
                                     "steps": [{"text": "hi"}]})
        sources = cast(dict[str, dict[str, Any]], result["sources"])
        resolved = cast(dict[str, Any], result["resolved"])
        assert sources["input_text_limit"]["source"] == "platform"
        assert sources["wake_cooldown"]["source"] == "time_rule"
        assert sources["wake_cooldown"]["source_index"] == 0
        assert "09:00-18:00" in str(sources["wake_cooldown"]["source_label"])
        assert sources["wake_message_threshold"]["source"] == "group"
        assert sources["wake_message_threshold"]["value"] == 4 == resolved["wake_message_threshold"]
        # 未设置的字段展示运行时默认值, 与校验使用的默认值表一致
        assert sources["input_media_limit"] == {
            "value": 8, "source": "builtin_default", "source_index": None, "source_label": "未设置, 使用默认值",
        }
        assert sources["wake_score_threshold"]["value"] == result["defaults"]["wake_score_threshold"]
        for key, entry in sources.items():
            if key in resolved:
                assert entry["value"] == resolved[key]

    @pytest.mark.asyncio
    async def test_sources_are_not_written_into_settings(self):
        """反例: 来源是展示结果, 不得混回草稿或 resolved 里的持久化字段"""
        settings: dict[str, object] = {"wake_mode": "frequency", "wake_talk_value": 0.5}
        result = await dry_run_wake({"settings": settings, "group_id": "20", "local_time": "10:00",
                                     "steps": [{"text": "hi"}]})
        resolved = cast(dict[str, Any], result["resolved"])
        assert "sources" not in resolved and "defaults" not in resolved
        assert not any(key.startswith("source") for key in resolved)
        assert "sources" not in settings and "defaults" not in settings

class TestExplicitPath:
    @pytest.mark.asyncio
    async def test_mention_and_wake_word(self):
        mention = await dry_run_wake({"settings": {"self_id": "10000"}, "probe": {"text": "在吗", "at_self": True}})
        explicit = _explicit(mention)
        assert explicit["rule"] == "mention" and explicit["triggered"] is True
        word = await dry_run_wake({"settings": {"wake_words": ["助手"]}, "probe": {"text": "助手帮我看看"}})
        explicit = _explicit(word)
        assert explicit["rule"] == "wake_word" and explicit["matched"] == "助手"
        miss = await dry_run_wake({"settings": {"wake_words": ["助手"]}, "probe": {"text": "随便聊聊"}})
        explicit = _explicit(miss)
        assert explicit["triggered"] is False and explicit["rule"] == "no_match"

    @pytest.mark.asyncio
    async def test_undetermined_without_probe_and_with_quote(self):
        result = await dry_run_wake({"settings": {}})
        explicit = _explicit(result)
        assert explicit["triggered"] is None and explicit["rule"] == "undetermined"
        assert "未提供探测消息" in str(explicit["reason"])
        quote = await dry_run_wake({"settings": {}, "probe": {"text": "引用回复", "quote_self": True}})
        explicit = _explicit(quote)
        assert explicit["rule"] == "undetermined" and "回源" in str(explicit["reason"])

    @pytest.mark.asyncio
    async def test_undetermined_without_messages(self):
        result = await dry_run_wake({"settings": {"wake_mode": "frequency"}, "steps": [{"submit": True}]})
        decision = _decision(result)
        assert decision["triggered"] is None and decision["rule"] == "undetermined"

class TestValidation:
    @pytest.mark.asyncio
    async def test_invalid_inputs_raise(self):
        with pytest.raises(ValueError, match="JSON 对象"):
            await dry_run_wake([])
        with pytest.raises(ValueError, match="settings"):
            await dry_run_wake({})
        with pytest.raises(ValueError):
            await dry_run_wake({"settings": {"wake_mode": "bogus"}})
        with pytest.raises(ValueError, match="local_time"):
            await dry_run_wake({"settings": {}, "local_time": "25:00"})
        with pytest.raises(ValueError, match="group_id"):
            await dry_run_wake({"settings": {}, "group_id": "abc"})
        with pytest.raises(ValueError, match="64"):
            await dry_run_wake({"settings": {}, "steps": [{"text": "x"}] * 65})
        with pytest.raises(ValueError, match="text"):
            await dry_run_wake({"settings": {}, "steps": [{"text": "  "}]})
        with pytest.raises(ValueError, match="advance_seconds"):
            bad_step: dict[str, object] = {"settings": {}, "steps": [{"text": "x", "advance_seconds": -1}]}
            await dry_run_wake(bad_step)

    @pytest.mark.asyncio
    async def test_dry_run_does_not_leak_state(self):
        settings: dict[str, object] = {"wake_mode": "frequency", "wake_message_threshold": 1}
        payload: dict[str, object] = {"settings": settings, "steps": [{"text": "a"}, {"submit": True}]}
        first = await dry_run_wake(payload)
        second = await dry_run_wake(payload)
        # 两次试算互不污染: 第二次 submit 仍可完整认领 (第一次的冷却没有残留)
        steps = _automatic(second)["steps"]
        assert isinstance(steps, list) and isinstance(steps[1], dict) and steps[1]["claimed"] == 1
        assert first == second

class TestControlRoute:
    @pytest.mark.asyncio
    async def test_dry_run_route(self, monkeypatch: pytest.MonkeyPatch):
        from satrap.core.backend import control_server as control

        def context(body: bytes) -> control._RouteContext:
            reader = asyncio.StreamReader()
            reader.feed_data(body)
            reader.feed_eof()
            raw = f"POST /config/wake-dry-run HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode("utf-8")
            return control._RouteContext("POST", "/config/wake-dry-run", "/config/wake-dry-run", reader, raw)

        import json as jsonlib

        good = jsonlib.dumps({"settings": {"wake_mode": "frequency", "wake_message_threshold": 1}, "steps": [{"text": "hi"}]}).encode()
        response = await control._route_wake_dry_run(context(good))
        assert response is not None
        status, body = response
        assert status == 200 and body["ok"] is True
        bad = jsonlib.dumps({"settings": {"wake_mode": "bogus"}}).encode()
        response = await control._route_wake_dry_run(context(bad))
        assert response is not None
        assert response[0] == 400 and response[1]["ok"] is False
        missed = await control._route_wake_dry_run(
            control._RouteContext("GET", "/config/models", "/config/models", asyncio.StreamReader(), b""),
        )
        assert missed is None

ROOT = Path(__file__).resolve().parents[2]

FIXTURE = ROOT / "tests" / "fixtures" / "wake_policy_cases.json"

GENERATED = ROOT / "satrap-ui" / "src" / "generated" / "wake-policy-contract.json"

FROZEN_HOT_KEYS = frozenset({
    "wake_max_wait", "wake_group_overrides", "wake_time_rules", "wake_score_threshold",
    "wake_question_weight", "wake_address_weight", "wake_backlog_weight", "wake_reply_penalty",
    "wake_mode", "wake_message_threshold", "wake_talk_value", "wake_cooldown", "wake_aliases",
    "wake_words", "group_whitelist", "context_scope", "enable_group", "enable_private",
    "input_text_limit", "input_media_limit", "message_text_limit", "asr_model", "voice_transcribe",
    "attachment_extract", "media_trusted_hosts", "media_insecure_tls", "media_plaintext_http",
    # 命令入口的 operator 名单属平台级热更新字段: 登记后无需重启即可生效
    "command_operators",
})

FROZEN_AUTOMATIC_KEYS = frozenset({
    "wake_mode", "wake_message_threshold", "wake_cooldown", "wake_score_threshold", "wake_max_wait",
    "wake_question_weight", "wake_address_weight", "wake_backlog_weight", "wake_reply_penalty",
    "wake_talk_value",
})

FROZEN_GROUP_KEYS = FROZEN_AUTOMATIC_KEYS | {
    "wake_words", "wake_aliases", "reply_with_quote", "reply_with_mention", "quote_lookup",
    "wake_on_quote_self", "forward_lookup",
}

FROZEN_DEFAULTS: dict[str, object] = {
    "message_text_limit": 2000, "input_text_limit": 20000, "input_media_limit": 8,
    "wake_mode": "explicit", "wake_message_threshold": 3, "wake_cooldown": 30,
    "wake_max_wait": 0, "wake_score_threshold": 0.65, "wake_question_weight": 0.55,
    "wake_address_weight": 0.15, "wake_backlog_weight": 0.30, "wake_reply_penalty": 0.40,
}

def _cases() -> list[dict[str, Any]]:
    """共享样例列表"""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return list(payload["cases"])

def _validate_case(case: Mapping[str, Any]) -> None:
    """按上下文把样例包成真实校验入口接受的结构"""
    context = str(case["context"])
    # 样例取值来自 JSON, 显式标注避免 Any 扩散到校验入口
    value: dict[str, object] = case["value"]
    if context == "platform":
        validate_wake_policy(value)
    elif context == "group":
        validate_wake_policy({"wake_group_overrides": {"123": value}})
    elif context == "time":
        rule: dict[str, object] = {"start": "00:00", "end": "06:00", "settings": value}
        validate_wake_policy({"wake_time_rules": [rule]})
    else:
        raise AssertionError(f"未知上下文: {context}")

async def _event(adapter: OneBotAdapter, text: str = "正文", message_id: str = "1") -> MessageEvent:
    """经真实 OneBot 入站构造事件"""
    await adapter._handle_group_message({
        "self_id": 10, "group_id": 20, "user_id": 30, "message_id": message_id,
        "message_type": "group", "message": [{"type": "text", "data": {"text": text}}],
    })
    return adapter._event_queue.get_nowait()

class _CountingAdapter(OneBotAdapter):
    """不发网络请求、只统计发送次数的适配器替身"""

    def __init__(self, settings: dict[str, object]) -> None:
        super().__init__(PlatformConfig(id="bot", type="onebot", settings=settings))
        # 发送路径要求客户端已初始化, 用替身替代真实连接 (由 _dispatch_action 统计次数)
        self._bot = AsyncMock()
        self.sent: list[str] = []

    async def _dispatch_action(self, session_id: str, private_action: str, group_action: str, **params: Any) -> Any:
        self.sent.append(group_action)
        return {"message_id": len(self.sent)}

class TestFrozenSets:
    def test_frozen_key_sets_match_policy_contract(self):
        # 热更新键/覆盖作用域键/默认值表是前后端共享契约的冻结快照
        assert hot_reload_keys() == FROZEN_HOT_KEYS | {"message_archive_retention_days"}
        assert AUTOMATIC_KEYS == FROZEN_AUTOMATIC_KEYS
        assert GROUP_KEYS == FROZEN_GROUP_KEYS
        assert POLICY_DEFAULTS == {**FROZEN_DEFAULTS, "message_archive_retention_days": 30}
        assert fields_with_scope("time") == AUTOMATIC_KEYS
        assert fields_with_scope("time", "group") == GROUP_KEYS
        assert fields_with_scope("platform") == {
            key for key, field in POLICY_FIELD_CONTRACT.items() if field["scope"] == "platform"
        }

    def test_contract_entries_cover_and_declare_consistent_ranges(self):
        # 表覆盖校验字段/作用域/默认值/热更新与前端编辑字段的并集, 不局限于热更新键
        assert FROZEN_HOT_KEYS | FROZEN_GROUP_KEYS | set(FROZEN_DEFAULTS) <= set(POLICY_FIELD_CONTRACT)
        # 不在旧 hot_keys 内的字段也在表里, 但保持需要重启
        for key in ("reply_with_quote", "quote_lookup", "notice_types"):
            assert POLICY_FIELD_CONTRACT[key]["hot_reload"] is False
        assert "group_whitelist" in POLICY_FIELD_CONTRACT
        assert "context_scope" in POLICY_FIELD_CONTRACT
        kinds = {
            "int", "number", "bool", "enum", "text", "list", "notice_types",
            "words", "group_ids", "scope", "group_map", "time_rules",
        }
        for key, field in POLICY_FIELD_CONTRACT.items():
            assert field["kind"] in kinds, key
            assert field["scope"] in {"platform", "group", "time"}, key
            # 同一字段不同时声明含端点上界与排他上界
            assert not ("max" in field and "max_exclusive" in field), key
            if field["kind"] in {"int", "number"}:
                assert "min" in field or "max" in field or "max_exclusive" in field, key
            if field["kind"] == "enum":
                assert field.get("enum"), key
            if field["kind"] == "int":
                assert field.get("integer") is True, key
            if field["kind"] in {"text", "list"}:
                assert "max_length" in field or "max_items" in field, key

class TestGeneratedContract:
    def test_generated_json_matches_serialization_and_content(self):
        from scripts.sync_wake_policy_contract import build_payload, render

        assert GENERATED.is_file(), "契约 JSON 缺失, 请运行 python scripts/sync_wake_policy_contract.py"
        assert GENERATED.read_bytes() == render(build_payload())
        payload = json.loads(GENERATED.read_text(encoding="utf-8"))
        assert payload["version"] == 2
        # 后端专属 message 字段不得进入前端契约
        for entry in payload["fields"]:
            assert "message" not in entry
        # 命令入口操作员名单必须进入生成契约, 前端编辑器与表单字段由它构建
        entry = next(field for field in payload["fields"] if field["key"] == "command_operators")
        assert entry == {
            "key": "command_operators", "kind": "list", "scope": "platform",
            "hot_reload": True, "display_in_preview": False, "max_items": 32, "max_length": 64,
        }

class TestSharedCases:
    @pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["id"]))
    def test_fixture_case_conclusion(self, case: dict[str, Any]):
        if case["valid"]:
            _validate_case(case)
            return
        with pytest.raises(ValueError):
            _validate_case(case)


    def test_text_length_counts_code_points(self):
        # 与前端 textLength 同一口径: 非 BMP 字符按 1 个码点计, 不按 UTF-16 码元
        assert len("😀" * 128) == 128
        validate_wake_policy({"asr_model": "😀" * 128})
        with pytest.raises(ValueError):
            validate_wake_policy({"asr_model": "😀" * 129})
        validate_wake_policy({"media_trusted_hosts": ["例" * 126 + "😀" * 127]})
        with pytest.raises(ValueError):
            validate_wake_policy({"media_trusted_hosts": ["例" * 126 + "😀" * 128]})

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_numbers_rejected(self, value: float):
        # NaN/Infinity 无法写进共享 JSON, 由两侧各自补测
        for key in ("wake_cooldown", "wake_score_threshold", "wake_max_wait", "wake_talk_value"):
            with pytest.raises(ValueError):
                validate_wake_policy({key: value})

    def test_legacy_cooldown_over_one_day_still_accepted(self):
        # 存量配置不受影响: 冷却不新增上限, 校验与适配器构造都按同一契约
        settings: dict[str, object] = {"wake_cooldown": 90000.0}
        validate_wake_policy(settings)
        adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=dict(settings)))
        assert adapter.config.settings["wake_cooldown"] == 90000.0

    def test_missing_override_field_does_not_gain_default(self):
        # 缺失仍表示继承: 校验不向覆盖对象注入默认值
        override: dict[str, object] = {"wake_mode": "frequency"}
        validate_wake_policy({"wake_group_overrides": {"123": override}})
        assert override == {"wake_mode": "frequency"}

class TestImportOrder:
    @pytest.mark.parametrize(
        "module", ["satrap.core.config.platform_policy", "satrap.core.config.wake_overrides"],
    )
    def test_each_module_imports_first(self, module: str):
        result = subprocess.run(
            [sys.executable, "-c", f"import {module} as m; print(m.__name__)"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert module in result.stdout

class TestPolicyDefaultSingleSource:
    """运行时默认值只能来自契约: 改写 POLICY_DEFAULTS 后消费方行为随之改变"""

    @pytest.mark.asyncio
    async def test_wake_window_decisions_follow_policy_defaults(self, monkeypatch: pytest.MonkeyPatch):
        adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"wake_mode": "frequency"}))
        window = WakeWindow()
        event = await _event(adapter)
        snapshot = window.observe(event, 0)
        window.claim(event, snapshot, False, 0)
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_cooldown", 600)
        assert window.decide(event, snapshot, 1).rule == "cooldown"
        assert window.cooldown_remaining(event, 1) > 0
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_cooldown", 0)
        assert window.cooldown_remaining(event, 1) == 0
        # 冷却不再拦截: 回到阈值判断 (默认阈值 3, 一条正文不触发)
        assert window.decide(event, snapshot, 1).rule == "frequency"

        necessity = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"wake_mode": "necessity"}))
        nwin = WakeWindow()
        nevent = await _event(necessity)
        nsnapshot = nwin.observe(nevent, 0)
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_score_threshold", 0.0)
        assert nwin.decide(nevent, nsnapshot, 1).triggered is True
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_score_threshold", 1.0)
        assert nwin.decide(nevent, nsnapshot, 1).triggered is False

        pending: tuple[PendingMessage, ...] = (
            PendingMessage(request_id="r1", actor_id="30", message_id="1", text="正文", received_at=0.0),
        )
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_max_wait", 0)
        assert window.decide(event, pending, 10, deadline=True).rule != "max_wait"
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_max_wait", 5)
        assert window.decide(event, pending, 10, deadline=True).rule == "max_wait"

    @pytest.mark.asyncio
    async def test_other_consumers_follow_policy_defaults(self, monkeypatch: pytest.MonkeyPatch):
        from satrap.core.pipeline.wake_policy import DEFAULT_MESSAGE_THRESHOLD

        assert DEFAULT_MESSAGE_THRESHOLD == policy_default("wake_message_threshold")

        adapter = _CountingAdapter({"self_id": "10"})
        monkeypatch.setitem(POLICY_DEFAULTS, "message_text_limit", 8)
        await adapter._send_message("group%20", MessageChain([Plain(text="x" * 32)]))
        split_calls = len(adapter.sent)
        adapter.sent.clear()
        monkeypatch.setitem(POLICY_DEFAULTS, "message_text_limit", 4096)
        await adapter._send_message("group%20", MessageChain([Plain(text="x" * 32)]))
        assert split_calls > 1
        assert len(adapter.sent) == 1

        projection_adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={}))
        projection_event = await _event(projection_adapter, "x" * 200)
        monkeypatch.setitem(POLICY_DEFAULTS, "input_text_limit", 10)
        assert len(project_input(projection_event, "none").message) <= 10
        monkeypatch.setitem(POLICY_DEFAULTS, "input_text_limit", 10000)
        assert project_input(projection_event, "none").message == "[用户 30, 消息 1] " + "x" * 200

        payload: dict[str, object] = {"settings": {"self_id": "10"}, "group_id": "20", "steps": [{"text": "正文"}]}
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_mode", "frequency")
        result = await dry_run_wake(payload)
        assert result["automatic"]["mode"] == "frequency"
