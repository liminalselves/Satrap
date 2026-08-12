"""上下文分离比例 + 滞回截断 + 总结压缩的单元测试"""

import pytest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, AsyncMock

from satrap.core.utils.context import ContextManager, AsyncContextManager


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
        assert cm.history_budget == int(128000 * 0.7)  # 89600
        assert cm.trigger_tokens == int(89600 * 0.8)   # 71680
        assert cm.floor_tokens == int(89600 * 0.4)     # 35840
        assert cm.output_budget == 128000 - 89600       # 38400

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
        # 填充少量消息, 远低于触发线
        for i in range(5):
            cm.add_user_message(f"消息 {i}")
            cm.add_bot_message(f"回复 {i}")
        result = cm.get_model_context()
        assert len(result) == 10  # 5 user + 5 assistant

    def test_truncation_stops_at_floor_not_trigger(self, tmp_path: Path):
        """截断后 token 数应 ≤ floor_tokens 而非 ≤ trigger_tokens"""
        cm = ContextManager(
            "test_trunc",
            db_path=str(tmp_path / "test.db"),
            max_context=1000,
            history_ratio=0.7,
            context_threshold=0.8,
            truncation_floor=0.4,
        )
        # trigger = 560, floor = 280
        assert cm.trigger_tokens == 560
        assert cm.floor_tokens == 280

        # 填充大量消息使总 token 超触发线
        for i in range(50):
            cm.add_user_message(f"用户消息 {i} " + "x" * 50)
            cm.add_bot_message(f"助手回复 {i} " + "y" * 50)

        result = cm.get_model_context()
        result_tokens = cm.estimate_token(result)
        # 截断后应 ≤ floor (280), 而非 ≤ trigger (560)
        assert result_tokens <= cm.floor_tokens


class TestSummarizeAndCompress:
    """总结压缩"""

    def test_compress_removes_old_turns_and_injects_summary(self, tmp_path: Path):
        cm = ContextManager("test_sum", db_path=str(tmp_path / "test.db"))
        cm.reset_system_prompt("你是一个助手")

        # 构造 5 轮对话
        for i in range(5):
            cm.add_user_message(f"问题 {i}")
            cm.add_bot_message(f"回答 {i}")

        mock_llm = MagicMock()
        mock_llm.chat.return_value = "用户问了5个问题, 分别是问题0到问题4"

        summary = cm.summarize_and_compress(mock_llm, keep_recent_turns=2)

        assert summary == "用户问了5个问题, 分别是问题0到问题4"
        # 前 3 轮被删, 保留最近 2 轮
        messages = cm.get_context()
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert len(user_msgs) == 2
        assert "问题 3" in str(user_msgs[0].get("content", ""))
        assert "问题 4" in str(user_msgs[1].get("content", ""))
        # system prompt 含摘要区块
        system_msgs = [m for m in messages if m.get("role") == "system"]
        assert len(system_msgs) == 1
        assert "<对话历史摘要>" in str(system_msgs[0].get("content", ""))
        assert "用户问了5个问题" in str(system_msgs[0].get("content", ""))

    def test_compress_with_multimodal_projects_images(self, tmp_path: Path):
        cm = ContextManager("test_mm", db_path=str(tmp_path / "test.db"))
        cm.reset_system_prompt("助手")

        # 构造含多模态的消息
        cm.add_user_message("看这张图", img_urls=["http://example.com/img.jpg"])
        cm.add_bot_message("我看到图片了")
        cm.add_user_message("什么问题")
        cm.add_bot_message("回答")

        mock_llm = MagicMock()
        mock_llm.chat.return_value = "用户分享了一张图片"

        cm.summarize_and_compress(mock_llm, keep_recent_turns=1)

        # 传给 LLM 的文本中图片已投影为 [图片] (chat 接收 messages 列表)
        sent_messages = mock_llm.chat.call_args_list[0][0][0]
        sent_text = sent_messages[0]["content"] if isinstance(sent_messages, list) else str(sent_messages)
        assert "[图片]" in sent_text
        assert "image_url" not in sent_text

    def test_compress_merges_existing_summary(self, tmp_path: Path):
        cm = ContextManager("test_merge", db_path=str(tmp_path / "test.db"))
        cm.reset_system_prompt("助手\n\n<对话历史摘要>\n旧摘要: 用户之前聊了天气\n</对话历史摘要>")

        for i in range(4):
            cm.add_user_message(f"问题 {i}")
            cm.add_bot_message(f"回答 {i}")

        mock_llm = MagicMock()
        # 第一次调用返回新总结, 第二次调用返回合并后的摘要
        mock_llm.chat.side_effect = ["新总结: 用户问了4个问题", "合并摘要: 用户聊了天气, 又问了4个问题"]

        cm.summarize_and_compress(mock_llm, keep_recent_turns=2)

        # 第二次调用是合并
        assert mock_llm.chat.call_count == 2
        system_content = str([m for m in cm.get_context() if m.get("role") == "system"][0].get("content", ""))
        assert "合并摘要" in system_content
        # 只有一段摘要区块
        assert system_content.count("<对话历史摘要>") == 1

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

        system_msgs = [m for m in cm.get_context() if m.get("role") == "system"]
        assert len(system_msgs) == 1
        assert "<对话历史摘要>" in str(system_msgs[0].get("content", ""))


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
            user_msgs = [m for m in cm.get_context() if m.get("role") == "user"]
            assert len(user_msgs) == 2
