from unittest.mock import AsyncMock, MagicMock
from pathlib import Path
import subprocess
import sqlite3
import asyncio
import pytest
import sys
import os

from satrap.core.framework.Base import ModelWorkflowFramework, AsyncModelWorkflowFramework
from satrap.core.utils.context import ContextManager, AsyncContextManager
from satrap.expend.tools.agent import SubAgentModel, AsyncSubAgentModel
from satrap.core.utils.paths import get_db_path


def test_memory_context_never_opens_sql_even_for_summary_and_usage(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("纯内存上下文不应访问 SQLite")

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
    assert len(context.get_context()) == 9
    assert context._runtime_state.api_input_tokens == 42
    context.close()
    assert not (tmp_path / "absent").exists()


@pytest.mark.asyncio
async def test_async_memory_context_never_opens_sql(tmp_path, monkeypatch):
    import aiosqlite

    def forbidden(*args, **kwargs):
        raise AssertionError("纯内存上下文不应访问 SQLite")

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
