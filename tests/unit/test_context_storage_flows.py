"""业务链路场景与集中边界契约"""
from __future__ import annotations

from unittest.mock import MagicMock
from pathlib import Path
import sqlite3
import pytest
from typing import Any
from satrap.core.utils.context import AsyncContextManager, ContextManager
from unittest.mock import AsyncMock, Mock
import json
import copy
from satrap.core.utils.context.utils import add_tool_message, _SUMMARY_PROMPT_VERSION
from typing import cast
from satrap.core.type import ContextUsageSnapshot, TokenUsage
from contextlib import closing
from satrap.core.storage.legacy_context_cleanup import LegacyContextCleanup
from satrap.expend.tools.agent.utils import SUB_AGENT_SYSTEM_PROMPT
import subprocess
import sys
import os
from satrap.core.framework.Base import ModelWorkflowFramework, AsyncModelWorkflowFramework
from satrap.expend.tools.agent import SubAgentModel, AsyncSubAgentModel
from satrap.core.utils.paths import get_db_path
from scripts import clean_request_context as request_cleanup
from satrap.core.type import StateScope
from satrap.core.config.conversation_data import ConversationDataService

def _row_ids(db_path: str, conversation_id: str) -> list[int]:
    """读取库中指定对话的消息行 id"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM chat_history WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]
    finally:
        conn.close()

def _row_contents(db_path: str, conversation_id: str) -> list[dict[str, Any]]:
    """读取库中指定对话的消息内容"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT role, content FROM chat_history WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [dict[str, Any](row) for row in rows]
    finally:
        conn.close()

def test_incremental_append_stable_ids_and_reload_roundtrip(tmp_path: Path):
    """纯追加只 INSERT 尾部新消息, 行 id 稳定且重载内容与顺序完整"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-inc", db_path=db)
    ctx.add_user_message("一")
    ctx.add_bot_message("回复一")
    assert _row_ids(db, "conv-inc") == [1, 2]

    ctx.add_user_message("二")
    ctx.add_bot_message("回复二")
    assert _row_ids(db, "conv-inc") == [1, 2, 3, 4]   # 追加, 原行 id 不变

    loaded = ContextManager("conv-inc", db_path=db)
    assert [m["content"] for m in loaded.get_context()] == ["一", "回复一", "二", "回复二"]

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_corrupt_tool_calls_does_not_discard_other_messages(tmp_path: Path, asynchronous: bool):
    """同步与异步加载遇到损坏 tool_calls 时都只丢弃该字段, 其余历史完整"""
    db = str(tmp_path / "chat_history.db")
    if asynchronous:
        ctx = AsyncContextManager("conv-corrupt", db_path=db)
        await ctx.initialize()
        await ctx.add_user_message("第一条")
        await ctx.add_bot_message("第二条")
    else:
        ctx = ContextManager("conv-corrupt", db_path=db)
        ctx.add_user_message("第一条")
        ctx.add_bot_message("第二条")

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE chat_history SET tool_calls = ? WHERE conversation_id = ? AND content = ?",
            ("{broken", "conv-corrupt", "第一条"),
        )
        conn.commit()
    finally:
        conn.close()

    if asynchronous:
        loaded = AsyncContextManager("conv-corrupt", db_path=db)
        await loaded.initialize()
    else:
        loaded = ContextManager("conv-corrupt", db_path=db)
    assert [message["content"] for message in loaded.get_context()] == ["第一条", "第二条"]
    assert "tool_calls" not in loaded.get_context()[0]

def test_reset_system_prompt_triggers_full_rewrite(tmp_path: Path):
    """编辑操作 (reset_system_prompt) 走全量重写, 库内容与内存一致"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-e", db_path=db)
    ctx.add_user_message("对话一")
    ctx.reset_system_prompt("新系统提示")

    loaded = ContextManager("conv-e", db_path=db)
    roles = [m["role"] for m in loaded.get_context()]
    assert roles == ["system", "user"]
    assert loaded.get_context()[0]["content"] == "新系统提示"

def test_del_context_and_del_message_persist(tmp_path: Path):
    """del_context / del_message 后保存, 重新加载与内存一致"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-d", db_path=db)
    ctx.add_user_message("一")
    ctx.add_user_message("二")
    ctx.add_user_message("三")

    ctx.del_message(1)   # 删除"二"
    ctx.add_user_message("四")
    ctx.del_last_chat(1)   # 删除最后一组 (四)

    loaded = ContextManager("conv-d", db_path=db)
    assert [m["content"] for m in loaded.get_context()] == ["一", "三"]

def test_external_append_persisted_on_next_save(tmp_path: Path):
    """外部直接 append 消息, 下次保存时尾部插入落库"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-x", db_path=db)
    ctx.add_user_message("一")

    ctx.get_context().append({"role": "user", "content": "外部追加"})
    ctx.save_context()

    loaded = ContextManager("conv-x", db_path=db)
    assert [m["content"] for m in loaded.get_context()] == ["一", "外部追加"]

def test_external_edit_with_mark_dirty_persists(tmp_path: Path):
    """外部修改已保存消息内容 + _mark_dirty, 全量重写落库"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-m", db_path=db)
    ctx.add_user_message("旧内容")

    ctx.get_context()[0]["content"] = "新内容"
    ctx._mark_dirty()
    ctx.save_context()

    loaded = ContextManager("conv-m", db_path=db)
    assert loaded.get_context()[0]["content"] == "新内容"

def test_keep_in_memory_manual_save(tmp_path: Path):
    """keep_in_memory=True 时手动 save_context 落库, 增量/全量均正确"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-k", db_path=db, keep_in_memory=True)
    ctx.add_user_message("一")
    ctx.add_user_message("二")
    ctx.save_context()

    loaded = ContextManager("conv-k", db_path=db)
    assert len(loaded.get_context()) == 2

    ctx.del_context()
    # 编辑后手动保存
    ctx.save_context()
    loaded2 = ContextManager("conv-k", db_path=db)
    assert loaded2.get_context() == []

def test_checkpoint_pointer_still_works_with_incremental_save(tmp_path: Path):
    """增量保存下指针检查点截断恢复仍准确 (行 id 稳定, 水位=COUNT)"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-c", db_path=db, enable_checkpoint=True, auto_checkpoint=False)
    ctx.add_user_message("一")
    ctx.add_user_message("二")
    cp = ctx.create_checkpoint(name="到二")
    assert cp.position == 2

    ctx.add_user_message("三")
    ctx.add_user_message("四")
    ctx.rollback(cp.checkpoint_id)

    assert [m["content"] for m in ctx.get_context()] == ["一", "二"]

@pytest.mark.asyncio
async def test_async_incremental_append(tmp_path: Path):
    """异步版增量保存: 追加落库 + 编辑全量重写"""
    db = str(tmp_path / "chat_history.db")
    ctx = AsyncContextManager("conv-a", db_path=db)
    await ctx.initialize()
    await ctx.add_user_message("一")
    await ctx.add_bot_message("回复一")
    await ctx.add_user_message("二")

    loaded = AsyncContextManager("conv-a", db_path=db)
    await loaded.initialize()
    assert [m["content"] for m in loaded.get_context()] == ["一", "回复一", "二"]

    await loaded.reset_system_prompt("异步系统")
    assert [m["role"] for m in loaded.get_context()] == ["system", "user", "assistant", "user"]

    loaded2 = AsyncContextManager("conv-a", db_path=db)
    await loaded2.initialize()
    assert loaded2.get_context()[0]["content"] == "异步系统"

def test_full_rewrite_failure_restores_dirty_watermark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """全量重写失败后水位恢复为 dirty, 下次保存重试不产生重复行"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-fw", db_path=db)
    ctx.add_user_message("一")
    ctx.add_bot_message("回复一")

    ctx._mark_dirty()   # 模拟编辑, 下次保存走全量重写
    ctx._messages.append({"role": "user", "content": "二"})   # 外部修改

    fake = MagicMock()
    fake.commit.side_effect = RuntimeError("disk full")
    monkeypatch.setattr(ctx, "_get_conn", lambda: fake)
    ctx.save_context()
    assert ctx._saved_count == -1   # 水位恢复到 dirty, 下次仍走全量重写

    monkeypatch.undo()
    ctx.save_context()
    contents = _row_contents(db, "conv-fw")
    assert [m["content"] for m in contents] == ["一", "回复一", "二"]   # 无重复

def test_incremental_failure_keeps_watermark_no_duplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """增量 INSERT 失败后水位不推进, 下次保存重试同批消息无重复"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-iw", db_path=db)
    ctx.add_user_message("一")
    ctx.add_bot_message("回复一")

    ctx._messages.append({"role": "user", "content": "二"})   # 外部追加, 未落库

    fake = MagicMock()
    fake.commit.side_effect = RuntimeError("disk full")
    monkeypatch.setattr(ctx, "_get_conn", lambda: fake)
    ctx.save_context()
    assert ctx._saved_count == 2   # 水位未推进

    monkeypatch.undo()
    ctx.save_context()
    contents = _row_contents(db, "conv-iw")
    assert [m["content"] for m in contents] == ["一", "回复一", "二"]

@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{"结果": 3}, "错误: x\n第二行", "", '{"x": 1}'])
async def test_tool_results_match_and_persist(tmp_path: Path, result: dict[str, Any] | str) -> None:
    """验证三种消息入口的字符串契约和持久化结果"""
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
    """验证同步和异步删除保持系统消息及工具调用分组"""
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
    """验证总结失效字段与即时保存边界"""
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

class _ModelIdentity:
    """仅提供模型名的测试替身"""

    model = "usage-test-model"

def _create_legacy_runtime_table(db_path: Path) -> None:
    """创建尚未包含 cache hit 字段的旧版运行时状态表"""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE context_runtime_state (
                conversation_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                covered_turn_count INTEGER NOT NULL DEFAULT 0,
                summary_model TEXT,
                summary_prompt_version INTEGER NOT NULL DEFAULT 1,
                usage_model TEXT,
                api_input_tokens INTEGER,
                estimated_input_tokens INTEGER,
                api_output_tokens INTEGER,
                api_total_tokens INTEGER,
                updated_at REAL NOT NULL
            )
            """
        )

def test_context_usage_snapshot_returns_current_budgets(tmp_path: Path) -> None:
    """同步 ContextManager 返回当前历史估算, 未记录 usage 时展示空缺"""
    manager = ContextManager(
        "usage-snapshot",
        db_path=str(tmp_path / "context.db"),
        max_context=10000,
        history_ratio=0.6,
        context_threshold=0.8,
        truncation_floor=0.25,
    )
    manager.add_user_message("用于估算历史 token 的消息")

    snapshot = manager.get_context_usage()

    assert isinstance(snapshot, ContextUsageSnapshot)
    assert snapshot.history_tokens == manager.estimate_token()
    assert snapshot.history_token_source == "tokenizer"
    assert snapshot.last_output_tokens is None
    assert snapshot.cache_hit_tokens is None
    assert snapshot.to_dict()["cache_hit_tokens"] is None
    manager.close()

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_context_usage_migrates_and_persists_cache_usage(tmp_path: Path, asynchronous: bool) -> None:
    """同步与异步 ContextManager 都应迁移旧表并持久化最近输出和缓存命中量"""
    db_path = tmp_path / "legacy-usage.db"
    _create_legacy_runtime_table(db_path)
    # cached_tokens 显式为 0: 与未上报 (None) 必须区分
    usage = TokenUsage(input_tokens=120, output_tokens=18, total_tokens=138, cached_tokens=0)
    if asynchronous:
        async with AsyncContextManager("persist", db_path=str(db_path)) as manager:
            await manager.add_user_message("测试 usage 持久化")
            prepared = await manager.prepare_model_context(llm=cast(Any, _ModelIdentity()))
            await manager.record_model_usage(prepared, usage)
        async with AsyncContextManager("persist", db_path=str(db_path)) as loaded:
            snapshot = loaded.get_context_usage()
            assert snapshot.last_output_tokens == 18 and snapshot.cache_hit_tokens == 0
            assert snapshot.history_tokens == loaded.estimate_token()
            prepared = await loaded.prepare_model_context(llm=cast(Any, _ModelIdentity()))
            await loaded.record_model_usage(prepared, None)
            assert loaded.get_context_usage().last_output_tokens is None
            assert loaded.get_context_usage().cache_hit_tokens is None
        async with AsyncContextManager("persist", db_path=str(db_path)) as reloaded:
            # 清空结论同样落盘: 重载后不得回退到更早请求的输出与缓存量
            assert reloaded.get_context_usage().last_output_tokens is None
            assert reloaded.get_context_usage().cache_hit_tokens is None
    else:
        manager = ContextManager("persist", db_path=str(db_path))
        manager.add_user_message("测试 usage 持久化")
        prepared = manager.prepare_model_context(llm=cast(Any, _ModelIdentity()))
        manager.record_model_usage(prepared, usage)
        manager.close()

        loaded = ContextManager("persist", db_path=str(db_path))
        snapshot = loaded.get_context_usage(method="experience")
        assert snapshot.last_output_tokens == 18 and snapshot.cache_hit_tokens == 0
        assert snapshot.history_token_source == "experience"
        loaded.record_model_usage(loaded.prepare_model_context(llm=cast(Any, _ModelIdentity())), None)
        assert loaded.get_context_usage().last_output_tokens is None
        assert loaded.get_context_usage().cache_hit_tokens is None
        loaded.close()
    with sqlite3.connect(db_path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(context_runtime_state)")}
    assert "api_cached_tokens" in columns

def test_context_usage_clears_unavailable_latest_usage(tmp_path: Path) -> None:
    """最新请求未返回 usage 时不得继续展示更早请求的输出和缓存量"""
    db_path = tmp_path / "missing-usage.db"
    manager = ContextManager("missing-usage", db_path=str(db_path))
    manager.add_user_message("测试缺失 usage")
    prepared = manager.prepare_model_context(llm=cast(Any, _ModelIdentity()))
    manager.record_model_usage(
        prepared,
        TokenUsage(output_tokens=9, cached_tokens=3),
    )
    assert manager.get_context_usage().last_output_tokens == 9
    assert manager.get_context_usage().cache_hit_tokens == 3

    manager.record_model_usage(prepared, None)

    snapshot = manager.get_context_usage()
    assert snapshot.last_output_tokens is None
    assert snapshot.cache_hit_tokens is None
    manager.close()

    loaded = ContextManager("missing-usage", db_path=str(db_path))
    assert loaded.get_context_usage().last_output_tokens is None
    assert loaded.get_context_usage().cache_hit_tokens is None
    loaded.close()

@pytest.fixture
def old_database(tmp_path):
    database = tmp_path / "legacy.db"
    contexts = []
    for identity, prompt in (("sub_agent_candidate", SUB_AGENT_SYSTEM_PROMPT), ("sub_agent_custom", "自定义任务"), ("formal", "正常对话")):
        context = ContextManager(identity, db_path=str(database))
        context.reset_system_prompt(prompt)
        contexts.append(context)
    yield database
    for context in contexts:
        context.close()

def test_exact_cleanup_backup_restore_and_conflict(old_database, tmp_path):
    service = LegacyContextCleanup(old_database)
    preview = service.preview()
    assert set(preview["candidates"]) == {"sub_agent_candidate"}
    backup = tmp_path / "backup.json"
    assert service.apply(preview, backup)["deleted"] == 1
    with closing(sqlite3.connect(old_database)) as connection:
        assert set(row[0] for row in connection.execute("SELECT DISTINCT conversation_id FROM chat_history")) == {"sub_agent_custom", "formal"}
        assert not connection.execute("SELECT 1 FROM context_catalog WHERE context_id='sub_agent_candidate'").fetchone()
    assert service.restore(backup)["restored"] == 1
    assert service.preview()["revision"] == preview["revision"]
    with pytest.raises(ValueError, match="占用"):
        service.restore(backup)

def test_formal_reference_blocks_cleanup(old_database):
    with closing(sqlite3.connect(old_database)) as connection, connection:
        connection.execute("CREATE TABLE session_configs (session_id TEXT)")
        connection.execute("INSERT INTO session_configs VALUES ('sub_agent_candidate')")
    preview = LegacyContextCleanup(old_database).preview()
    assert not preview["candidates"]
    assert any(row["reason"] == "存在正式引用" for row in preview["rejected"])

def test_new_message_between_preview_and_apply_aborts_entire_operation(old_database, tmp_path):
    service = LegacyContextCleanup(old_database)
    preview = service.preview()
    with closing(sqlite3.connect(old_database)) as connection, connection:
        connection.execute("INSERT INTO chat_history (conversation_id, role, content) VALUES ('sub_agent_candidate', 'user', '新消息')")
    with pytest.raises(ValueError, match="变化"):
        service.apply(preview, tmp_path / "backup.json")
    assert not (tmp_path / "backup.json").exists()
    with closing(sqlite3.connect(old_database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM chat_history WHERE conversation_id='sub_agent_candidate'").fetchone()[0] == 2

def test_backup_write_failure_does_not_delete(old_database, tmp_path):
    service = LegacyContextCleanup(old_database)
    backup = tmp_path / "occupied.json"
    backup.write_text("原始备份", encoding="utf-8")
    preview = service.preview()
    with pytest.raises(FileExistsError):
        service.apply(preview, backup)
    assert service.preview()["revision"] == preview["revision"]

def test_bad_checksum_prevents_restore(old_database, tmp_path):
    service = LegacyContextCleanup(old_database)
    backup = tmp_path / "backup.json"
    service.apply(service.preview(), backup)
    backup.write_text(backup.read_text(encoding="utf-8").replace(SUB_AGENT_SYSTEM_PROMPT.strip(), "篡改提示词"), encoding="utf-8")
    with pytest.raises(ValueError, match="校验"):
        service.restore(backup)
    assert not service.preview()["candidates"]

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_memory_context_never_opens_sql_even_for_summary_and_usage(tmp_path, monkeypatch, asynchronous: bool):
    """纯内存上下文 (同步/异步) 在摘要与状态保存时都不访问 SQLite"""
    def forbidden(*args, **kwargs):
        raise AssertionError("纯内存上下文不应访问 SQLite")

    if asynchronous:
        import aiosqlite

        monkeypatch.setattr(aiosqlite, "connect", forbidden)
        context = AsyncContextManager("memory", db_path=str(tmp_path / "absent/db.sqlite"), persistent=False)
        await context.initialize()
        await context.reset_system_prompt("系统")
        for index in range(4):
            await context.add_user_message(f"问题{index}")
            await context.add_bot_message(f"回答{index}")
        model = MagicMock()
        model.chat = AsyncMock(return_value="摘要")
        assert await context.summarize_and_compress(model, keep_recent_turns=1) == "摘要"
        context._runtime_state.api_input_tokens = 42
        context._runtime_state_dirty = True
        await context._save_runtime_state()
        await context.save_context(raise_on_error=True)
        await context.load_context()
    else:
        monkeypatch.setattr(sqlite3, "connect", forbidden)
        context = ContextManager("memory", db_path=str(tmp_path / "absent/db.sqlite"), persistent=False)
        context.reset_system_prompt("系统")
        for index in range(4):
            context.add_user_message(f"问题{index}")
            context.add_bot_message(f"回答{index}")
        model = MagicMock()
        model.chat.return_value = "摘要"
        assert context.summarize_and_compress(model, keep_recent_turns=1) == "摘要"
        context._runtime_state.api_input_tokens = 42
        context._runtime_state_dirty = True
        context._save_runtime_state()
        context.save_context(raise_on_error=True)
        context.load_context()
        context.close()
    assert len(context.get_context()) == 9
    assert context._runtime_state.api_input_tokens == 42
    assert not (tmp_path / "absent").exists()

@pytest.mark.parametrize("context_type", [ContextManager, AsyncContextManager])
def test_memory_rejects_persistent_checkpoint(context_type, tmp_path):
    with pytest.raises(ValueError, match="检查点"):
        context_type("memory", persistent=False, enable_checkpoint=True, db_path=str(tmp_path / "absent.sqlite"))
    assert not (tmp_path / "absent.sqlite").exists()

@pytest.mark.parametrize("workflow_type", [ModelWorkflowFramework, AsyncModelWorkflowFramework])
def test_memory_rejects_recovery(workflow_type):
    with pytest.raises(ValueError, match="恢复"):
        workflow_type(MagicMock(), "memory", persist_context=False, recoverable=True)

@pytest.mark.asyncio
async def test_both_subagent_models_use_memory(monkeypatch):
    import aiosqlite

    def forbidden(*args, **kwargs):
        raise AssertionError("子代理不应访问 SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(aiosqlite, "connect", forbidden)
    sync_agent = SubAgentModel(MagicMock(), "sync", MagicMock())
    async_agent = AsyncSubAgentModel(MagicMock(), "async", MagicMock())
    await async_agent.initialize()
    assert not sync_agent.ctx.persistent
    assert not async_agent.ctx.persistent
    assert sync_agent.ctx.get_context()[0]["role"] == "system"
    assert async_agent.ctx.get_context()[0]["role"] == "system"

def test_test_storage_isolation_inherited_by_child_process():
    root = Path(os.environ["SATRAP_DATA_ROOT"]).resolve()
    assert root in Path(get_db_path()).resolve().parents
    result = subprocess.run(
        [sys.executable, "-c", "from satrap.core.utils.paths import get_db_path; print(get_db_path())"],
        encoding="utf-8", capture_output=True, check=True, env=os.environ.copy(),
    )
    assert root in Path(result.stdout.strip().splitlines()[-1]).resolve().parents


@pytest.mark.parametrize("version", ["initial", "timed", "current"])
def test_one_time_request_cleanup_covers_history_and_every_restore_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    """一次性清洗保留记忆, 目标, 身份和媒体, 检查点及备份恢复不回灌资料"""
    database = tmp_path / "platform" / "platform.db"
    database.parent.mkdir()
    context = ContextManager("scope_main", db_path=str(database), enable_checkpoint=True, auto_checkpoint=False)
    data = {"bot": {"self_id": "10", "nickname": "机器人", "group_card": "助手"},
            "conversation": {"kind": "group", "chat_id": "20"}, "speaker_id": "30", "source_message_id": "40",
            "quotes": [], "mentions": ["10"], "capabilities": {"text": {"state": "supported", "reason": "adapter_implemented"}}}
    if version != "initial":
        data["current_time"] = "2026-10-12T00:07:30+08:00"
    data["available_group_chat_tools" if version == "current" else "available_tools"] = []
    goal = "【持续目标】\n<active-goal>完成目标</active-goal>\n" + request_cleanup.GOAL_END
    memory = "【长期记忆】\n<long-term-memory>\n- [约定] 保留此条\n</long-term-memory>\n"
    if version == "timed":
        memory = request_cleanup.MEMORY_HEADER + json.dumps({"memories": [], "has_omitted": True}) + "\n\n"
    env = request_cleanup.GROUP_HEADER + json.dumps(data, ensure_ascii=False) + "\n\n"
    if version == "current":
        env += request_cleanup.TOOLS_NOTICE + "\n\n"
    legacy = goal + memory + env + "[你当前的平台机器人身份: 账号 ID 10, 账号昵称 机器人, 本群昵称 助手]\n[用户 小明 (ID 30), 消息 40] 正文"
    context.add_user_message(legacy, img_urls=["https://example.com/picture.png"])
    context.add_bot_message("回复")
    original = copy.deepcopy(context.get_context())
    assert context.state_store is not None
    checkpoint = context.state_store.create_checkpoint(StateScope("conversation", "scope_main"), materialize=True)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE conversation_data_backups (id TEXT PRIMARY KEY,conversation_id TEXT,layer TEXT,payload TEXT,reason TEXT,created_at REAL)")
        db.execute("INSERT INTO conversation_data_backups VALUES ('backup','scope_main','context',?,'test',0)", (json.dumps(original, ensure_ascii=False),))
        db.execute("CREATE TABLE display_turns (id INTEGER PRIMARY KEY,conversation_id TEXT)")
        db.execute("CREATE TABLE display_turn_variants (id INTEGER PRIMARY KEY,turn_id INTEGER,context_messages TEXT)")
        db.execute("INSERT INTO display_turns VALUES (1,'scope_main')")
        db.execute("INSERT INTO display_turn_variants VALUES (1,1,?)", (json.dumps(original, ensure_ascii=False),))
        db.execute("INSERT INTO context_runtime_state (conversation_id,summary,covered_turn_count,updated_at) VALUES ('scope_main','旧环境摘要',1,0)")
        records = {table: [dict(zip([field[0] for field in cursor.description], row)) for row in cursor.fetchall()]
                   for table in ("chat_history", "state_snapshots", "conversation_data_backups", "display_turns", "display_turn_variants", "context_runtime_state")
                   for cursor in [db.execute(f"SELECT * FROM {table}")]}
    archive = database.parent / "trash" / "sessions" / "old"
    archive.mkdir(parents=True)
    raw = json.dumps(records, ensure_ascii=False).encode("utf-8")
    (archive / "records.json").write_bytes(raw)
    (archive / "manifest.json").write_text(json.dumps({"archive_version": 2, "records_sha256": request_cleanup.digest(raw)}), encoding="utf-8")
    initial_ids = _row_ids(str(database), "scope_main")
    context.close()
    monkeypatch.setattr(request_cleanup, "recognizable_writers", lambda: [])
    report = tmp_path / "report.json"
    assert request_cleanup.main(["--database", str(database), "--report", str(report)]) == 0
    assert request_cleanup.preview_database(database).changes
    assert request_cleanup.main(["--database", str(database), "--apply", "--offline-confirmed", "--backup-dir", str(tmp_path / "external-backup"), "--report", str(report)]) == 0
    assert json.loads(report.read_text(encoding="utf-8"))["completed"]
    assert _row_ids(str(database), "scope_main") == initial_ids
    cleaned = ContextManager("scope_main", db_path=str(database), enable_checkpoint=True, auto_checkpoint=False)
    clean_body = str(cleaned.get_context()[0]["content"])
    assert "完成目标" in clean_body and ("保留此条" in clean_body or "has_omitted" in clean_body)
    assert "小明 (ID 30)" in clean_body and "消息 40" in clean_body and "正文" in clean_body
    assert "群聊环境资料" not in clean_body and "你当前的平台机器人身份" not in clean_body
    assert "https://example.com/picture.png" in clean_body
    assert not request_cleanup.preview_database(database).changes
    assert not request_cleanup.preview_archive(archive / "records.json").changed
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT summary,covered_turn_count FROM context_runtime_state WHERE conversation_id='scope_main'").fetchone() == ("", 0)
        increment = json.loads(db.execute("SELECT context_messages FROM display_turn_variants").fetchone()[0])
    fork = ContextManager("fork_main", db_path=str(database))
    fork.add_turn_messages(increment)
    assert "群聊环境资料" not in str(fork.get_context())
    fork.close()
    cleaned.rollback(checkpoint.checkpoint_id)
    assert "群聊环境资料" not in str(cleaned.get_context())
    cleaned.close()
    store = ConversationDataService(database)
    current = store.read("scope_main", "context")
    store.mutate("scope_main", "context", {"action": "restore", "backup_id": "backup", "expected_revision": current["revision"]})
    assert not request_cleanup.preview_database(database).changes
    backup = Path(json.loads(report.read_text(encoding="utf-8"))["backup"]) / "database-0.db"
    assert request_cleanup.preview_database(backup).changes


@pytest.mark.parametrize("status", ["running", "interrupted", "needs_attention", "failed"])
def test_one_time_request_cleanup_blocks_unfinished_workflows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    """实际工作流 scope 的未完成任务阻止清洗, 不修改指纹强行恢复"""
    from satrap.core.framework.Base.execution.store import RunStore
    database = tmp_path / "platform" / "platform.db"
    database.parent.mkdir()
    context = ContextManager("scope_main", db_path=str(database))
    context.add_user_message("[你当前的平台机器人身份: 账号 ID 10, 账号昵称 机器人]\n原文")
    context.close()
    runs = RunStore(database, "scope_main")
    run_id = runs.create({"user_input": "任务"}, runs.history_signature(), "configuration")
    runs.update(run_id, status=status)
    monkeypatch.setattr(request_cleanup, "recognizable_writers", lambda: [])
    before = _row_contents(str(database), "scope_main")
    assert request_cleanup.main(["--database", str(database), "--apply", "--offline-confirmed", "--backup-dir", str(tmp_path / "external-backup")]) == 2
    assert _row_contents(str(database), "scope_main") == before
    assert not (tmp_path / "external-backup").exists()


def test_one_time_request_cleanup_preserves_ambiguous_text_and_rejects_online_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户引用, 未知版本和活跃进程不会被宽泛匹配或强行清洗"""
    quoted = "用户说:\n[你当前的平台机器人身份: 账号 ID 10, 账号昵称 机器人]\n原文"
    assert request_cleanup.clean_text(quoted).text == quoted
    ambiguous = "【持续目标】\n缺少结束标记\n" + request_cleanup.GROUP_HEADER + "{}\n\n正文"
    result = request_cleanup.clean_text(ambiguous)
    assert result.text == ambiguous and result.issue and not result.removed
    database = tmp_path / "platform" / "platform.db"
    database.parent.mkdir()
    context = ContextManager("scope_main", db_path=str(database))
    context.add_user_message("[你当前的平台机器人身份: 账号 ID 10, 账号昵称 机器人]\n原文")
    context.close()
    with pytest.raises(ValueError, match="offline-confirmed"):
        request_cleanup.main(["--database", str(database), "--apply"])
    monkeypatch.setattr(request_cleanup, "recognizable_writers", lambda: [{"pid": 123, "name": "python.exe"}])
    assert request_cleanup.main(["--database", str(database), "--apply", "--offline-confirmed", "--backup-dir", str(tmp_path / "external-backup")]) == 2
    assert request_cleanup.preview_database(database).changes


def test_one_time_skill_cleanup_preserves_user_quotes_and_restore_copies(tmp_path, monkeypatch):
    """旧技能块仅在系统尾部完整匹配时移除, 检查点和备份不会恢复常驻说明"""
    from satrap.core.utils.skills import Skill
    folder = tmp_path / "known-skills" / "weekly"
    folder.mkdir(parents=True)
    source = folder / "SKILL.md"
    source.write_text("---\nname: weekly\ndescription: 周报整理\n---\n唯一旧正文", encoding="utf-8")
    block = Skill.from_file(str(source)).to_text()
    database = tmp_path / "platform" / "platform.db"
    database.parent.mkdir()
    context = ContextManager("skill_main", db_path=str(database), enable_checkpoint=True, auto_checkpoint=False)
    context.reset_system_prompt("保留基础指令\n\n" + block)
    context.add_user_message(block)
    assert context.state_store is not None
    checkpoint = context.state_store.create_checkpoint(StateScope("conversation", "skill_main"), materialize=True)
    original = copy.deepcopy(context.get_context())
    context.close()
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE conversation_data_backups (id TEXT,conversation_id TEXT,layer TEXT,payload TEXT)")
        db.execute("INSERT INTO conversation_data_backups VALUES ('old','skill_main','context',?)", (json.dumps(original),))
        db.execute("CREATE TABLE display_turns (id INTEGER, conversation_id TEXT)")
        db.execute("CREATE TABLE display_turn_variants (id INTEGER,turn_id INTEGER,context_messages TEXT)")
        db.execute("INSERT INTO display_turns VALUES (1,'skill_main')")
        db.execute("INSERT INTO display_turn_variants VALUES (1,1,?)", (json.dumps(original),))
        records = {table: [dict(zip([column[0] for column in cursor.description], row)) for row in cursor.fetchall()]
                   for table in ("chat_history", "state_snapshots", "conversation_data_backups", "display_turns", "display_turn_variants")
                   for cursor in [db.execute(f"SELECT * FROM {table}")]}
    archive = database.parent / "trash" / "sessions" / "old-skills"
    archive.mkdir(parents=True)
    raw = json.dumps(records).encode("utf-8")
    (archive / "records.json").write_bytes(raw)
    (archive / "manifest.json").write_text(json.dumps({"archive_version": 2, "records_sha256": request_cleanup.digest(raw)}), encoding="utf-8")
    monkeypatch.setattr(request_cleanup, "recognizable_writers", lambda: [])
    monkeypatch.setattr(request_cleanup, "SKILL_BLOCKS", None)
    flags = ["--database", str(database), "--skills", "--skill-root", str(folder.parent)]
    assert request_cleanup.main(flags) == 0
    plan = request_cleanup.preview_database(database)
    assert {change.table for change in plan.changes} == {"chat_history", "state_snapshots", "conversation_data_backups", "display_turn_variants"}
    assert request_cleanup.preview_archive(archive / "records.json").changed
    assert request_cleanup.main([*flags, "--apply", "--offline-confirmed", "--backup-dir", str(tmp_path / "backup")]) == 0
    restored = ContextManager("skill_main", db_path=str(database), enable_checkpoint=True, auto_checkpoint=False)
    assert restored.get_context()[0]["content"] == "保留基础指令"
    assert restored.get_context()[1]["content"] == block
    restored.rollback(checkpoint.checkpoint_id)
    assert restored.get_context()[0]["content"] == "保留基础指令"
    restored.close()
    assert not request_cleanup.preview_database(database).changes
    assert not request_cleanup.preview_archive(archive / "records.json").changed
