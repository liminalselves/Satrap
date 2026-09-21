"""群级和时段策略覆盖的优先级及边界"""
from datetime import datetime

import pytest

from satrap.core.config.wake_overrides import resolve_wake_settings
from satrap.core.config.platform_policy import validate_wake_policy


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
