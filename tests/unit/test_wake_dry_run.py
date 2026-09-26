"""唤醒策略试算: 频率/必要性/窗口/冷却/最长等待覆盖, 显式路径与无法判断分支, 输入校验与路由"""
from __future__ import annotations

from typing import Any, cast

import asyncio
import pytest

from satrap.core.pipeline.wake_dry_run import dry_run_wake


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
    async def test_zero_talk_value_closes_automatic_and_deadline(self):
        """反例: 试算的到期分支同样不得被 talk_value=0 绕过"""
        settings: dict[str, object] = {"wake_mode": "frequency", "wake_talk_value": 0, "wake_max_wait": 30}
        payload: dict[str, object] = {
            "settings": settings, "steps": [{"text": "积压"}, {"text": "还在", "advance_seconds": 40}],
        }
        result = await dry_run_wake(payload)
        threshold = cast(dict[str, Any], _automatic(result)["threshold"])
        assert threshold["closed"] is True and threshold["value"] is None
        assert threshold["source"] == "talk_value" and "不补偿" in str(threshold["hint"])
        for key in ("decision", "deadline_decision"):
            decision = _decision(result, key)
            assert decision["triggered"] is False and "wake_talk_value=0" in str(decision["reason"])

    @pytest.mark.asyncio
    async def test_explicit_threshold_overrides_talk_value_hint(self):
        """反例: 显式阈值存在时不得把 talk_value=0 显示成关闭, 要显示被覆盖"""
        settings: dict[str, object] = {
            "wake_mode": "frequency", "wake_talk_value": 0, "wake_message_threshold": 2, "wake_max_wait": 30,
        }
        payload: dict[str, object] = {
            "settings": settings, "steps": [{"text": "积压"}, {"text": "还在", "advance_seconds": 40}],
        }
        result = await dry_run_wake(payload)
        threshold = cast(dict[str, Any], _automatic(result)["threshold"])
        assert threshold["overridden"] is True and threshold["closed"] is False
        assert threshold["value"] == 2 and threshold["source"] == "explicit"
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
