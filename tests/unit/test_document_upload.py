"""通用文档解析和真实上传接口验证"""
from __future__ import annotations

from importlib.machinery import ModuleSpec
from unittest.mock import Mock
from urllib.parse import urlencode
from pathlib import Path
import asyncio
import pytest
from typing import Any, cast
import json

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.framework.SessionManager import SessionConfigStore
from satrap.core.config.rag_service import rag_upload_document
from satrap.core.utils.documents import DEFAULT_MAX_FILE_SIZE, SUPPORTED_EXTENSIONS, extract_document, extract_text
from satrap.core.utils.minihttp import MiniHTTPServer
from satrap.core.server_auth import ServerAuth
from satrap.display.service import ChatService
from satrap.display.server import ChatHTTPServer
from satrap.core.backend import control_server
from satrap.core.storage import StorageLayout
from satrap.core.utils import documents
from satrap.core.type import EmbeddingConfig, SessionConfig
from satrap.core.rag import RagService


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
