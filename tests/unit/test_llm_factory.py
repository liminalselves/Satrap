"""
build_llm_from_config 统一 LLM 构造工厂测试

覆盖:
- 全字段映射 (含 top_p / lock_api_key / reasoning_body / thinking_fields)
- 输出预算 = context_window x (1 - history_ratio), 优先于 max_tokens
- max_tokens 缺省回退 4096
- sync / async 两种构造
- 测试替身 (SimpleNamespace 缺字段) 兼容
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from satrap.core.APICall.LLMCall import AsyncLLM, LLM, build_llm_from_config
from satrap.core.type import LLMConfig


def _cfg(**kw: Any) -> LLMConfig:
    base: dict[str, Any] = dict(name="m", model="mod", base_url="http://x/v1", api_key="k")
    base.update(kw)
    return LLMConfig(**base)


def test_maps_all_fields():
    """全部字段透传, lock_api_key=False 时不锁定 api_key"""
    llm = build_llm_from_config(
        _cfg(
            temperature=0.3,
            top_p=0.9,
            max_tokens=512,
            lock_api_key=False,
            reasoning_body={"thinking": {"type": "enabled"}},
            thinking_field_name="reasoning",
            thinking_fields=["reasoning_effort"],
        )
    )
    assert isinstance(llm, LLM)
    assert llm.temperature == 0.3
    assert llm.top_p == 0.9
    assert llm.max_tokens == 512
    assert llm.api_key == "k"
    assert llm.reasoning_body == {"thinking": {"type": "enabled"}}
    assert llm.thinking_field_name == "reasoning"
    assert llm.thinking_fields == ["reasoning_effort"]


def test_output_budget_wins_over_max_tokens():
    """context_window + history_ratio 配置时输出预算优先于 max_tokens"""
    llm = build_llm_from_config(_cfg(context_window=128000, history_ratio=0.7, max_tokens=512))
    assert isinstance(llm, LLM)
    assert llm.max_tokens == int(128000 * 0.3)


def test_max_tokens_fallback_4096():
    """无输出预算且未配置 max_tokens 时回退 4096"""
    llm = build_llm_from_config(_cfg())
    assert isinstance(llm, LLM)
    assert llm.max_tokens == 4096


def test_max_tokens_zero_preserved():
    """显式 max_tokens=0 不被 or 吞掉"""
    llm = build_llm_from_config(_cfg(max_tokens=0))
    assert isinstance(llm, LLM)
    assert llm.max_tokens == 0


def test_async_variant():
    llm = build_llm_from_config(_cfg(max_tokens=100), async_=True)
    assert isinstance(llm, AsyncLLM)
    assert llm.max_tokens == 100


def test_simple_namespace_substitute():
    """缺字段替身 (SimpleNamespace) 也能构造, 用 LLM 默认值"""
    ns = SimpleNamespace(api_key="k", base_url="http://x", model="m")
    llm = build_llm_from_config(ns)   # type: ignore[arg-type]
    assert isinstance(llm, LLM)
    assert llm.max_tokens == 4096
    assert llm.temperature == 0.7
