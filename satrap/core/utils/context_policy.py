"""模型上下文策略解析与运行时应用服务"""
from __future__ import annotations

from dataclasses import dataclass
from numbers import Real

from satrap.core.type import LLMConfig
from satrap.core.utils.context import AsyncContextManager, ContextManager


CONTEXT_STRATEGIES = ("sliding", "mid_truncate", "summarize")
DEFAULT_CONTEXT_WINDOW = 128000
DEFAULT_HISTORY_RATIO = 0.7
DEFAULT_CONTEXT_THRESHOLD = 0.8
DEFAULT_TRUNCATION_FLOOR = 0.4
DEFAULT_SUMMARY_KEEP_RECENT_TURNS = 6


@dataclass(frozen=True)
class ResolvedContextPolicy:
    """已补齐默认值并通过校验的模型上下文策略"""
    context_window: int
    history_ratio: float
    context_strategy: str
    context_threshold: float
    truncation_floor: float
    summary_keep_recent_turns: int

    @property
    def history_budget(self) -> int:
        """返回历史上下文 token 预算"""
        return int(self.context_window * self.history_ratio)

    @property
    def trigger_tokens(self) -> int:
        """返回触发上下文处理的 token 数"""
        return int(self.history_budget * self.context_threshold)

    @property
    def floor_tokens(self) -> int:
        """返回处理后的目标 token 数"""
        return int(self.history_budget * self.truncation_floor)

    @property
    def output_budget(self) -> int:
        """返回为模型输出保留的 token 预算"""
        return self.context_window - self.history_budget


def resolve_context_policy(config: LLMConfig) -> ResolvedContextPolicy:
    """
    解析并校验模型上下文策略

    参数:
    - config: LLM 配置

    返回:
    - ResolvedContextPolicy: 已补齐默认值的上下文策略
    """
    context_window = config.context_window if config.context_window is not None else DEFAULT_CONTEXT_WINDOW
    history_ratio = config.history_ratio if config.history_ratio is not None else DEFAULT_HISTORY_RATIO
    strategy = config.context_strategy or "sliding"
    threshold = config.context_threshold
    floor = config.truncation_floor
    keep_recent = config.summary_keep_recent_turns

    if isinstance(context_window, bool) or not isinstance(context_window, int):
        raise ValueError("context_window 必须是整数")
    if isinstance(history_ratio, bool) or not isinstance(history_ratio, Real):
        raise ValueError("history_ratio 必须是数字")
    if not isinstance(strategy, str):
        raise ValueError("context_strategy 必须是字符串")
    if isinstance(threshold, bool) or not isinstance(threshold, Real):
        raise ValueError("context_threshold 必须是数字")
    if isinstance(floor, bool) or not isinstance(floor, Real):
        raise ValueError("truncation_floor 必须是数字")
    if context_window <= 0:
        raise ValueError("context_window 必须大于 0")
    if not 0 < history_ratio <= 1:
        raise ValueError("history_ratio 必须在 (0, 1] 区间")
    if strategy not in CONTEXT_STRATEGIES:
        raise ValueError(f"不支持的 context_strategy: {strategy}")
    if not 0 < floor < threshold <= 1:
        raise ValueError("上下文比例必须满足 0 < truncation_floor < context_threshold <= 1")
    if isinstance(keep_recent, bool) or not isinstance(keep_recent, int) or keep_recent < 0:
        raise ValueError("summary_keep_recent_turns 必须是大于等于 0 的整数")

    policy = ResolvedContextPolicy(
        context_window=context_window,
        history_ratio=float(history_ratio),
        context_strategy=strategy,
        context_threshold=float(threshold),
        truncation_floor=float(floor),
        summary_keep_recent_turns=keep_recent,
    )
    if policy.floor_tokens <= 0 or policy.floor_tokens >= policy.trigger_tokens:
        raise ValueError("上下文预算过小, 无法形成有效的触发和压缩目标")
    return policy


def apply_context_policy(
    context: ContextManager | AsyncContextManager,
    config: LLMConfig,
) -> ResolvedContextPolicy:
    """
    将模型上下文策略原子应用到运行时上下文管理器

    参数:
    - context: 同步或异步上下文管理器
    - config: LLM 配置

    返回:
    - ResolvedContextPolicy: 实际应用的上下文策略
    """
    policy = resolve_context_policy(config)
    context.max_context = policy.context_window
    context.history_ratio = policy.history_ratio
    context.context_threshold = policy.context_threshold
    context.truncation_floor = policy.truncation_floor
    context.exceed_process = policy.context_strategy
    context.summary_keep_recent_turns = policy.summary_keep_recent_turns
    context.history_budget = policy.history_budget
    context.trigger_tokens = policy.trigger_tokens
    context.floor_tokens = policy.floor_tokens
    context.output_budget = policy.output_budget
    return policy
