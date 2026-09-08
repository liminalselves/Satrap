"""验证拆包后的旧导入路径和会话可信来源约束"""
from importlib import import_module
from pathlib import Path
from setuptools import find_packages

import pytest

from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.Base import Session


@pytest.fixture(scope="module")
def packaged_modules() -> set[str]:
    """按项目的 setuptools 配置检查真实打包发现结果"""
    root = Path(__file__).resolve().parents[2]
    return set(find_packages(where=str(root), include=["satrap", "satrap.*"]))


@pytest.mark.parametrize("module,names", [
    ("satrap.core.APICall.EmbedCall", ["Embedding", "AsyncEmbedding", "parse_embedding_response"]),
    ("satrap.core.APICall.ReRankCall", ["ReRank", "AsyncReRank", "parse_rerank_result"]),
    ("satrap.core.framework.Base", ["Session", "AsyncSession", "ModelWorkflowFramework", "AsyncModelWorkflowFramework"]),
    ("satrap.core.framework.command", ["CommandHandler", "AsyncCommandHandler"]),
    ("satrap.core.utils.TCBuilder", ["Tool", "AsyncTool", "ToolsManager", "AsyncToolsManager", "create_tool_defined"]),
    ("satrap.core.utils.context", ["ContextManager", "AsyncContextManager", "PreparedModelContext", "add_bot_message"]),
    ("satrap.core.utils.skills", ["Skill", "SkillsManager", "SkillTool"]),
    ("satrap.core.utils.outbound", ["safe_sync_get", "safe_async_get", "resolve_outbound_http_url"]),
    ("satrap.core.utils.mcp", ["MCPClient", "MCPToolAdapter", "SyncMCPToolAdapter", "export_tools_to_mcp"]),
    ("satrap.edictum.simple_session", ["SimpleSession", "AsyncSimpleSession", "HandlerResult", "SessionHandler"]),
    ("satrap.expend.tools.agent", ["SubAgent", "AsyncSubAgent", "SubAgentModel", "AsyncSubAgentModel"]),
    ("satrap.expend.tools.search", ["SearchTool", "AsyncSearchTool", "FetchPageTool", "AsyncFetchPageTool"]),
    ("satrap.expend.tools.sandbox_tools", ["CodeSandboxTool", "AsyncCodeSandboxTool"]),
    ("satrap.expend.plugins.base_take.tools", ["get_tools", "ReadDocumentTool", "AsyncReadDocumentTool", "AddMemoryTool", "AsyncAddMemoryTool"]),
    ("satrap.expend.plugins.satrap_coding.tools", ["get_tools", "ReadFileTool", "AsyncReadFileTool", "WriteFileTool", "AsyncWriteFileTool"]),
])
def test_legacy_imports_and_package_discovery(module: str, names: list[str], packaged_modules: set[str]):
    assert module in packaged_modules
    loaded = import_module(module)
    assert loaded.__spec__ is not None and loaded.__spec__.submodule_search_locations is not None
    for name in names:
        assert callable(getattr(loaded, name))


@pytest.mark.parametrize("class_path", [
    "satrap.core.framework.Base.Session",
    "satrap.core.framework.Base.AsyncSession",
    "satrap.edictum.simple_session.SimpleSession",
    "satrap.edictum.simple_session.AsyncSimpleSession",
])
def test_legacy_session_class_paths_remain_trusted(tmp_path: Path, class_path: str):
    manager = SessionClassConfigManager(storage_path=tmp_path / "sessions.json")
    module, name = class_path.rsplit(".", 1)
    assert manager._load_class(class_path) is getattr(import_module(module), name)


def test_session_reexport_cannot_escape_its_package(tmp_path: Path, monkeypatch):
    class ExternalSession(Session):
        pass

    module = import_module("satrap.core.framework.Base")
    monkeypatch.setattr(module, "Session", ExternalSession)
    manager = SessionClassConfigManager(storage_path=tmp_path / "sessions.json")
    with pytest.raises(ValueError, match="不是模块内声明"):
        manager._load_class("satrap.core.framework.Base.Session")
