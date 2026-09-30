"""统一输入总预算: 文本额度记账, 顶层媒体实际裁剪与窗口块余量消耗"""
from unittest.mock import AsyncMock
from typing import Any

import pytest

from satrap.core.pipeline.input_projection import ProjectionBudget, _ContextBlock, project_input, resolve_quotes
from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform import PlatformConfig
from satrap.core.framework.providers import BindingState, BindingStatus


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
        """
        恒定答复可运行

        返回:
        - BindingStatus: 可运行
        """
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
            "input_text_limit": 60,
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
        assert len(user_call.message) <= 60
        assert user_call.message.startswith("@10" + "正" * 40)
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
