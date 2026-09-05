"""ContextManager 上下文预算与最近 usage 快照测试"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import pytest
from typing import Any, cast

from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap.core.type import ContextUsageSnapshot, TokenUsage


class _ModelIdentity:
    """仅提供模型名的测试替身"""

    model = "usage-test-model"


def _create_legacy_runtime_table(db_path: Path) -> None:
    """
    创建尚未包含 cache hit 字段的旧版运行时状态表

    参数:
    - db_path: SQLite 数据库路径
    """
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
    """
    同步 ContextManager 应返回当前历史估算和全部预算字段

    参数:
    - tmp_path: 临时目录
    """
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
    assert snapshot.context_window_tokens == 10000
    assert snapshot.reserved_output_tokens == 4000
    assert snapshot.history_upper_tokens == 6000
    assert snapshot.history_lower_tokens == 1500
    assert snapshot.last_output_tokens is None
    assert snapshot.cache_hit_tokens is None
    assert snapshot.history_token_source == "tokenizer"
    assert snapshot.to_dict()["cache_hit_tokens"] is None
    manager.close()


def test_sync_context_usage_migrates_and_persists_cache_usage(tmp_path: Path) -> None:
    """
    同步 ContextManager 应迁移旧表并持久化最近输出和缓存命中量

    参数:
    - tmp_path: 临时目录
    """
    db_path = tmp_path / "legacy-sync.db"
    _create_legacy_runtime_table(db_path)
    manager = ContextManager("sync-persist", db_path=str(db_path))
    manager.add_user_message("测试同步 usage 持久化")
    prepared = manager.prepare_model_context(llm=cast(Any, _ModelIdentity()))
    manager.record_model_usage(
        prepared,
        TokenUsage(
            input_tokens=120,
            output_tokens=18,
            total_tokens=138,
            cached_tokens=64,
        ),
    )
    manager.close()

    loaded = ContextManager("sync-persist", db_path=str(db_path))
    snapshot = loaded.get_context_usage(method="experience")

    assert snapshot.last_output_tokens == 18
    assert snapshot.cache_hit_tokens == 64
    assert snapshot.history_token_source == "experience"
    with sqlite3.connect(db_path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(context_runtime_state)")}
    assert "api_cached_tokens" in columns
    loaded.close()


def test_context_usage_clears_unavailable_latest_usage(tmp_path: Path) -> None:
    """
    最新请求未返回 usage 时不得继续展示更早请求的输出和缓存量

    参数:
    - tmp_path: 临时目录
    """
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


@pytest.mark.asyncio
async def test_async_context_usage_migrates_and_persists_zero_cache_hit(tmp_path: Path) -> None:
    """
    异步 ContextManager 应保留明确的零缓存命中并支持旧表迁移

    参数:
    - tmp_path: 临时目录
    """
    db_path = tmp_path / "legacy-async.db"
    _create_legacy_runtime_table(db_path)
    async with AsyncContextManager("async-persist", db_path=str(db_path)) as manager:
        await manager.add_user_message("测试异步 usage 持久化")
        prepared = await manager.prepare_model_context(llm=cast(Any, _ModelIdentity()))
        await manager.record_model_usage(
            prepared,
            TokenUsage(
                input_tokens=80,
                output_tokens=11,
                total_tokens=91,
                cached_tokens=0,
            ),
        )

    async with AsyncContextManager("async-persist", db_path=str(db_path)) as loaded:
        snapshot = loaded.get_context_usage()

        assert snapshot.last_output_tokens == 11
        assert snapshot.cache_hit_tokens == 0
        assert snapshot.history_tokens == loaded.estimate_token()
        prepared = await loaded.prepare_model_context(llm=cast(Any, _ModelIdentity()))
        await loaded.record_model_usage(prepared, None)
        assert loaded.get_context_usage().last_output_tokens is None
        assert loaded.get_context_usage().cache_hit_tokens is None

    async with AsyncContextManager("async-persist", db_path=str(db_path)) as reloaded:
        assert reloaded.get_context_usage().last_output_tokens is None
        assert reloaded.get_context_usage().cache_hit_tokens is None
