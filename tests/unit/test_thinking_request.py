"""LLM 思考请求字段构造测试"""
from __future__ import annotations

from satrap.core.APICall.LLMCall import _build_thinking_extra_body


def test_off_keeps_none_fields_by_default():
    """默认保留供应商使用的 none 关闭值"""
    body = _build_thinking_extra_body(
        "off",
        ["thinking.type", "reasoning_effort", "enable_thinking"],
    )

    assert body == {
        "thinking": {"type": "disabled"},
        "reasoning_effort": "none",
        "enable_thinking": False,
    }


def test_off_can_omit_none_fields_without_dropping_explicit_switches():
    """开启省略选项后只删除 none 字段并保留显式关闭开关"""
    body = _build_thinking_extra_body(
        "off",
        ["thinking.type", "reasoning_effort", "thinking_level", "enable_thinking"],
        omit_none_thinking_fields=True,
    )

    assert body == {
        "thinking": {"type": "disabled"},
        "enable_thinking": False,
    }


def test_enabled_level_is_not_affected_by_none_omission():
    """开启思考时应完整发送开关和强度"""
    body = _build_thinking_extra_body(
        "low",
        ["thinking.type", "reasoning_effort", "thinking_level", "enable_thinking"],
        omit_none_thinking_fields=True,
    )

    assert body == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
        "thinking_level": "low",
        "enable_thinking": True,
    }


def test_extended_levels_are_forwarded_unchanged():
    """扩展思考强度应由强度字段原样传给供应商"""
    for level in ("xhigh", "max", "ultra"):
        body = _build_thinking_extra_body(level, ["reasoning_effort", "thinking_level"])
        assert body == {"reasoning_effort": level, "thinking_level": level}


def test_empty_effective_field_set_returns_none():
    """全部字段被忽略或省略时不发送 extra_body"""
    body = _build_thinking_extra_body(
        "off",
        ["reasoning_effort", "unknown"],
        omit_none_thinking_fields=True,
    )

    assert body is None
