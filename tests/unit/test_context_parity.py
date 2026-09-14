from unittest.mock import AsyncMock, Mock
from pathlib import Path
from typing import Any
import pytest
import json

from satrap.core.utils.context.utils import add_tool_message, _SUMMARY_PROMPT_VERSION
from satrap.core.utils.context import AsyncContextManager, ContextManager


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{"结果": 3}, "错误: x\n第二行", "", '{"x": 1}'])
async def test_tool_results_match_and_persist(tmp_path: Path, result: dict[str, Any] | str) -> None:
    """
    验证三种消息入口的字符串契约和持久化结果

    参数:
    - tmp_path: 隔离文件与数据库的临时目录
    - result: 需要验证的字典或字符串结果
    """
    sync = ContextManager("sync", db_path=str(tmp_path / "sync.db"))
    async with AsyncContextManager("async", db_path=str(tmp_path / "async.db")) as async_ctx:
        sync_checkpoint = Mock()
        async_checkpoint = AsyncMock()
        sync._maybe_auto_checkpoint = sync_checkpoint
        async_ctx._maybe_auto_checkpoint = async_checkpoint
        sync.add_tool_message("call", result)
        await async_ctx.add_tool_message("call", result)
        raw: list[dict[str, Any]] = []
        add_tool_message(raw, "call", result)
        expected = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else result
        assert raw[0]["content"] == expected
        assert sync.get_context() == async_ctx.get_context() == raw
        sync_checkpoint.assert_called_once_with()
        async_checkpoint.assert_awaited_once_with()
    reloaded = ContextManager("async", db_path=str(tmp_path / "async.db"))
    assert reloaded.get_context() == raw
    sync_reloaded = ContextManager("sync", db_path=str(tmp_path / "sync.db"))
    assert sync_reloaded.get_context() == raw
    sync.close()
    reloaded.close()
    sync_reloaded.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("n,remaining", [(1, 4), (2, 1), (9, 1), (0, 4)])
async def test_delete_turns_preserves_system_and_tool_groups(tmp_path: Path, n: int, remaining: int) -> None:
    """
    验证同步和异步删除保持系统消息及工具调用分组

    参数:
    - tmp_path: 隔离文件与数据库的临时目录
    - n: 待删除的对话组数
    - remaining: 期望保留的消息数
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": "一"},
        {"role": "assistant", "content": "调用"},
        {"role": "tool", "content": "结果", "tool_call_id": "call"},
        {"role": "user", "content": "二"},
        {"role": "assistant", "content": "回复"},
    ]
    sync = ContextManager("sync", keep_in_memory=True, db_path=str(tmp_path / "sync.db"))
    async with AsyncContextManager("async", keep_in_memory=True, db_path=str(tmp_path / "async.db")) as async_ctx:
        sync._messages = messages.copy()
        async_ctx._messages = messages.copy()
        sync.del_last_chat(n)
        await async_ctx.del_last_chat(n)
        assert sync.get_context() == async_ctx.get_context() == messages[:remaining]
        sync._messages = []
        async_ctx._messages = []
        sync.del_last_chat(n)
        await async_ctx.del_last_chat(n)
        assert sync.get_context() == async_ctx.get_context() == []


@pytest.mark.asyncio
async def test_summary_reset_has_identical_persistence_boundaries(tmp_path: Path) -> None:
    """
    验证总结失效字段与即时保存边界

    参数:
    - tmp_path: 隔离文件与数据库的临时目录
    """
    sync = ContextManager("sync", keep_in_memory=True, db_path=str(tmp_path / "sync.db"))
    async with AsyncContextManager("async", keep_in_memory=True, db_path=str(tmp_path / "async.db")) as async_ctx:
        sync_save, async_save = Mock(), AsyncMock()
        sync._save_runtime_state = sync_save
        async_ctx._save_runtime_state = async_save
        for ctx in (sync, async_ctx):
            ctx._runtime_state.summary = "旧总结"
            ctx._runtime_state.covered_turn_count = 9
            ctx._runtime_state.summary_model = "old"
            ctx._runtime_state.summary_prompt_version = -1
            ctx._runtime_state.api_input_tokens = 123
            ctx._runtime_state_dirty = False
            ctx._mark_dirty()
            assert ctx._saved_count == -1
            assert ctx._runtime_state.summary == ""
            assert ctx._runtime_state.covered_turn_count == 0
            assert ctx._runtime_state.summary_model is None
            assert ctx._runtime_state.summary_prompt_version == _SUMMARY_PROMPT_VERSION
            assert ctx._runtime_state.api_input_tokens == 123
            assert ctx._runtime_state_dirty
        sync._invalidate_summary()
        await async_ctx._invalidate_summary()
        sync_save.assert_not_called()
        async_save.assert_not_called()
        sync._invalidate_summary(persist=True)
        await async_ctx._invalidate_summary(persist=True)
        sync_save.assert_called_once_with()
        async_save.assert_awaited_once_with()
