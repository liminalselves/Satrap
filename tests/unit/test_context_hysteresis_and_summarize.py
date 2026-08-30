"""上下文分离比例 + 滞回截断 + 总结压缩的单元测试"""

import pytest
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, AsyncMock

from satrap.core.utils.context import ContextManager, AsyncContextManager
from satrap.core.type import TokenUsage


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
        """
        截断后 token 数应 <= floor_tokens 而非 <= trigger_tokens

        参数:
        - tmp_path: tmp路径
        """
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
        """
        摘要模型只返回空正文时应使用扩展输出预算重试

        参数:
        - tmp_path: 临时目录
        """
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
        """
        异步摘要模型只返回空正文时应使用扩展输出预算重试

        参数:
        - tmp_path: 临时目录
        """
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
