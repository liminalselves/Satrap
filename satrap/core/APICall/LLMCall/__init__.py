"""大语言模型 API 兼容入口, 保留原模块的类与函数导出"""

from typing import Any

from satrap.core.type import (
    LLMConfig,
    LLMCallResponse,
    LLMCallStreamEvent,
    TokenUsage,
    safe_getattr,
    safe_getattr_str,
)
from .responses import (
    parse_chat_response,
    parse_call_response,
    _response_content_text,
    _extract_thinking_from_message,
    _usage_int,
    _extract_token_usage,
)
from .async_ import AsyncLLM
from .stream import (
    _StreamCallAccumulator,
    _stream_field,
    _stream_text,
    _stream_first_text,
)
from .utils import (
    _compute_output_budget,
    _as_message_params,
    _as_tool_params,
    _as_tool_choice_param,
    _stream_usage_option_unsupported,
    _rename_thinking_field,
    _build_thinking_extra_body,
    _THINKING_FIELD_MAP,
)
from .sync import LLM

__all__ = [
    "LLM",
    "AsyncLLM",
    "build_llm_from_config",
    "parse_chat_response",
    "parse_call_response",
]


def build_llm_from_config(cfg: LLMConfig, *, async_: bool = False) -> "LLM | AsyncLLM":
    """
    由 LLMConfig 统一构造 LLM / AsyncLLM (字段映射 + 输出预算)

    统一映射全部字段并应用输出预算, 消除各调用方自行构造时的遗漏:
    - 输出预算 = context_window x (1 - history_ratio), 两者都配置时优先于 max_tokens
    - max_tokens = 输出预算 or cfg.max_tokens or 4096
    - top_p / lock_api_key / thinking_field_name / thinking_fields / omit_none_thinking_fields 全部透传
    - 用 safe_getattr 兼容测试替身 (SimpleNamespace 可能缺字段)

    参数:
    - cfg: LLMConfig (或含同名字段的替身对象)
    - async_: True 返回 AsyncLLM, False 返回 LLM

    返回:
    - 'LLM | AsyncLLM': 由 LLMConfig 统一构造 LLM / AsyncLLM (字段映射 + 输出预算)
    """
    cls = AsyncLLM if async_ else LLM
    kwargs: dict[str, Any] = {
        "api_key": safe_getattr_str(cfg, "api_key"),
        "base_url": safe_getattr_str(cfg, "base_url"),
        "model": safe_getattr_str(cfg, "model"),
        "lock_api_key": safe_getattr(cfg, "lock_api_key", True),
        "allow_insecure_base_url": safe_getattr(cfg, "allow_insecure_base_url", False),
    }
    # 可选字段: 仅非 None 时透传, 避免覆盖 LLM 类默认值 (temperature=0 也需保留)
    temperature = safe_getattr(cfg, "temperature")
    if temperature is not None:
        kwargs["temperature"] = temperature
    budget = _compute_output_budget(cfg)
    if budget is not None:
        kwargs["max_tokens"] = budget
    else:
        max_tokens = safe_getattr(cfg, "max_tokens")
        kwargs["max_tokens"] = max_tokens if max_tokens is not None else 4096
    for name in (
        "top_p",
        "thinking_field_name",
        "supports_visual_input", "thinking_fields",
        "omit_none_thinking_fields",
    ):
        val = safe_getattr(cfg, name)
        if val is not None:
            kwargs[name] = val
    return cls(**kwargs)
