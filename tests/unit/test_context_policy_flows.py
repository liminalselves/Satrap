"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
import pytest
from typing import Any, cast
from satrap.core.utils.context_policy import apply_context_policy, resolve_context_policy
from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.core.utils.context import ContextManager
from satrap.core.type import LLMConfig
from satrap.edictum import AsyncSimpleSession
from unittest.mock import MagicMock, AsyncMock
from satrap.core.utils.context import AsyncContextManager
from satrap.core.type import TokenUsage
from satrap.core.pipeline.input_projection import ProjectionBudget, _ContextBlock, project_input, resolve_quotes
from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform import PlatformConfig
from satrap.core.framework.providers import BindingState, BindingStatus
import asyncio
from dataclasses import replace
from unittest.mock import Mock
from satrap.core.components import Image
from satrap.core.pipeline.input_projection import select_media, select_window_media
from satrap.core.pipeline.wake_window import PendingImage, WakeWindow
from satrap.core.platform.event import MessageEvent
from satrap.core.platform.identity import BotIdentity
from satrap.core.type import UserCall

class _NoCallAsyncLLM(AsyncLLM):
    """无需真实客户端的异步 LLM 替身"""

    def __init__(self) -> None:
        self.model = "fake"

def test_resolve_and_apply_context_policy(tmp_path: Path):
    """策略解析后应一次性更新全部派生预算"""
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
    """非法上下文策略必须在写入运行时前被拒绝"""
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
    """上下文策略拒绝会导致运行时比较异常的字段类型"""
    config = LLMConfig(**cast(Any, kwargs))

    with pytest.raises(ValueError):
        resolve_context_policy(config)

@pytest.mark.asyncio
async def test_async_session_workflow_inherits_policy_after_initialization(tmp_path: Path):
    """异步会话在工作流创建前应用的策略必须由后创建的主工作流继承"""
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

@pytest.fixture
def cm(tmp_path: Path) -> ContextManager:
    return ContextManager("test_conv", db_path=str(tmp_path / "test.db"))

class TestHysteresisParams:
    """三参数与滞回截断派生值"""

    def test_default_derived_values(self, cm: ContextManager):
        assert cm.max_context == 128000
        assert cm.history_ratio == 0.7
        assert cm.context_threshold == 0.8
        assert cm.truncation_floor == 0.4
        assert cm.history_budget == int(128000 * 0.7)   # 历史预算为 89600
        assert cm.trigger_tokens == int(89600 * 0.8)   # 触发阈值为 71680
        assert cm.floor_tokens == int(89600 * 0.4)   # 截断底线为 35840
        assert cm.output_budget == 128000 - 89600   # 输出预算为 38400

    def test_custom_derived_values(self, tmp_path: Path):
        cm = ContextManager(
            "test_custom",
            db_path=str(tmp_path / "test.db"),
            max_context=400000,
            history_ratio=0.7,
            context_threshold=0.8,
            truncation_floor=0.4,
        )
        assert cm.history_budget == 280000
        assert cm.trigger_tokens == 224000
        assert cm.floor_tokens == 112000
        assert cm.output_budget == 120000

    def test_floor_must_be_less_than_threshold(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING"):
            ContextManager(
                "test_invalid",
                db_path=str(tmp_path / "test.db"),
                truncation_floor=0.8,
                context_threshold=0.4,
            )
        assert "截断底线必须小于上下文阈值" in caplog.text

    def test_history_ratio_must_be_positive(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING"):
            ContextManager(
                "test_invalid_ratio",
                db_path=str(tmp_path / "test.db"),
                history_ratio=0.0,
            )
        assert "历史上下文比例必须在" in caplog.text

    def test_history_ratio_must_not_exceed_one(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING"):
            ContextManager(
                "test_invalid_ratio2",
                db_path=str(tmp_path / "test.db"),
                history_ratio=1.1,
            )
        assert "历史上下文比例必须在" in caplog.text

class TestHysteresisTruncation:
    """滞回截断行为"""

    def test_no_truncation_below_trigger(self, cm: ContextManager):
        for i in range(5):
            cm.add_user_message(f"消息 {i}")
            cm.add_bot_message(f"回复 {i}")
        # 填充少量消息, 远低于触发线
        result = cm.get_model_context()
        assert len(result) == 10   # 共 5 条 user 消息和 5 条 assistant 消息

    def test_truncation_stops_at_floor_not_trigger(self, tmp_path: Path):
        """截断后 token 数应 <= floor_tokens 而非 <= trigger_tokens"""
        cm = ContextManager(
            "test_trunc",
            db_path=str(tmp_path / "test.db"),
            max_context=1000,
            history_ratio=0.7,
            context_threshold=0.8,
            truncation_floor=0.4,
        )
        # 触发阈值为 560, 截断底线为 280
        assert cm.trigger_tokens == 560
        assert cm.floor_tokens == 280

        for i in range(50):
            cm.add_user_message(f"用户消息 {i} " + "x" * 50)
            cm.add_bot_message(f"助手回复 {i} " + "y" * 50)
        # 填充大量消息使总 token 超触发线

        result = cm.get_model_context()
        result_tokens = cm.estimate_token(result)
        # 截断后应 <= floor (280), 而非 <= trigger (560)
        assert result_tokens <= cm.floor_tokens

class TestSummarizeAndCompress:
    """总结压缩"""

    def test_compress_preserves_full_history_and_builds_summary_view(self, tmp_path: Path):
        cm = ContextManager("test_sum", db_path=str(tmp_path / "test.db"))
        cm.reset_system_prompt("你是一个助手")

        for i in range(5):
            cm.add_user_message(f"问题 {i}")
            cm.add_bot_message(f"回答 {i}")
        # 构造 5 轮对话

        mock_llm = MagicMock()
        mock_llm.chat.return_value = "用户问了5个问题, 分别是问题0到问题4"

        original_messages = [dict(message) for message in cm.get_context()]
        summary = cm.summarize_and_compress(mock_llm, keep_recent_turns=2)

        assert summary == "用户问了5个问题, 分别是问题0到问题4"
        assert mock_llm.chat.call_args.kwargs["thinking"] == "off"
        assert mock_llm.chat.call_args.kwargs["max_tokens"] >= 512
        # 完整历史不变, 模型视图仅保留摘要与最近 2 轮
        assert cm.get_context() == original_messages
        messages = cm._summary_context(cm.get_context(), keep_recent_turns=2)
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert len(user_msgs) == 2
        assert "问题 3" in str(user_msgs[0].get("content", ""))
        assert "问题 4" in str(user_msgs[1].get("content", ""))
        # system prompt 含摘要区块
        system_msgs = [m for m in messages if m.get("role") == "system"]
        assert len(system_msgs) == 2
        assert any("<对话历史摘要>" in str(message.get("content", "")) for message in system_msgs)
        assert any("用户问了5个问题" in str(message.get("content", "")) for message in system_msgs)

    def test_summary_retries_with_extended_output_budget(self, tmp_path: Path):
        """摘要模型只返回空正文时应使用扩展输出预算重试"""
        cm = ContextManager("test_summary_retry", db_path=str(tmp_path / "test.db"))
        for index in range(3):
            cm.add_user_message(f"问题 {index}")
            cm.add_bot_message(f"回答 {index}")
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = ["", "重试后的摘要"]

        summary = cm.summarize_and_compress(mock_llm, keep_recent_turns=1)

        assert summary == "重试后的摘要"
        assert mock_llm.chat.call_count == 2
        assert mock_llm.chat.call_args.kwargs["max_tokens"] == 4096

    def test_prepare_summary_is_automatic_and_non_destructive(self, tmp_path: Path):
        """超过触发线时应自动总结, 但完整历史不能改变"""
        cm = ContextManager(
            "test_prepare_summary",
            db_path=str(tmp_path / "test.db"),
            max_context=10000,
            exceed_process="summarize",
        )
        for i in range(10):
            cm.add_user_message(f"问题 {i} " + "甲" * 1200)
            cm.add_bot_message(f"回答 {i} " + "乙" * 1200)
        original_messages = [dict(message) for message in cm.get_context()]
        mock_llm = MagicMock()
        mock_llm.model = "summary-model"
        mock_llm.chat.return_value = "旧对话摘要"

        prepared = cm.prepare_model_context(llm=mock_llm, keep_recent_turns=2)

        assert prepared.compressed is True
        assert prepared.strategy == "summarize"
        assert cm.get_context() == original_messages
        assert len([m for m in prepared.messages if m.get("role") == "user"]) == 2
        assert any("<对话历史摘要>" in str(m.get("content")) for m in prepared.messages)

    def test_prepare_tightens_oversized_summary_before_failing(self, tmp_path: Path):
        """摘要过长时应先二次压缩, 最近轮次本身超限才报错"""
        cm = ContextManager(
            "test_tight_summary",
            db_path=str(tmp_path / "test.db"),
            max_context=5000,
            exceed_process="summarize",
            summary_keep_recent_turns=1,
        )
        for i in range(5):
            cm.add_user_message(f"问题 {i} " + "甲" * 400)
            cm.add_bot_message(f"回答 {i} " + "乙" * 400)
        mock_llm = MagicMock()
        mock_llm.model = "summary-model"

        def _chat(messages: list[dict[str, str]], **_: Any) -> str:
            prompt = messages[0]["content"]
            return "精简摘要" if "进一步压缩" in prompt else "冗长摘要" * 2000

        mock_llm.chat.side_effect = _chat

        prepared = cm.prepare_model_context(llm=mock_llm)

        assert prepared.effective_input_tokens <= cm.history_budget
        assert cm._runtime_state.summary == "精简摘要"
        assert any("进一步压缩" in call.args[0][0]["content"] for call in mock_llm.chat.call_args_list)

    def test_api_usage_calibrates_same_model_estimate(self, tmp_path: Path):
        """真实 input usage 应校准同模型的下一次请求估算"""
        cm = ContextManager("test_usage", db_path=str(tmp_path / "test.db"))
        cm.add_user_message("测试消息")
        mock_llm = MagicMock()
        mock_llm.model = "usage-model"
        first = cm.prepare_model_context(llm=mock_llm)
        cm.record_model_usage(
            first,
            TokenUsage(
                input_tokens=first.estimated_input_tokens * 2,
                output_tokens=4,
                total_tokens=first.estimated_input_tokens * 2 + 4,
            ),
        )

        second = cm.prepare_model_context(llm=mock_llm)

        assert second.token_source == "api_calibrated"
        assert second.effective_input_tokens == second.estimated_input_tokens * 2

    def test_request_estimate_includes_tools_and_pending_messages(self, tmp_path: Path):
        """工具定义和临时工具结果必须进入当前请求估算"""
        cm = ContextManager("test_request_estimate", db_path=str(tmp_path / "test.db"))
        cm.add_user_message("开始")
        base = cm.prepare_model_context()
        prepared = cm.prepare_model_context(
            pending_messages=[{"role": "tool", "tool_call_id": "call-1", "content": "结果" * 200}],
            tools=[{"type": "function", "function": {"name": "search", "description": "搜索" * 100}}],
        )

        assert prepared.original_estimated_input_tokens > base.original_estimated_input_tokens

    def test_compress_with_multimodal_projects_images(self, tmp_path: Path):
        cm = ContextManager("test_mm", db_path=str(tmp_path / "test.db"))
        cm.reset_system_prompt("助手")

        cm.add_user_message("看这张图", img_urls=["http://example.com/img.jpg"])
        # 构造含多模态的消息
        cm.add_bot_message("我看到图片了")
        cm.add_user_message("什么问题")
        cm.add_bot_message("回答")

        mock_llm = MagicMock()
        mock_llm.chat.return_value = "用户分享了一张图片"

        cm.summarize_and_compress(mock_llm, keep_recent_turns=1)

        sent_messages = mock_llm.chat.call_args_list[0][0][0]
        # 传给 LLM 的文本中图片已投影为 [图片] (chat 接收 messages 列表)
        # mock_llm 是 MagicMock, call_args 内容无类型, cast 收窄为 str
        sent_text = cast(str, sent_messages[0]["content"]) if isinstance(sent_messages, list) else str(sent_messages)
        assert "[图片]" in sent_text
        assert "image_url" not in sent_text

    def test_compress_incrementally_merges_cached_summary(self, tmp_path: Path):
        cm = ContextManager("test_merge", db_path=str(tmp_path / "test.db"))
        cm.reset_system_prompt("助手")

        for i in range(4):
            cm.add_user_message(f"问题 {i}")
            cm.add_bot_message(f"回答 {i}")

        mock_llm = MagicMock()
        mock_llm.chat.side_effect = ["前两轮摘要", "前三轮摘要"]

        cm.summarize_and_compress(mock_llm, keep_recent_turns=2)
        cm.add_user_message("问题 4")
        cm.add_bot_message("回答 4")
        cm.summarize_and_compress(mock_llm, keep_recent_turns=2)

        assert mock_llm.chat.call_count == 2
        second_prompt = mock_llm.chat.call_args_list[1][0][0][0]["content"]
        assert "已有摘要:\n前两轮摘要" in second_prompt
        assert "问题 2" in second_prompt
        assert "问题 0" not in second_prompt

    def test_compress_insufficient_turns_returns_empty(self, tmp_path: Path):
        cm = ContextManager("test_few", db_path=str(tmp_path / "test.db"))
        cm.add_user_message("问题 0")
        cm.add_bot_message("回答 0")

        mock_llm = MagicMock()
        result = cm.summarize_and_compress(mock_llm, keep_recent_turns=5)
        assert result == ""
        mock_llm.chat.assert_not_called()

    def test_compress_without_system_prompt(self, tmp_path: Path):
        cm = ContextManager("test_nosys", db_path=str(tmp_path / "test.db"))
        for i in range(4):
            cm.add_user_message(f"问题 {i}")
            cm.add_bot_message(f"回答 {i}")

        mock_llm = MagicMock()
        mock_llm.chat.return_value = "总结"

        cm.summarize_and_compress(mock_llm, keep_recent_turns=2)

        assert [m for m in cm.get_context() if m.get("role") == "system"] == []
        model_context = cm._summary_context(cm.get_context(), keep_recent_turns=2)
        system_msgs = [m for m in model_context if m.get("role") == "system"]
        assert len(system_msgs) == 1
        assert "<对话历史摘要>" in str(system_msgs[0].get("content", ""))

    def test_summary_cache_persists_and_edit_invalidates_it(self, tmp_path: Path):
        """摘要可跨实例复用, 删除历史后必须按明确规则失效"""
        db_path = str(tmp_path / "test.db")
        cm = ContextManager("test_summary_state", db_path=db_path)
        for i in range(4):
            cm.add_user_message(f"问题 {i}")
            cm.add_bot_message(f"回答 {i}")
        mock_llm = MagicMock()
        mock_llm.chat.return_value = "持久化摘要"
        cm.summarize_and_compress(mock_llm, keep_recent_turns=2)
        cm.close()

        loaded = ContextManager("test_summary_state", db_path=db_path)
        assert len([m for m in loaded.get_context() if m.get("role") == "user"]) == 4
        assert loaded._runtime_state.summary == "持久化摘要"
        loaded.del_last_chat()
        loaded.close()

        edited = ContextManager("test_summary_state", db_path=db_path)
        assert edited._runtime_state.summary == ""
        assert edited._runtime_state.covered_turn_count == 0

class TestAsyncContextManager:
    """异步版三参数与滞回截断"""

    @pytest.mark.asyncio
    async def test_async_derived_values(self, tmp_path: Path):
        async with AsyncContextManager(
            "test_async",
            db_path=str(tmp_path / "test.db"),
            max_context=400000,
            history_ratio=0.7,
            context_threshold=0.8,
            truncation_floor=0.4,
        ) as cm:
            assert cm.history_budget == 280000
            assert cm.trigger_tokens == 224000
            assert cm.floor_tokens == 112000
            assert cm.output_budget == 120000

    @pytest.mark.asyncio
    async def test_async_invalid_params(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING"):
            AsyncContextManager(
                "test_async_invalid",
                db_path=str(tmp_path / "test.db"),
                truncation_floor=0.9,
                context_threshold=0.4,
            )
        assert "截断底线必须小于上下文阈值" in caplog.text

    @pytest.mark.asyncio
    async def test_async_summarize_and_compress(self, tmp_path: Path):
        async with AsyncContextManager(
            "test_async_sum",
            db_path=str(tmp_path / "test.db"),
        ) as cm:
            await cm.reset_system_prompt("助手")
            for i in range(4):
                await cm.add_user_message(f"问题 {i}")
                await cm.add_bot_message(f"回答 {i}")

            mock_llm = MagicMock()
            mock_llm.chat = AsyncMock(return_value="异步总结")

            summary = await cm.summarize_and_compress(mock_llm, keep_recent_turns=2)

            assert summary == "异步总结"
            assert mock_llm.chat.call_args.kwargs["thinking"] == "off"
            assert mock_llm.chat.call_args.kwargs["max_tokens"] >= 512
            assert len([m for m in cm.get_context() if m.get("role") == "user"]) == 4
            model_context = cm._summary_context(cm.get_context(), keep_recent_turns=2)
            user_msgs = [m for m in model_context if m.get("role") == "user"]
            assert len(user_msgs) == 2

    @pytest.mark.asyncio
    async def test_async_summary_retries_with_extended_output_budget(self, tmp_path: Path):
        """异步摘要模型只返回空正文时应使用扩展输出预算重试"""
        async with AsyncContextManager(
            "test_async_summary_retry",
            db_path=str(tmp_path / "test.db"),
        ) as cm:
            for index in range(3):
                await cm.add_user_message(f"问题 {index}")
                await cm.add_bot_message(f"回答 {index}")
            mock_llm = MagicMock()
            mock_llm.chat = AsyncMock(side_effect=["", "异步重试后的摘要"])

            summary = await cm.summarize_and_compress(mock_llm, keep_recent_turns=1)

            assert summary == "异步重试后的摘要"
            assert mock_llm.chat.await_count == 2
            assert mock_llm.chat.call_args.kwargs["max_tokens"] == 4096

async def make_event(settings: dict[str, object], segments: list[dict[str, object]]):
    """构造已通过适配器入站的群消息事件"""
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings)))
    adapter._bot = AsyncMock()
    await adapter._handle_group_message({
        "self_id": 10000, "user_id": 123, "group_id": 456, "message_id": 77,
        "message_type": "group", "message": segments,
    })
    return adapter, adapter._event_queue.get_nowait()

class _RunnableRegistry:
    """绑定判定恒为可运行的会话定义注册表替身"""

    @staticmethod
    def binding_status(*_args: object) -> BindingStatus:
        """恒定答复可运行"""
        return BindingStatus(BindingState.RUNNABLE)

class TestProjectionBudget:
    """总额度记账的固定优先级: 正文保前缀 > 来源标记头 > 资料块内容"""

    def test_body_prefix_beats_context_headers(self):
        notes: list[str] = []
        blocks = [_ContextBlock("quote", "[引用 甲 的消息: ", "长内容" * 100, "]")]
        result = ProjectionBudget(20).assemble("正文" * 30, blocks, notes)
        assert len(result) == 20 and result.endswith("…")
        assert "[引用" not in result
        assert notes == ["body_budget_truncated", "quote_budget_dropped"]

    def test_header_beats_own_content(self):
        notes: list[str] = []
        blocks = [_ContextBlock("quote", "[引用 甲: ", "x" * 100, "]")]
        result = ProjectionBudget(20).assemble("问题", blocks, notes)
        assert len(result) <= 20
        assert result.startswith("[引用 甲: ") and "…]" in result
        assert result.endswith("\n问题")
        assert notes == ["quote_budget_truncated"]

    def test_second_block_content_yields_to_first_block_but_keeps_header(self):
        notes: list[str] = []
        blocks = [
            _ContextBlock("quote", "AH[", "a" * 10, "]"),
            _ContextBlock("forward", "BH[", "b" * 10, "]"),
        ]
        result = ProjectionBudget(30).assemble("b", blocks, notes)
        assert len(result) <= 30
        assert "a" * 10 in result
        assert "BH[" in result and "b" * 10 not in result
        assert notes == ["forward_budget_truncated"]

    def test_full_content_borrows_truncation_marker_reserve(self):
        notes: list[str] = []
        blocks = [_ContextBlock("quote", "H[", "cccc", "]")]
        result = ProjectionBudget(10).assemble("ab", blocks, notes)
        assert result == "H[cccc]\nab"
        assert notes == []
        # 差一字符即触发截断, 截断提示计入总量
        result = ProjectionBudget(9).assemble("ab", [_ContextBlock("quote", "H[", "cccc", "]")], notes)
        assert result == "H[cc…]\nab" and len(result) == 9

    def test_header_only_block_without_content(self):
        notes: list[str] = []
        blocks = [_ContextBlock("quote", "[引用了一条无法获取原文的消息]", "")]
        result = ProjectionBudget(50).assemble("正文", blocks, notes)
        assert result == "[引用了一条无法获取原文的消息]\n正文"
        assert notes == []

    def test_invalid_limit_rejected(self):
        with pytest.raises(ValueError, match="总额度"):
            ProjectionBudget(0)

class TestInputBudgetValidation:
    @pytest.mark.parametrize("value", [0, 200001, True, "20000", 1.5])
    def test_input_text_limit_range(self, value: object):
        with pytest.raises(ValueError, match="input_text_limit"):
            validate_wake_policy({"input_text_limit": value})

    @pytest.mark.parametrize("value", [0, 33, True, "8", 2.5])
    def test_input_media_limit_range(self, value: object):
        with pytest.raises(ValueError, match="input_media_limit"):
            validate_wake_policy({"input_media_limit": value})

    def test_valid_limits_accepted(self):
        validate_wake_policy({"input_text_limit": 1, "input_media_limit": 32})

    def test_input_limits_are_platform_level_not_group_overridable(self):
        with pytest.raises(ValueError, match="唤醒规则字段"):
            validate_wake_policy({"wake_group_overrides": {"456": {"input_text_limit": 100}}})
        with pytest.raises(ValueError, match="自动参与参数"):
            time_rules: list[dict[str, object]] = [
                {"start": "08:00", "end": "09:00", "settings": {"input_media_limit": 4}},
            ]
            validate_wake_policy({"wake_time_rules": time_rules})

class TestTopMediaTruncation:
    @pytest.mark.asyncio
    async def test_top_media_actually_truncated_to_limit(self):
        segments: list[dict[str, object]] = [
            *[{"type": "image", "data": {"url": f"http://x/i{i}.png"}} for i in range(6)],
            *[{"type": "video", "data": {"url": f"http://x/v{i}.mp4"}} for i in range(5)],
            {"type": "text", "data": {"text": "看图"}},
        ]
        _, event = await make_event({"input_media_limit": 8}, segments)
        projected = project_input(event, "none")
        assert len(projected.images) == 6 and len(projected.videos) == 2
        assert "top_media_truncated" in projected.notes

    @pytest.mark.asyncio
    async def test_top_media_consumes_shared_budget_before_quote_media(self):
        segments: list[dict[str, object]] = [
            *[{"type": "image", "data": {"url": f"http://x/top{i}.png"}} for i in range(3)],
            {"type": "reply", "data": {"id": "5"}},
            {"type": "text", "data": {"text": "看这个"}},
        ]
        adapter, event = await make_event({"input_media_limit": 4}, segments)
        adapter._bot.get_msg.return_value = {
            "message_id": 5, "group_id": 456, "message_type": "group", "self_id": 10000,
            "sender": {"user_id": 321, "nickname": "小明"}, "time": 1,
            "message": [{"type": "image", "data": {"url": f"http://x/q{i}.png"}} for i in range(3)],
        }
        status = await resolve_quotes(event)
        projected = project_input(event, status)
        # 顶层 3 张消耗后仅剩 1 个余量供引用媒体
        assert len(projected.images) == 4
        assert "quote_media_truncated" in projected.notes
        assert "top_media_truncated" not in projected.notes

class TestTinyLimitContract:
    @pytest.mark.asyncio
    async def test_quote_content_truncated_before_body(self):
        adapter, event = await make_event({"input_text_limit": 30}, [
            {"type": "reply", "data": {"id": "5"}},
            {"type": "text", "data": {"text": "这个对吗"}},
        ])
        adapter._bot.get_msg.return_value = {
            "message_id": 5, "group_id": 456, "message_type": "group", "self_id": 10000,
            "sender": {"user_id": 321, "nickname": "小明"}, "time": 1,
            "message": [{"type": "text", "data": {"text": "y" * 2100}}],
        }
        status = await resolve_quotes(event)
        projected = project_input(event, status)
        # 引用块单块上限先截断到 2000, 总预算再按优先级截断: 正文完整, 标记头保留, 内容截断
        assert len(projected.message) <= 30
        assert projected.message.endswith("\n这个对吗")
        assert projected.message.startswith("[引用 小明 的消息: ")
        assert "…]" in projected.message
        assert "quote_truncated" in projected.notes and "quote_budget_truncated" in projected.notes

    @pytest.mark.asyncio
    async def test_tiny_limit_drops_context_but_keeps_body(self):
        adapter, event = await make_event({"input_text_limit": 10}, [
            {"type": "reply", "data": {"id": "5"}},
            {"type": "text", "data": {"text": "正文正文正文正文"}},
        ])
        adapter._bot.get_msg.return_value = {
            "message_id": 5, "group_id": 456, "message_type": "group", "self_id": 10000,
            "sender": {"user_id": 321, "nickname": "小明"}, "time": 1,
            "message": [{"type": "text", "data": {"text": "引用原文"}}],
        }
        status = await resolve_quotes(event)
        projected = project_input(event, status)
        assert len(projected.message) <= 10
        assert projected.message.startswith("正文正文")
        assert "[引用" not in projected.message
        assert "quote_budget_dropped" in projected.notes

class TestWindowBlockBudget:
    @pytest.mark.asyncio
    async def test_window_block_consumes_remaining_budget(self):
        """窗口块拼接消耗投影剩余额度, 实际 UserCall 总额不超过 input_text_limit"""
        from satrap.core.framework.SessionManager import SessionManager
        from typing import cast

        manager = AsyncMock()
        manager.provider_registry = _RunnableRegistry()
        manager.handle_call_async.return_value = ""
        scheduler = PipelineScheduler(cast(SessionManager, manager))
        adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={
            "self_id": "10", "wake_mode": "frequency", "wake_message_threshold": 5, "wake_cooldown": 0,
            "input_text_limit": 100,
        }))
        adapter.started = True
        adapter._bot = AsyncMock()
        await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 1,
            "message_type": "group", "message": [{"type": "text", "data": {"text": "窗" * 80}}]})
        await scheduler.execute(adapter._event_queue.get_nowait())
        manager.handle_call_async.assert_not_awaited()

        await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 2,
            "message_type": "group", "message": [{"type": "at", "data": {"qq": "10"}},
                                                 {"type": "text", "data": {"text": "正" * 40}}]})
        event = adapter._event_queue.get_nowait()
        await scheduler.execute(event)
        manager.handle_call_async.assert_awaited_once()
        user_call = manager.handle_call_async.await_args.args[0]
        assert len(user_call.message) <= 100
        assert user_call.message.startswith("[用户 30, 消息 2] @10" + "正" * 40)
        assert "[先前窗口消息" in user_call.message
        projected = event.get_extra("input_projection")
        assert "window_budget_truncated" in projected.notes

    @pytest.mark.asyncio
    async def test_window_block_dropped_when_no_budget_remains(self):
        from satrap.core.framework.SessionManager import SessionManager
        from typing import cast

        manager = AsyncMock()
        manager.provider_registry = _RunnableRegistry()
        manager.handle_call_async.return_value = ""
        scheduler = PipelineScheduler(cast(SessionManager, manager))
        adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={
            "self_id": "10", "wake_mode": "frequency", "wake_message_threshold": 5, "wake_cooldown": 0,
            "input_text_limit": 20,
        }))
        adapter.started = True
        adapter._bot = AsyncMock()
        await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 1,
            "message_type": "group", "message": [{"type": "text", "data": {"text": "窗口内容"}}]})
        await scheduler.execute(adapter._event_queue.get_nowait())
        await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 2,
            "message_type": "group", "message": [{"type": "at", "data": {"qq": "10"}},
                                                 {"type": "text", "data": {"text": "正" * 30}}]})
        event = adapter._event_queue.get_nowait()
        await scheduler.execute(event)
        manager.handle_call_async.assert_awaited_once()
        user_call = manager.handle_call_async.await_args.args[0]
        # 正文已被投影截断至额度, 窗口块无剩余额度可用
        assert len(user_call.message) <= 20
        assert "[先前窗口消息" not in user_call.message
        projected = event.get_extra("input_projection")
        assert "window_budget_dropped" in projected.notes

class RunnableRegistry:
    """返回可运行会话定义的测试注册表"""

    @staticmethod
    def binding_status(*_args: object) -> BindingStatus:
        """返回可运行的绑定状态"""
        return BindingStatus(BindingState.RUNNABLE)

def runtime(settings: dict[str, Any] | None = None) -> tuple[OneBotAdapter, PipelineScheduler, AsyncMock]:
    """构造使用真实 OneBot 解析和调度器的本地测试运行时"""
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={"self_id": "10", **(settings or {})}))
    adapter.started = True
    adapter._bot = AsyncMock()
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
    manager.handle_call_async.return_value = ""
    users = Mock()
    users.resolve_session.return_value = "test-session"
    return adapter, PipelineScheduler(manager, user_manager=users), manager

async def incoming(
    adapter: OneBotAdapter, number: int, segments: list[dict[str, Any]], actor: int = 30,
    sender: dict[str, Any] | None = None, group: int = 20,
) -> MessageEvent:
    """由平台入站入口创建事件, 避免绕过唤醒和路由设置"""
    await adapter._handle_group_message({"self_id": 10, "group_id": group, "user_id": actor,
        "message_id": number, "message_type": "group", "message": segments,
        "sender": sender or {"user_id": actor}})
    return adapter._event_queue.get_nowait()

def text(value: str) -> dict[str, Any]:
    """构造 OneBot 文字段"""
    return {"type": "text", "data": {"text": value}}

def picture(name: str) -> dict[str, Any]:
    """构造保留原始图片标识的 OneBot 图片段"""
    return {"type": "image", "data": {"file": f"{name}.image", "url": f"https://cdn/{name}.png"}}

AT = {"type": "at", "data": {"qq": "10"}}

@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """替换真实图片下载, 保留文件创建和清理路径"""
    urls: list[str] = []

    async def download(url: str, *_args: object, **_kwargs: object) -> bytes:
        """记录下载地址并返回本地测试图片字节"""
        urls.append(url)
        return b"\x89PNG\r\n\x1a\n" + bytes(32)

    monkeypatch.setattr("satrap.core.pipeline.media_resolve._download", download)
    return urls

@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["explicit", "frequency", "necessity"])
async def test_unwoken_text_and_image_then_question_share_one_input(mode: str, downloads: list[str]):
    adapter, scheduler, manager = runtime({"wake_mode": mode, "wake_message_threshold": 32,
        "wake_score_threshold": 1, "wake_cooldown": 0})
    first = await incoming(adapter, 1, [text("图前"), picture("old"), text("图后")],
                           sender={"user_id": 30, "nickname": "小明"})
    await scheduler.execute(first)
    assert not downloads
    manager.handle_call_async.assert_not_awaited()
    snapshot = scheduler.wake_window.peek(first)
    assert snapshot[0].parts == ("图前", PendingImage("old.image", "https://cdn/old.png"), "图后")
    cast(Image, first.get_messages()[1]).url = "https://cdn/mutated.png"
    first.platform_message.sender.nickname = "后来的名字"
    files: list[str] = []

    async def model(call: UserCall) -> str:
        """确认模型调用时文件可读, 同时捕获本轮输入"""
        files.extend(call.img_urls or [])
        assert all(Path(file).exists() for file in files)
        assert call.message is not None
        assert "[用户 小明 (ID 30), 消息 1]" in call.message
        assert "图前[图片 1]图后" in call.message
        assert call.message.count("这张图是什么") == 1
        assert "后来的名字" not in call.message
        assert "https://cdn/" not in call.message
        return ""

    manager.handle_call_async.side_effect = model
    second = await incoming(adapter, 2, [AT, text("这张图是什么")])
    await scheduler.execute(second)
    manager.handle_call_async.assert_awaited_once()
    assert downloads == ["https://cdn/old.png"]
    assert files and all(not Path(file).exists() for file in files)
    assert scheduler.wake_window.peek(second) == ()

@pytest.mark.asyncio
async def test_pure_at_consumes_prior_image_only_message(downloads: list[str]):
    adapter, scheduler, manager = runtime()
    await scheduler.execute(await incoming(adapter, 1, [picture("only")]))
    await scheduler.execute(await incoming(adapter, 2, [AT]))
    manager.handle_call_async.assert_awaited_once()
    call = manager.handle_call_async.await_args.args[0]
    assert len(call.img_urls) == 1 and "[图片 1]" in call.message
    assert downloads == ["https://cdn/only.png"]

@pytest.mark.asyncio
async def test_current_media_then_newest_history_use_shared_budget(downloads: list[str]):
    adapter, scheduler, manager = runtime({"input_media_limit": 2})
    await scheduler.execute(await incoming(adapter, 1, [picture("old")]))
    await scheduler.execute(await incoming(adapter, 2, [picture("recent")]))
    await scheduler.execute(await incoming(adapter, 3, [AT, picture("current"), text("比较一下")]))
    call = manager.handle_call_async.await_args.args[0]
    assert downloads == ["https://cdn/current.png", "https://cdn/recent.png"]
    assert len(call.img_urls) == 2
    assert "消息 1] [图片未纳入本次输入]" in call.message
    assert "消息 2] [图片 2]" in call.message

@pytest.mark.asyncio
async def test_same_image_in_current_and_window_is_downloaded_once(downloads: list[str]):
    adapter, scheduler, manager = runtime({"input_media_limit": 1})
    await scheduler.execute(await incoming(adapter, 1, [picture("same")]))
    await scheduler.execute(await incoming(adapter, 2, [AT, picture("same"), text("看图")]))
    call = manager.handle_call_async.await_args.args[0]
    assert downloads == ["https://cdn/same.png"]
    assert len(call.img_urls) == 1 and "消息 1] [图片 1]" in call.message

@pytest.mark.asyncio
async def test_small_text_budget_never_downloads_invisible_history(downloads: list[str]):
    adapter, scheduler, manager = runtime({"input_text_limit": 32})
    await scheduler.execute(await incoming(adapter, 1, [picture("old")]))
    current = await incoming(adapter, 2, [AT, text("x" * 32)])
    await scheduler.execute(current)
    call = manager.handle_call_async.await_args.args[0]
    assert len(call.message) <= 32 and not call.img_urls and not downloads
    assert "window_budget_dropped" in current.get_extra("input_projection").notes

@pytest.mark.asyncio
@pytest.mark.parametrize("scope,visible", [("legacy_user", False), ("group_member", False), ("group", True)])
async def test_window_respects_member_visibility(scope: str, visible: bool, downloads: list[str]):
    adapter, scheduler, manager = runtime({"context_scope": scope})
    await scheduler.execute(await incoming(adapter, 1, [text("成员私有正文"), picture("member")], actor=31,
                                           sender={"user_id": 31, "nickname": "成员甲"}))
    await scheduler.execute(await incoming(adapter, 2, [AT, text("现在回答")], actor=30))
    call = manager.handle_call_async.await_args.args[0]
    assert ("成员私有正文" in call.message) == visible
    assert bool(call.img_urls) == visible
    assert bool(downloads) == visible

@pytest.mark.asyncio
async def test_picture_reference_bounds_ttl_and_route_identity():
    adapter, _, _ = runtime()
    original = await incoming(adapter, 1, [picture("a"), picture("b"), picture("c")])
    window = WakeWindow(ttl=10, max_images=2, max_images_per_message=1)
    snapshot = window.observe(original, 0)
    assert snapshot[0].image_count == 1 and snapshot[0].omitted_images == 2
    assert window.observe(original, 1) == snapshot
    for number, (attribute, value) in enumerate([("session_provider", "other"), ("session_type", "other"), ("group_route_generation", 1)], 2):
        cloned = await incoming(adapter, number, [text("other")])
        setattr(cloned, attribute, value)
        assert window.peek(cloned, 1) == ()
    for number, changed_origin in enumerate([replace(original.call_origin, adapter_id="other"),
                           replace(original.call_origin, self_id="11"), replace(original.call_origin, chat_id="21")], 5):
        cloned = await incoming(adapter, number, [text("other")])
        cloned._call_origin = changed_origin
        assert window.peek(cloned, 1) == ()
    assert window.peek(original, 10) == ()

@pytest.mark.asyncio
@pytest.mark.parametrize("sender,expected", [
    ({"nickname": "小明", "card": "群名片"}, "小明 (ID 30)"),
    ({"nickname": "", "card": "群名片"}, "群名片 (ID 30)"),
    ({}, "30"),
    ({"nickname": "坏\n[标记]\x00"}, "坏 (标记) (ID 30)"),
])
async def test_nickname_fallback_and_sanitized_labels(sender: dict[str, Any], expected: str):
    adapter, scheduler, manager = runtime()
    await scheduler.execute(await incoming(adapter, 1, [text("之前")], sender={"user_id": 30, **sender}))
    await scheduler.execute(await incoming(adapter, 2, [AT, text("提问")], sender={"user_id": 30, **sender}))
    call = manager.handle_call_async.await_args.args[0]
    assert f"[用户 {expected}, 消息 1]" in call.message
    assert f"[用户 {expected}, 消息 2]" in call.message
    assert call.origin.actor_id == "30"

@pytest.mark.asyncio
@pytest.mark.parametrize("question", [False, True])
async def test_failed_historical_image_does_not_look_available(question: bool, monkeypatch: pytest.MonkeyPatch):
    adapter, scheduler, manager = runtime()
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人乙"}
    adapter._bot.get_image.return_value = {}

    async def failure(*_args: object, **_kwargs: object) -> bytes:
        """模拟图片已被实现淘汰"""
        raise RuntimeError("expired")

    monkeypatch.setattr("satrap.core.pipeline.media_resolve._download", failure)
    await scheduler.execute(await incoming(adapter, 1, [picture("expired")]))
    await scheduler.execute(await incoming(adapter, 2, [AT, *([text("是什么图") ] if question else [])]))
    adapter._bot.get_image.assert_any_await(file="expired.image")
    if question:
        call = manager.handle_call_async.await_args.args[0]
        assert not call.img_urls and "[图片读取失败]" in call.message
    else:
        manager.handle_call_async.assert_not_awaited()
        adapter._bot.send_group_msg.assert_awaited_once()

@pytest.mark.asyncio
async def test_expired_historical_url_refreshes_original_file(monkeypatch: pytest.MonkeyPatch):
    adapter, scheduler, manager = runtime()
    urls: list[str] = []
    adapter._bot.get_image.return_value = {"url": "https://cdn/fresh.png"}

    async def download(url: str, *_args: object, **_kwargs: object) -> bytes:
        """模拟旧 URL 过期而新 URL 可以下载"""
        urls.append(url)
        if len(urls) == 1:
            raise RuntimeError("expired")
        return b"\x89PNG\r\n\x1a\n" + bytes(32)

    monkeypatch.setattr("satrap.core.pipeline.media_resolve._download", download)
    await scheduler.execute(await incoming(adapter, 1, [picture("expired")]))
    await scheduler.execute(await incoming(adapter, 2, [AT, text("看图")]))
    assert urls == ["https://cdn/expired.png", "https://cdn/fresh.png"]
    adapter._bot.get_image.assert_awaited_once_with(file="expired.image")
    assert len(manager.handle_call_async.await_args.args[0].img_urls) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("with_identity", [False, True])
async def test_deadline_wake_preserves_picture_and_original_sender(with_identity: bool, downloads: list[str]):
    adapter, scheduler, manager = runtime({"wake_mode": "frequency", "wake_message_threshold": 3,
        "wake_max_wait": 0.01, "wake_cooldown": 0})
    if with_identity:
        adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人乙"}
        adapter._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 20, "card": "本群助手"}
    first = await incoming(adapter, 1, [picture("timer")], sender={"user_id": 30, "nickname": "小明"})
    await scheduler.execute(first)
    assert not downloads
    timed = await asyncio.wait_for(adapter._event_queue.get(), timeout=1)
    await scheduler.execute(timed)
    await scheduler.wake_timers.close()
    manager.handle_call_async.assert_awaited_once()
    call = manager.handle_call_async.await_args.args[0]
    prefix = "[你当前的平台机器人身份: 账号 ID 10, 账号昵称 机器人乙, 本群昵称 本群助手]\n" if with_identity else ""
    assert call.message == prefix + "[用户 小明 (ID 30), 消息 1] [图片 1]"
    assert len(call.img_urls) == 1 and downloads == ["https://cdn/timer.png"]

@pytest.mark.asyncio
async def test_retained_image_caps_are_visible_in_projection():
    adapter, _, _ = runtime()
    event = await incoming(adapter, 1, [picture("a"), picture("b")])
    snapshot = WakeWindow(max_images_per_message=1).observe(event)
    selection = select_window_media(event, select_media(event, "none"), snapshot, "none", "none", (), True)
    projected = project_input(event, "none", selection=selection)
    assert "另有 1 张图片超出窗口容量" in projected.message
    assert "window_media_storage_truncated" in projected.notes

@pytest.mark.asyncio
async def test_rate_limit_keeps_window_until_later_success(downloads: list[str]):
    adapter, scheduler, manager = runtime()
    limiter = AsyncMock()
    limiter.check.side_effect = [(False, 1.0), (True, 0.0)]
    scheduler.rate_limiter = limiter
    await scheduler.execute(await incoming(adapter, 1, [picture("kept")]))
    blocked = await incoming(adapter, 2, [AT, text("第一次提问")])
    await scheduler.execute(blocked)
    manager.handle_call_async.assert_not_awaited()
    assert len(scheduler.wake_window.peek(blocked)) == 2 and not downloads
    await scheduler.execute(await incoming(adapter, 3, [AT, text("第二次提问")]))
    call = manager.handle_call_async.await_args.args[0]
    assert "第一次提问" in call.message and "第二次提问" in call.message
    assert downloads == ["https://cdn/kept.png"]
    assert not scheduler.wake_window.peek(blocked)

@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["explicit", "frequency", "necessity"])
async def test_commands_leave_image_window_untouched(mode: str, downloads: list[str]):
    adapter, scheduler, manager = runtime({"wake_mode": mode, "wake_message_threshold": 32, "wake_score_threshold": 1})
    first = await incoming(adapter, 1, [picture("waiting")])
    await scheduler.execute(first)
    before = scheduler.wake_window.peek(first)
    command = await incoming(adapter, 2, [AT, text(" /help")])
    await scheduler.execute(command)
    assert manager.handle_call_async.await_args.args[0].message == "/help"
    assert scheduler.wake_window.peek(command) == before and not downloads
    await scheduler.execute(await incoming(adapter, 3, [AT, text("看看")]))
    assert downloads == ["https://cdn/waiting.png"]

@pytest.mark.asyncio
async def test_window_capacity_counts_preserved_whitespace():
    adapter, _, _ = runtime()
    window = WakeWindow(max_chars=8)
    first = await incoming(adapter, 1, [text("     a"), picture("first")])
    second = await incoming(adapter, 2, [text("     b"), picture("second")])
    window.observe(first)
    snapshot = window.observe(second)
    assert [item.message_id for item in snapshot] == ["2"]
    assert snapshot[0].text == "b" and snapshot[0].text_size == 6

@pytest.mark.asyncio
async def test_own_identity_is_distinct_from_sender_and_wake_alias():
    adapter, scheduler, manager = runtime({"wake_aliases": ["唤醒别名"]})
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人乙"}
    adapter._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 20, "card": "本群助手"}
    await scheduler.execute(await incoming(adapter, 1, [AT, text("你叫什么")], sender={"user_id": 30, "nickname": "用户甲"}))
    call = manager.handle_call_async.await_args.args[0]
    assert call.message.startswith("[你当前的平台机器人身份: 账号 ID 10, 账号昵称 机器人乙, 本群昵称 本群助手]\n")
    assert "[用户 用户甲 (ID 30), 消息 1]" in call.message
    assert "你叫什么" in call.message and "唤醒别名" not in call.message
    assert call.origin.actor_id == "30" and call.origin.self_id == "10"
    adapter._bot.get_group_member_info.assert_awaited_once_with(self_id="10", group_id=20, user_id=10, no_cache=True)

@pytest.mark.asyncio
async def test_private_message_gets_own_account_nickname():
    adapter, scheduler, manager = runtime()
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人乙"}
    await adapter._handle_private_message({"self_id": 10, "user_id": 30, "message_id": 1, "message_type": "private",
        "sender": {"user_id": 30, "nickname": "用户甲"}, "message": [text("你叫什么")]})
    await scheduler.execute(adapter._event_queue.get_nowait())
    call = manager.handle_call_async.await_args.args[0]
    assert call.message.startswith("[你当前的平台机器人身份: 账号 ID 10, 账号昵称 机器人乙]\n")
    assert "你叫什么" in call.message and "本群昵称" not in call.message
    adapter._bot.get_group_member_info.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["unwoken", "command", "limited", "disabled"])
async def test_identity_is_not_queried_before_normal_input_is_accepted(gate: str):
    adapter, scheduler, manager = runtime()
    segments = [AT, text("提问")]
    if gate == "unwoken":
        segments = [text("提问")]
    elif gate == "command":
        segments = [AT, text("/help")]
    elif gate == "limited":
        scheduler.rate_limiter = AsyncMock()
        scheduler.rate_limiter.check.return_value = (False, 1.0)
    else:
        manager.provider_registry = Mock()
        manager.provider_registry.binding_status.return_value = BindingStatus(BindingState.DISABLED)
    await scheduler.execute(await incoming(adapter, 1, segments))
    adapter._bot.get_login_info.assert_not_awaited()
    adapter._bot.get_group_member_info.assert_not_awaited()
    if gate != "command":
        manager.handle_call_async.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [32, 128, 256])
async def test_identity_reserves_history_budget_without_orphan_images(limit: int, downloads: list[str]):
    adapter, scheduler, manager = runtime({"input_text_limit": limit})
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人乙"}
    adapter._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 20, "card": "本群助手"}
    await scheduler.execute(await incoming(adapter, 1, [picture("old")]))
    event = await incoming(adapter, 2, [AT, text("现在看图")])
    await scheduler.execute(event)
    call = manager.handle_call_async.await_args.args[0]
    assert len(call.message) <= limit and "现在看图" in call.message
    for number in range(1, len(call.img_urls or []) + 1):
        assert f"[图片 {number}]" in call.message
    if limit == 32:
        assert "你当前的平台机器人身份" not in call.message
        assert "bot_identity_budget_dropped" in event.get_extra("input_projection").notes
    else:
        assert "账号昵称 机器人乙" in call.message

@pytest.mark.asyncio
@pytest.mark.parametrize("identity,expected", [
    (BotIdentity("10", "机器人\n[乙]\x00"), "账号昵称 机器人 (乙)"),
    (BotIdentity("11", "其他账号"), ""),
    (BotIdentity("10", "其他群", "21", "其他名片"), ""),
])
async def test_identity_projection_sanitizes_names_and_rejects_wrong_scope(identity: BotIdentity, expected: str):
    adapter, _, _ = runtime()
    event = await incoming(adapter, 1, [AT, text("提问")])
    selection = replace(select_media(event, "none"), self_identity=identity)
    projected = project_input(event, "none", selection=selection)
    assert "提问" in projected.message
    if expected:
        assert expected in projected.message and "\x00" not in projected.message
        assert projected.message.count("\n") == 1
    else:
        assert "你当前的平台机器人身份" not in projected.message
