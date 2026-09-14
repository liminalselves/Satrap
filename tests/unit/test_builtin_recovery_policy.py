"""恢复策略影响未知副作用是否自动重试, 在公开同步与异步工具入口校验边界"""
from types import SimpleNamespace
from typing import Any, cast

import pytest

from satrap.core.utils.skills.tool import SkillTool
from satrap.expend.plugins.base_take import tools as base_tools
from satrap.expend.plugins.satrap_coding import tools as coding_tools
from satrap.expend.plugins.rag import tools as rag_tools
from satrap.expend.tools import search, sandbox_tools, agent
from satrap import AsyncSimpleSession
from satrap.core.APICall.LLMCall import AsyncLLM


@pytest.mark.parametrize("module,names,policy", [
    (coding_tools, ("ReadFileTool", "ListDirTool", "GlobFilesTool", "GrepFilesTool"), "retry"),
    (coding_tools, ("WriteFileTool", "EditFileTool", "SearchReplaceTool", "TodoWriteTool", "AskUserTool", "ShellTool", "SubAgentTool"), "manual"),
    (base_tools, ("ReadDocumentTool", "ListMemoriesTool"), "retry"),
    (base_tools, ("AddMemoryTool", "UpdateMemoryTool", "DeleteMemoryTool"), "manual"),
    (search, ("SearchTool",), "retry"),
    (search, ("FetchPageTool",), "manual"),
    (sandbox_tools, ("CodeSandboxTool",), "manual"),
    (agent, ("SubAgent",), "manual"),
])
def test_public_sync_async_policy_boundary(module, names, policy):
    for name in names:
        assert getattr(module, name).recovery_policy == policy
        assert getattr(module, "Async" + name).recovery_policy == policy
    assert SkillTool.recovery_policy == "retry"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_rag_factory_only_retries_queries(tmp_path, monkeypatch, asynchronous):
    service = SimpleNamespace(validate_references=lambda config: config)
    monkeypatch.setattr(rag_tools, "RagService", lambda *args: service)
    session: Any = AsyncSimpleSession("probe", cast(AsyncLLM, object()), db_path=str(tmp_path / "p.db")) if asynchronous else SimpleNamespace(session_id="probe")
    session.storage_layout = object()
    session.plugin_model_manager = object()
    session.storage_platform_id = "probe"
    tools = rag_tools.get_tools(session, {})
    assert {tool.tool_name: tool.recovery_policy for tool in tools} == {
        "rag_search": "retry", "rag_list": "retry", "rag_ingest": "manual",
    }
