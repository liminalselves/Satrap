"""业务链路场景与集中边界契约"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import threading
from pathlib import Path
import sqlite3
import pytest
from typing import Any, cast
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
from typing import Literal
from satrap.expend.tools.rag import LiteVectorRAG, _filter_embedding_results
from importlib.machinery import ModuleSpec
from urllib.parse import urlencode
import asyncio
import json
from satrap.core.framework.SessionManager import SessionConfigStore
from satrap.core.config.rag_service import rag_upload_document
from satrap.core.utils.documents import DEFAULT_MAX_FILE_SIZE, SUPPORTED_EXTENSIONS, extract_document, extract_text
from satrap.core.utils.minihttp import MiniHTTPServer
from satrap.core.server_auth import ServerAuth
from satrap.display.service import ChatService
from satrap.display.server import ChatHTTPServer
from satrap.core.backend import control_server
from satrap.core.utils import documents
from satrap.core.type import SessionConfig
import time
from satrap.core.utils.text_utils import TextSplitter

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

def test_plugin_save_validates_effective_budgets_and_session_references(
    environment: tuple[StorageLayout, ModelConfigManager, dict[str, bool]], tmp_path,
):
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
async def test_installed_rag_tools_use_current_session_and_do_not_generate_answers(
        environment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asynchronous: bool) -> None:
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
    denied = await invoke("rag_search", query="苹果", kb_id="foreign")
    assert denied["ok"] is False and denied["error_type"] == "invalid_arguments" and denied["tool_name"] == "rag_search"

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("内部路径 D:/secret")

    from satrap.expend.plugins.rag.tools import AsyncRagTool, RagTool
    search_tool = cast(AsyncRagTool | RagTool, tools_manager.tools["rag_search"])
    search_service: RagService = search_tool.service
    monkeypatch.setattr(search_service, "search", broken)
    hidden = await invoke("rag_search", query="苹果")
    assert hidden == {"ok": False, "error": "知识库操作暂不可用, 请查看后端日志", "error_type": "execution_error", "tool_name": "rag_search"}
    assert "D:/secret" not in str(hidden)
    model.call.assert_not_called()
    if isinstance(session, AsyncSimpleSession):
        await session.uninstall_plugin("rag")
    else:
        session.uninstall_plugin("rag")
    assert not any(name.startswith("rag_") for name in tools_manager.tools)

def test_rag_factory_tools_carry_entry_binding(environment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """直接工厂构造的 RAG 工具也带入口绑定, 与官方安装路径一致"""
    from satrap.expend.plugins.rag.tools import get_tools

    layout, models, _ = environment
    monkeypatch.setattr("satrap.edictum.simple_session.PluginConfigManager", lambda: PluginConfigManager(tmp_path / "config"))
    session = SimpleSession("one", Mock(spec=LLM), db_path=str(layout.platform_db("chat")))
    session.storage_layout, session.storage_platform_id, session.plugin_model_manager = layout, "chat", models
    tools = get_tools(session, {})
    assert {tool.get_tool_name() for tool in tools} == {"rag_search", "rag_list", "rag_ingest"}
    ingest = next(tool for tool in tools if tool.get_tool_name() == "rag_ingest")
    parameters = ingest.get_tool_defined()["function"]["parameters"]
    assert parameters["required"] == []
    assert parameters["oneOf"] == [
        {"required": ["text"], "not": {"required": ["path"]}},
        {"required": ["path"], "not": {"required": ["text"]}},
    ]
    for tool in tools:
        # 绑定由 bind_plugin_tool 动态写入, 类型上不可见
        binding = cast(Any, tool)._plugin_entry_binding
        assert binding.plugin_name == "rag" and binding.name == tool.get_tool_name()

class _FakeEmbedding:
    """返回预设批量向量的 Embedding 替身"""

    def __init__(self, result: list[list[float]] | Literal[False]):
        self.result: list[list[float]] | Literal[False] = result
        self.inputs: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]] | Literal[False]:
        """记录当前输入并返回预设结果"""
        self.inputs.append(texts)
        return self.result

class _FakeVectorDB:
    """记录批量写入内容的向量库替身"""

    def __init__(self):
        self.calls: list[tuple[str, list[str], list[list[float]], list[dict[str, Any]]]] = []

    def add_to_collection(
        self,
        name: str,
        documents: list[str],
        vectors: list[list[float]],
        metadata: list[dict[str, Any]],
    ) -> int:
        """记录一次批量写入"""
        self.calls.append((name, documents, vectors, metadata))
        return len(documents)

def _rag_with_fakes(
    embedding_result: list[list[float]] | Literal[False],
) -> tuple[LiteVectorRAG, _FakeEmbedding, _FakeVectorDB]:
    """构造不访问网络和磁盘的 RAG 实例"""
    embedding = _FakeEmbedding(embedding_result)
    vector_db = _FakeVectorDB()
    rag = object.__new__(LiteVectorRAG)
    rag.default_vectorstore_name = "test"
    rag.chunk_size = 100
    rag.chunk_overlap = 0
    rag.batch_size = 3
    rag.k_default = 4
    rag.threshold = 0.5
    rag.embeddings = cast(Any, embedding)
    rag.vector_db = cast(Any, vector_db)
    return rag, embedding, vector_db

@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["add_documents", "add_text_file", "swift"])
async def test_empty_vectors_are_filtered_before_writing(tmp_path: Path, path: str):
    """三条写入路径都只写入向量化成功的文本块, 空向量与失败文本被丢弃"""
    rag, embedding, vector_db = _rag_with_fakes([[1.0, 0.0], [], [0.0, 1.0]])
    if path == "add_documents":
        assert await rag.add_documents(["a", "b", "c"]) is True
        assert embedding.inputs == [["a", "b", "c"]]
    elif path == "add_text_file":
        file_path = tmp_path / "input.txt"
        file_path.write_text("a\nb\nc", encoding="utf-8")
        assert await rag.add_text_file(str(file_path), chunk_size=1, chunk_overlap=0, batch_size=3) is True
        assert embedding.inputs == [["a", "\nb", "\nc"]]
    else:
        assert await rag.swift(add_documents=["a", "b", "c"]) == ([], [])
        assert embedding.inputs == [["a", "b", "c"]]
    assert vector_db.calls[0][1:3] == (["a", "\nc"] if path == "add_text_file" else ["a", "c"],
                                       [[1.0, 0.0], [0.0, 1.0]])

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["mismatch", "all_empty"])
async def test_embedding_batch_rejects_mismatch_or_all_empty(kind: str):
    """返回数量不一致直接报错, 全部向量化失败则返回 False 且不写入"""
    if kind == "mismatch":
        with pytest.raises(ValueError, match="返回数量与输入文本数量不一致"):
            _filter_embedding_results(["a", "b"], [[1.0]])
    else:
        rag, embedding, vector_db = _rag_with_fakes([[], [], []])
        assert await rag.add_documents(["a", "b", "c"]) is False
        assert embedding.inputs == [["a", "b", "c"]]
        assert vector_db.calls == []

@pytest.mark.parametrize("extension", [ext for ext in SUPPORTED_EXTENSIONS if ext not in {".pdf", ".docx", ".xlsx"}])
def test_utf8_formats_and_bom(tmp_path: Path, extension: str) -> None:
    path = tmp_path / ("document" + extension)
    path.write_bytes(b"\xef\xbb\xbf" + "中文文档\nsecond line".encode("utf-8"))
    assert extract_document(path) == "中文文档\nsecond line"

@pytest.mark.parametrize("content", [b"\xff", b"", b"   ", b"hello\x00world"])
def test_invalid_text_fails_explicitly(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "document.txt"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        extract_document(path)

def test_strict_limit_and_legacy_truncation(tmp_path: Path) -> None:
    path = tmp_path / "document.md"
    path.write_text("12345", encoding="utf-8")
    assert extract_document(path, max_length=5) == "12345"
    assert extract_text(path, max_length=4) == "1234"
    with pytest.raises(ValueError, match="拆分"):
        extract_document(path, max_length=4)

@pytest.mark.parametrize("kind", ["docx", "xlsx"])
def test_office_parsing_preserves_chinese_and_tables(tmp_path: Path, kind: str) -> None:
    path = tmp_path / ("document." + kind)
    if kind == "docx":
        module = pytest.importorskip("docx")
        document = module.Document()
        document.add_paragraph("中文段落")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "项目"
        table.cell(0, 1).text = "中文内容"
        document.save(path)
    else:
        module = pytest.importorskip("openpyxl")
        workbook = module.Workbook()
        workbook.active.append(["项目", "中文内容"])
        workbook.create_sheet("第二页").append(["中文段落", 123])
        workbook.save(path)
        workbook.close()
    text = extract_document(path)
    assert "中文段落" in text and "中文内容" in text and "项目" in text

@pytest.mark.parametrize("kind", ["docx", "xlsx"])
def test_empty_office_documents_have_no_artificial_text(tmp_path: Path, kind: str) -> None:
    path = tmp_path / ("empty." + kind)
    if kind == "docx":
        module = pytest.importorskip("docx")
        document = module.Document()
        document.add_table(rows=1, cols=1)
        document.save(path)
    else:
        module = pytest.importorskip("openpyxl")
        workbook = module.Workbook()
        workbook.save(path)
        workbook.close()
    with pytest.raises(ValueError, match="没有可提取文字"):
        extract_document(path)

def make_pdf(path: Path, text: str) -> None:
    stream = ("BT /F1 12 Tf 72 720 Td (" + text + ") Tj ET").encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, content in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(str(number).encode() + b" 0 obj\n" + content + b"\nendobj\n")
    start = len(data)
    data.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF\n")
    path.write_bytes(data)

def test_pdf_text_and_empty_page(tmp_path: Path) -> None:
    pytest.importorskip("pdfplumber")
    path = tmp_path / "document.pdf"
    make_pdf(path, "upload document")
    assert "upload document" in extract_document(path)
    make_pdf(path, "")
    with pytest.raises(ValueError, match="OCR"):
        extract_document(path)

def test_missing_parser_is_reported_in_capabilities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = documents.find_spec
    def find_parser(name: str) -> ModuleSpec | None:
        return None if name == "pdfplumber" else original(name)
    monkeypatch.setattr(documents, "find_spec", find_parser)
    assert cast(dict[str, str], documents.document_capabilities()["missing_parsers"])[".pdf"] == "pdfplumber"
    with pytest.raises(ValueError, match="缺少解析依赖"):
        extract_document(tmp_path / "document.pdf")

@pytest.fixture
def rag_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[StorageLayout, ModelConfigManager, RagService, str]:
    layout = StorageLayout(tmp_path / "data")
    models = ModelConfigManager(tmp_path / "models.json", auto_create=False)
    models.set_embedding_config(EmbeddingConfig(model="offline", api_key="test", dimensions=3), "embed")
    def embed(text: str | list[str]) -> list[float] | list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in text] if isinstance(text, list) else [1.0, 0.0, 0.0]
    client = Mock()
    client.embed.side_effect = embed
    monkeypatch.setattr("satrap.core.rag.build_model_client", Mock(return_value=client))
    service = RagService(layout, models, "chat")
    kb = service.create("文档", "global", {"embed": "embed", "chunk_size": 100000, "chunk_overlap": 0})
    return layout, models, service, kb["id"]

def test_upload_cleans_temp_files_on_success_and_failure(rag_environment: tuple[StorageLayout, ModelConfigManager, RagService, str]) -> None:
    layout, _, service, kb_id = rag_environment
    assert rag_upload_document(service, kb_id, "a.md", "中文".encode())["status"] == "indexed"
    assert rag_upload_document(service, kb_id, "a.md", "中文".encode())["status"] == "skipped"
    with pytest.raises(ValueError):
        rag_upload_document(service, kb_id, "bad.txt", b"\xff")
    assert not list((layout.root / "rag" / "imports").iterdir())
    assert len(service.documents(kb_id)) == 1

@pytest.mark.parametrize("via", ["control", "chat"])
async def test_real_http_upload_limits_scope_and_search(
    rag_environment: tuple[StorageLayout, ModelConfigManager, RagService, str],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via: str,
) -> None:
    layout, models, service, kb_id = rag_environment
    token = "test-upload-token-longer-than-32-characters"
    origin = "http://127.0.0.1:5173"
    auth = ServerAuth.create("127.0.0.1", 0, token=token, allowed_origins=frozenset({origin}))
    chat: ChatHTTPServer | None = None
    if via == "control":
        monkeypatch.setattr(control_server, "_CONTROL_AUTH", auth)
        monkeypatch.setattr(control_server, "_configured_platform_ids", lambda: list[str]())
        monkeypatch.setattr(control_server, "_configured_storage_layout", lambda: layout)
        monkeypatch.setattr(control_server, "_model_config_service", lambda: Mock(manager=models))
        server = await asyncio.start_server(control_server._handle_request, "127.0.0.1", 0)
        route = "/config/rag"
    else:
        svc = Mock(spec=ChatService)
        svc._storage, svc._model_cfg, svc._platform_id, svc._conversations = layout, models, "chat", {}
        chat = ChatHTTPServer.__new__(ChatHTTPServer)
        MiniHTTPServer.__init__(chat, host="127.0.0.1", port=0, auth=auth)
        chat.service = svc
        await chat.start()
        assert chat._server is not None
        server = chat._server
        route = "/api/chat/rag"
    port = server.sockets[0].getsockname()[1]

    async def request(path: str, body: bytes, *, length: int | None = None, authorized: bool = True) -> tuple[int, dict[str, Any]]:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        header = ("POST " + path + " HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/octet-stream\r\n"
                  + "Origin: " + origin + "\r\n"
                  + ("Authorization: Bearer " + token + "\r\n" if authorized else "")
                  + "Content-Length: " + str(len(body) if length is None else length) + "\r\nConnection: close\r\n\r\n")
        writer.write(header.encode() + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 15)
        writer.close()
        await writer.wait_closed()
        head, payload = response.split(b"\r\n\r\n", 1)
        assert ("access-control-allow-origin: " + origin).encode() in head.lower()
        return int(head.split(b" ")[1]), cast(dict[str, Any], json.loads(payload))

    upload = route + "/upload?" + urlencode({"platform_id": "chat", "kb_id": kb_id, "file_name": "真实中文.md"})
    try:
        store = SessionConfigStore(layout.platform_db("chat"))
        for session_id in ("one", "two"):
            store.upsert(SessionConfig(session_id=session_id))
        private = RagService(layout, models, "chat", "one").create("私有库", "session", {"embed": "embed"})
        foreign = route + "/upload?" + urlencode({"platform_id": "chat", "session_id": "two", "kb_id": private["id"], "file_name": "foreign.txt"})
        assert (await request(foreign, b"private data"))[0] == 400
        content = ("中文资料" * 100000).encode("utf-8")
        assert len(content) > 1024 * 1024
        status, result = await request(upload, content)
        assert status == 200 and result["status"] == "indexed"
        assert service.search("中文", {"db_scope": "global", "global_db_ids": [kb_id]})["status"] == "found"
        assert (await request(upload, content))[1]["status"] == "skipped"
        assert (await request(upload, b"", length=DEFAULT_MAX_FILE_SIZE + 1))[0] == 413
        assert (await request(upload, b"bad", authorized=False))[0] == 401
        assert (await request(upload, b"\xff"))[0] == 400
        assert (await request(upload.replace(kb_id, "missing"), b"data"))[0] == 400
        ordinary_limit = 1024 * 1024 if via == "control" else 16 * 1024 * 1024
        assert (await request(route, b"", length=ordinary_limit + 1))[0] == 413
        status, result = await request(upload, b"x" * DEFAULT_MAX_FILE_SIZE)
        assert status == 400 and "拆分" in result["error"]
        assert result["stage"] == "解析文档"
        with monkeypatch.context() as failure:
            client = Mock()
            client.embed.side_effect = RuntimeError("Embedding 服务返回 429: quota exceeded")
            failure.setattr("satrap.core.rag.build_model_client", Mock(return_value=client))
            status, result = await request(upload + "&source=new-document", b"new content")
            assert status == 500 and result["stage"] == "向量化或构建索引"
            assert "429: quota exceeded" in result["error"]
            kb = service.list()[0]
            rebuild: dict[str, object] = {"action": "rebuild", "kb_id": kb_id, "config": kb["config"], "expected_revision": kb["revision"]}
            status, result = await request(route + "?platform_id=chat", json.dumps(rebuild).encode())
            assert status == 500 and result["stage"] == "重建索引"
            assert "429: quota exceeded" in result["error"]
        assert len(service.documents(kb_id)) == 1
        assert service.search("中文", {"db_scope": "global", "global_db_ids": [kb_id]})["status"] == "found"
        assert not list((layout.root / "rag" / "imports").iterdir())
        if chat is not None:
            async def fail_route(*args: object) -> tuple[int, dict[str, object]]:
                raise RuntimeError("unexpected")
            monkeypatch.setattr(chat, "_route", fail_route)
            assert (await request(route, b"{}")) == (500, {"error": "internal server error"})
    finally:
        if chat is not None:
            await chat.stop()
        else:
            server.close()
            await server.wait_closed()

NO_SEPARATOR_400K = "中文资料" * 100_000

RAG_CHUNK_SIZE = 100_000

RAG_CHUNK_OVERLAP = 0

SPLIT_SECONDS_BUDGET = 3.0

LARGE_CORPUS_SECONDS_BUDGET = 8.0

@pytest.mark.parametrize("chunk_size, chunk_overlap, keep_separator, text, expected", [
    (4, 0, True, "abcdefghij", ["abcd", "efgh", "ij"]),
    (5, 1, True, "abcdefghijklmno", ["abcde", "efghi", "ijklm", "mno"]),
    (12, 3, True, "第一段内容\n\n第二段内容\n\n第三段内容", ["第一段内容", "\n\n第二段内容", "\n\n第三段内容"]),
    (12, 3, False, "第一段内容\n\n第二段内容\n\n第三段内容", ["第一段内容\n\n第二段内容", "第三段内容"]),
])
def test_text_split_reference_cases(chunk_size, chunk_overlap, keep_separator, text, expected):
    """超长单块/重叠窗口/双换行分段的切分基线"""
    assert TextSplitter(chunk_size, chunk_overlap, keep_separator=keep_separator).split_text(text) == expected

def test_documents_are_split_in_order():
    splitter = TextSplitter(5, 0)
    assert splitter.split_documents(["abcdefg", "1234567"]) == ["abcde", "fg", "12345", "67"]

@pytest.mark.parametrize("chunk_size, chunk_overlap, expected_chunks", [
    (RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP, 4),
    (1000, 200, 500),
])
def test_long_text_split_stays_within_budget(chunk_size, chunk_overlap, expected_chunks):
    """40 万字符无分隔文本在预算内切分, 重叠语义不改变原文还原结果"""
    TextSplitter(chunk_size, chunk_overlap).split_text("预热")   # 预热排除首次导入抖动
    started = time.perf_counter()
    chunks = TextSplitter(chunk_size, chunk_overlap).split_text(NO_SEPARATOR_400K)
    spent = time.perf_counter() - started
    assert spent < SPLIT_SECONDS_BUDGET, f"40 万字符切分耗时 {spent:.2f} 秒, 超过 {SPLIT_SECONDS_BUDGET} 秒预算"
    assert len(chunks) == expected_chunks
    assert all(len(chunk) <= chunk_size for chunk in chunks)
    if chunk_overlap:
        assert all(chunk[:chunk_overlap] == chunks[index - 1][-chunk_overlap:] for index, chunk in enumerate(chunks) if index)
        assert chunks[0] + "".join(chunk[chunk_overlap:] for chunk in chunks[1:]) == NO_SEPARATOR_400K
    else:
        assert "".join(chunks) == NO_SEPARATOR_400K

def test_chunking_cost_is_bounded_by_document_times_window():
    """预算同时约束文档长度与合并窗口的乘积

    旧实现每个分块边界都把整个窗口逐个从队首弹出, 单次成本为 O(窗口²), 总成本为
    O(文档长度 × 窗口大小): 固定窗口时仍随长度线性增长, 但常数因子等于窗口大小。
    因此这里同时放大文档长度 (120 万字符) 并沿用知识库的大窗口, 实测 deque 实现 0.4 秒,
    改回 list.pop(0) 后约 45 秒。
    """
    corpus = "资" * 1_200_000
    started = time.perf_counter()
    chunks = TextSplitter(RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP).split_text(corpus)
    spent = time.perf_counter() - started
    assert spent < LARGE_CORPUS_SECONDS_BUDGET, f"120 万字符切分耗时 {spent:.2f} 秒, 超过 {LARGE_CORPUS_SECONDS_BUDGET} 秒预算"
    assert len(chunks) == 12
    assert "".join(chunks) == corpus
