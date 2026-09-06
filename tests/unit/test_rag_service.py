"""RAG 分层访问, 来源, 重建与失败语义的离线验证"""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import threading
from pathlib import Path
import sqlite3
import pytest
from typing import Any
from types import SimpleNamespace
import yaml

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.storage.session_fork import fork_session_settings
from satrap.edictum.plugin_settings import PluginSettingsService
from satrap.edictum.plugin_config import PluginConfigManager, parse_config_schema
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.core.utils.TCBuilder import AsyncTool
from satrap.core.storage import StorageLayout, StorageMaintenanceService
from satrap.core.type import EmbeddingConfig, ReRankConfig
from satrap.core.rag import RagService
from satrap.edictum import AsyncSimpleSession, SimpleSession


@pytest.fixture
def environment(tmp_path, monkeypatch):
    layout = StorageLayout(tmp_path / "data")
    models = ModelConfigManager(tmp_path / "models.json", auto_create=False)
    models.set_embedding_config(EmbeddingConfig(model="fruit", api_key="test", dimensions=3), "embed")
    models.set_rerank_config(ReRankConfig(model="rank", api_key="test", base_url="http://localhost:1234"), "rank")
    flags = {"fail_embed": False, "fail_rerank": False}

    class Embed:
        client = SimpleNamespace(close=lambda: None)

        def embed(self, texts):
            if flags["fail_embed"]:
                raise ValueError("向量服务失败")
            def vector(text):
                return [1.0, 0.0, 0.2] if "苹果" in text else [0.0, 1.0, 0.2]
            return [vector(text) for text in texts] if isinstance(texts, list) else vector(texts)

    class Rank:
        def call(self, query, documents, **kwargs):
            if flags["fail_rerank"]:
                raise ValueError("重排服务失败")
            return [{"original_index": index, "score": 1 - index / 10} for index in reversed(range(len(documents)))]

    monkeypatch.setattr("satrap.core.rag.build_model_client", lambda kind, config: Embed() if kind == "embed" else Rank())
    return layout, models, flags


def make_library(service, name="知识库", scope="session", **config):
    return service.create(name, scope, {"embed": "embed", **config})["id"]


def test_scopes_union_and_source_deduplication(environment):
    layout, models, _ = environment
    service = RagService(layout, models, "chat", "one")
    local = make_library(service)
    shared = make_library(service, "共享库", "global")
    foreign = make_library(RagService(layout, models, "chat", "two"))
    assert not layout.session_root("chat", "one").exists()
    service.ingest(local, "苹果的资料", "local.md")
    service.ingest(shared, "苹果的资料", "global.md")
    config = {"db_scope": "session_global", "global_db_ids": [shared]}
    result = service.search("苹果", config)
    assert len(result["results"]) == 1
    assert {item["scope"] for item in result["results"][0]["sources"]} == {"session", "global"}
    assert "source_text" not in str(result)
    with pytest.raises(ValueError, match="其他会话"):
        service.documents(foreign)
    with pytest.raises(ValueError, match="允许"):
        service.search("苹果", config, [foreign])
    assert service.write_target(config) == local
    selected_local = make_library(service, "选择的会话库")
    assert service.write_target({**config, "session_db_ids": [selected_local]}) == selected_local


def test_empty_result_and_missing_library_are_distinct(environment):
    layout, models, _ = environment
    service = RagService(layout, models, "chat", "one")
    with pytest.raises(ValueError, match="未配置"):
        service.search("苹果", {})
    make_library(service)
    assert service.search("苹果", {})["status"] == "empty"


def test_atomic_source_replace_and_embedding_failure(environment):
    layout, models, flags = environment
    service = RagService(layout, models, "chat", "one")
    kb = make_library(service, duplicate_policy="replace")
    first = service.ingest(kb, "苹果旧资料", "document.md")
    flags["fail_embed"] = True
    with pytest.raises(ValueError, match="向量服务失败"):
        service.ingest(kb, "香蕉新资料", "document.md")
    assert service.documents(kb, include_text=True)[0]["source_text"] == "苹果旧资料"
    flags["fail_embed"] = False
    service.ingest(kb, "香蕉新资料", "document.md")
    assert len(service.documents(kb)) == 1
    assert service.documents(kb, include_text=True)[0]["source_text"] == "香蕉新资料"
    assert service.delete_document(kb, first["source_id"])
    assert service.search("香蕉", {})["status"] == "empty"


def test_skip_policy_and_model_identity_change(environment):
    layout, models, _ = environment
    service = RagService(layout, models, "chat", "one")
    kb = make_library(service)
    service.ingest(kb, "苹果资料", "document.md")
    assert service.ingest(kb, "苹果新资料", "document.md")["status"] == "skipped"
    models.set_embedding_config(EmbeddingConfig(model="other", api_key="test", dimensions=3), "embed")
    with pytest.raises(ValueError, match="重建"):
        service.search("苹果", {})
    revision = service.list()[0]["revision"]
    service.rebuild(kb, {"embed": "embed"}, revision)
    assert service.search("苹果", {})["status"] == "found"
    _, row = service._get(kb)
    assert [path.name for path in service._root(row).iterdir() if path.is_dir()] == [row["generation"]]


def test_failed_rebuild_keeps_generation_and_data(environment):
    layout, models, flags = environment
    service = RagService(layout, models, "chat", "one")
    kb = make_library(service)
    service.ingest(kb, "苹果资料", "document.md")
    before = service.list()[0]
    flags["fail_embed"] = True
    with pytest.raises(ValueError):
        service.rebuild(kb, {"embed": "embed", "chunk_size": 10, "chunk_overlap": 2}, before["revision"])
    after = service.list()[0]
    assert after["generation"] == before["generation"]
    assert after["config"] == before["config"]
    flags["fail_embed"] = False
    assert service.search("苹果", {})["status"] == "found"
    _, row = service._get(kb)
    assert [path.name for path in service._root(row).iterdir() if path.is_dir()] == [row["generation"]]


def test_delete_library_removes_only_its_index_files(environment):
    layout, models, _ = environment
    service = RagService(layout, models, "chat", "one")
    local, shared = make_library(service), make_library(service, "共享", "global")
    service.ingest(local, "苹果资料", "local.md")
    service.ingest(shared, "共享资料", "shared.md")
    _, row = service._get(local)
    root = service._root(row)
    service.delete(local)
    assert not root.exists()
    assert not list(root.parent.glob(".deleted-*"))
    assert service.documents(shared)[0]["source"] == "shared.md"
    with pytest.raises(ValueError, match="不存在"):
        service.documents(local)


def test_archive_waits_for_active_ingest_and_restores_completed_data(environment, monkeypatch):
    layout, models, _ = environment
    service = RagService(layout, models, "chat", "one")
    kb = make_library(service)
    entered, release = threading.Event(), threading.Event()
    original = service._embed_source
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(service, "_embed_source", blocked)
    maintenance = StorageMaintenanceService(layout)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(service.ingest, kb, "苹果最新资料", "document.md")
        assert entered.wait(5)
        archive = executor.submit(maintenance.archive_session, "chat", "one")
        try:
            with pytest.raises(TimeoutError):
                archive.result(timeout=0.1)
        finally:
            release.set()
        assert writer.result(timeout=5)["status"] == "indexed"
        saved = archive.result(timeout=5)
    assert service.list() == []
    maintenance.restore_archive("chat", saved["archive_id"])
    assert service.documents(kb, include_text=True)[0]["source_text"] == "苹果最新资料"


def test_default_library_change_invalidates_previous_editors(environment):
    layout, models, _ = environment
    service = RagService(layout, models, "chat", "one")
    first, second = make_library(service), make_library(service, "第二个库")
    original = {item["id"]: item for item in service.list()}
    service.update(second, "第二个库", original[second]["config"], original[second]["revision"], True)
    assert service.write_target({}) == second
    with pytest.raises(ValueError, match="已更新"):
        service.update(first, "旧编辑器", original[first]["config"], original[first]["revision"], True)


def test_rerank_fallback_and_result_budget(environment):
    layout, models, flags = environment
    service = RagService(layout, models, "chat", "one")
    kb = make_library(service)
    service.ingest(kb, "苹果资料很长很长", "document.md")
    flags["fail_rerank"] = True
    result = service.search("苹果", {"rerank": "rank", "max_result_chars": 4})
    assert result["warnings"]
    assert result["truncated"]
    assert sum(len(item["text"]) for item in result["results"]) <= 4
    with pytest.raises(ValueError, match="重排失败"):
        service.search("苹果", {"rerank": "rank", "rerank_failure_policy": "error"})


def test_session_library_archive_restore(environment):
    layout, models, _ = environment
    service = RagService(layout, models, "chat", "one")
    kb = make_library(service)
    service.ingest(kb, "苹果资料", "document.md")
    maintenance = StorageMaintenanceService(layout)
    archived = maintenance.archive_session("chat", "one")
    assert service.list() == []
    maintenance.restore_archive("chat", archived["archive_id"])
    assert service.search("苹果", {})["status"] == "found"


def test_fork_copies_private_indexes_and_remaps_only_private_references(environment):
    """分支副本独立写入, 全局引用仍共享, 其他配置域原样继承"""
    layout, models, _ = environment
    source = RagService(layout, models, "chat", "one")
    local = make_library(source, duplicate_policy="replace")
    shared = make_library(source, "共享", "global")
    source.ingest(local, "苹果原资料", "document.md")
    store = SessionOverrideStore(layout.platform_db("chat"))
    store.replace("one", "plugins.rag", {"session_db_ids": [local], "global_db_ids": [shared], "write_db_id": local}, expected_revision=0)
    store.replace("one", "example", {"enabled": False, "optional": None, "items": []}, expected_revision=0)
    mapping = fork_session_settings(layout, "chat", "one", "two")
    assert mapping[local] != local
    assert store.read("two", "plugins.rag")["overrides"] == {"session_db_ids": [mapping[local]], "global_db_ids": [shared], "write_db_id": mapping[local]}
    assert store.read("two", "example")["overrides"] == store.read("one", "example")["overrides"]
    target = RagService(layout, models, "chat", "two")
    assert target.search("苹果", {})["status"] == "found"
    target.ingest(mapping[local], "香蕉分支资料", "document.md")
    assert source.documents(local, include_text=True)[0]["source_text"] == "苹果原资料"
    assert target.documents(mapping[local], include_text=True)[0]["source_text"] == "香蕉分支资料"


def test_fork_empty_library_and_parameters_do_not_create_directories(environment):
    layout, models, _ = environment
    source = RagService(layout, models, "chat", "one")
    make_library(source)
    store = SessionOverrideStore(layout.platform_db("chat"))
    store.replace("one", "example", {"budget": 0}, expected_revision=0)
    fork_session_settings(layout, "chat", "one", "two")
    assert not layout.session_root("chat", "one").exists()
    assert not layout.session_root("chat", "two").exists()
    assert store.read("two", "example")["overrides"] == {"budget": 0}


def test_fork_failure_does_not_publish_partial_records(environment, monkeypatch):
    layout, models, _ = environment
    service = RagService(layout, models, "chat", "one")
    kb = make_library(service)
    service.ingest(kb, "苹果资料", "document.md")
    with sqlite3.connect(layout.platform_db("chat")) as connection:
        connection.execute("UPDATE rag_knowledge_bases SET generation='missing' WHERE id=?", (kb,))
    with pytest.raises(ValueError, match="索引文件缺失"):
        fork_session_settings(layout, "chat", "one", "two")
    assert RagService(layout, models, "chat", "two").list() == []


def test_plugin_save_validates_effective_budgets_and_session_references(environment, tmp_path):
    layout, models, _ = environment
    meta = yaml.safe_load(Path("satrap/expend/plugins/rag/meta.yaml").read_text(encoding="utf-8"))
    assert isinstance(meta, dict)
    schema = parse_config_schema(meta)
    service = RagService(layout, models, "chat", "one")
    foreign = make_library(RagService(layout, models, "chat", "two"))
    settings = PluginSettingsService(layout.platform_db("chat"), PluginConfigManager(tmp_path / "config"), models=models, rag=service)
    for values in ({"candidate_k": 1}, {"session_db_ids": [foreign]}, {"rerank": "missing"}, {"write_db_id": "missing"}):
        with pytest.raises(ValueError):
            settings.save("one", "rag", schema, values, expected_revision=0)
        assert settings.get("one", "rag", schema)["revision"] == 0
    assert settings.save("one", "rag", schema, {"top_k": 1, "candidate_k": 1, "similarity_threshold": None}, expected_revision=0)["config"]["similarity_threshold"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_installed_rag_tools_use_current_session_and_do_not_generate_answers(environment, tmp_path, monkeypatch, asynchronous):
    layout, models, _ = environment
    monkeypatch.setattr("satrap.edictum.simple_session.PluginConfigManager", lambda: PluginConfigManager(tmp_path / "config"))
    service = RagService(layout, models, "chat", "one")
    kb = make_library(service)
    session_type = AsyncSimpleSession if asynchronous else SimpleSession
    model = Mock(spec=AsyncLLM if asynchronous else LLM)
    session = session_type("one", model, db_path=str(layout.platform_db("chat")))
    session.storage_layout, session.storage_platform_id, session.plugin_model_manager = layout, "chat", models
    session.plugin_override_store = SessionOverrideStore(layout.platform_db("chat"))
    session.plugin_override_store.replace("one", "plugins.rag", {"max_result_chars": 3}, expected_revision=0)
    if isinstance(session, AsyncSimpleSession):
        await session.initialize()
        plugin = await session.install_plugin("satrap/expend/plugins/rag")
    else:
        plugin = session.install_plugin("satrap/expend/plugins/rag")
    assert session._wf is not None
    tools_manager = session._wf.tools_manager
    async def invoke(name: str, **values: Any) -> dict[str, Any]:
        tool = tools_manager.tools[name]
        return await tool.execute(**values) if isinstance(tool, AsyncTool) else tool.execute(**values)
    assert {name for name in tools_manager.tools if name.startswith("rag_")} == {"rag_search", "rag_list", "rag_ingest"}
    assert "llm" not in plugin.config_schema
    assert (await invoke("rag_ingest", text="苹果知识资料", source="notes.md"))["status"] == "indexed"
    found = await invoke("rag_search", query="苹果")
    assert found["results"][0]["text"] == "苹果知"
    assert found["results"][0]["sources"][0]["kb_id"] == kb
    assert (await invoke("rag_search", query="苹果", kb_id="foreign"))["status"] == "error"
    model.call.assert_not_called()
    if isinstance(session, AsyncSimpleSession):
        await session.uninstall_plugin("rag")
    else:
        session.uninstall_plugin("rag")
    assert not any(name.startswith("rag_") for name in tools_manager.tools)
