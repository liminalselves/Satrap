"""模型上下文策略解析与运行时应用测试"""
from pathlib import Path
from typing import Any, cast

import pytest

from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.core.type import LLMConfig
from satrap.core.utils.context import ContextManager
from satrap.core.utils.context_policy import apply_context_policy, resolve_context_policy
from satrap.edictum import AsyncSimpleSession


class _NoCallAsyncLLM(AsyncLLM):
    """无需真实客户端的异步 LLM 替身"""

    def __init__(self) -> None:
        self.model = "fake"


def test_resolve_and_apply_context_policy(tmp_path: Path):
    """
    策略解析后应一次性更新全部派生预算

    参数:
    - tmp_path: 临时目录
    """
    config = LLMConfig(
        context_window=10000,
        history_ratio=0.6,
        context_strategy="summarize",
        context_threshold=0.75,
        truncation_floor=0.25,
        summary_keep_recent_turns=3,
    )
    context = ContextManager("policy", db_path=str(tmp_path / "context.db"))
    policy = apply_context_policy(context, config)

    assert policy.history_budget == 6000
    assert context.max_context == 10000
    assert context.history_budget == 6000
    assert context.trigger_tokens == 4500
    assert context.floor_tokens == 1500
    assert context.output_budget == 4000
    assert context.exceed_process == "summarize"
    assert context.summary_keep_recent_turns == 3
    context.close()


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (LLMConfig(context_window=0), "context_window"),
        (LLMConfig(history_ratio=0), "history_ratio"),
        (LLMConfig(context_strategy="unknown"), "context_strategy"),
        (LLMConfig(context_threshold=0.4, truncation_floor=0.5), "truncation_floor"),
        (LLMConfig(summary_keep_recent_turns=-1), "summary_keep_recent_turns"),
    ],
)
def test_resolve_context_policy_rejects_invalid_values(config: LLMConfig, message: str):
    """
    非法上下文策略必须在写入运行时前被拒绝

    参数:
    - config: 待校验配置
    - message: 预期错误字段
    """
    with pytest.raises(ValueError, match=message):
        resolve_context_policy(config)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"context_window": True},
        {"context_window": "128000"},
        {"history_ratio": "0.7"},
        {"context_strategy": ["sliding"]},
        {"context_threshold": "0.8"},
        {"truncation_floor": False},
    ],
)
def test_resolve_context_policy_rejects_invalid_types(kwargs: dict[str, object]):
    """
    上下文策略拒绝会导致运行时比较异常的字段类型

    参数:
    - kwargs: 待覆盖的配置字段
    """
    config = LLMConfig(**cast(Any, kwargs))

    with pytest.raises(ValueError):
        resolve_context_policy(config)


@pytest.mark.asyncio
async def test_async_session_workflow_inherits_policy_after_initialization(tmp_path: Path):
    """
    异步会话在工作流创建前应用的策略必须由后创建的主工作流继承

    参数:
    - tmp_path: 临时目录
    """
    config = LLMConfig(
        context_window=20000,
        history_ratio=0.5,
        context_strategy="mid_truncate",
        context_threshold=0.7,
        truncation_floor=0.2,
        summary_keep_recent_turns=2,
    )
    session = AsyncSimpleSession(
        "async-policy",
        _NoCallAsyncLLM(),
        db_path=str(tmp_path / "context.db"),
        enable_checkpoint=False,
    )
    session.apply_context_config(config)
    await session.initialize()

    assert session.session_ctx.exceed_process == "mid_truncate"
    assert session.ctx.exceed_process == "mid_truncate"
    assert session.ctx.history_budget == 10000
    assert session.ctx.trigger_tokens == 7000
    assert session.ctx.floor_tokens == 2000
    assert session.ctx.summary_keep_recent_turns == 2
