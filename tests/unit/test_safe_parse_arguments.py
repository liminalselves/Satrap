"""工具调用参数容错解析测试"""
from __future__ import annotations

import pytest

from satrap.core.utils import safe_parse_arguments


def test_safe_parse_arguments_returns_dict_for_valid_json_object() -> None:
    """标准 JSON 对象应原样解析"""
    assert safe_parse_arguments('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_safe_parse_arguments_accepts_dict_passthrough() -> None:
    """字典入参应直接透传"""
    source = {"a": 1}
    assert safe_parse_arguments(source) == {"a": 1}


@pytest.mark.parametrize("raw", ["[]", "[1, 2]", "123", "true", "false", "null", '"text"'])
def test_safe_parse_arguments_normalizes_non_dict_json_to_empty(raw: str) -> None:
    """非字典 JSON (列表/数字/布尔/null/字符串) 应归一为空字典"""
    assert safe_parse_arguments(raw) == {}


def test_safe_parse_arguments_rejects_non_string_keys() -> None:
    """含非字符串键的字典应归一为空字典"""
    assert safe_parse_arguments("{1: 2}") == {}


def test_safe_parse_arguments_literal_eval_fallback() -> None:
    """单引号 Python 字面量应经 literal_eval 兜底解析"""
    assert safe_parse_arguments("{'a': 1}") == {"a": 1}


def test_safe_parse_arguments_repairs_bare_newline_in_string() -> None:
    """字符串字段内的裸换行应被修复后解析"""
    assert safe_parse_arguments('{"k": "v1\nv2"}') == {"k": "v1\nv2"}


def test_safe_parse_arguments_returns_empty_for_garbage() -> None:
    """完全无法解析的输入应返回空字典"""
    assert safe_parse_arguments("not json at all") == {}
