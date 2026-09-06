"""RAG 管理接口适配, 控制服务与 Chat 共用"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager
import tempfile
from pathlib import Path
import sqlite3
import base64
from typing import Any

from satrap.edictum.plugin_settings import model_options
from satrap.core.utils.documents import DEFAULT_MAX_FILE_SIZE, SUPPORTED_EXTENSIONS, document_capabilities, extract_document
from satrap.core.rag import RagService, validate_rag_config

from satrap.core.log import logger


class RagOperationError(ValueError):
    """包含失败阶段的 RAG 操作异常"""

    def __init__(self, stage: str, cause: Exception) -> None:
        """
        保留可显示的失败原因和 HTTP 状态

        参数:
        - stage: 文档处理阶段
        - cause: 原始异常
        """
        self.stage = stage
        self.status = 400 if isinstance(cause, (ValueError, TypeError)) else 500
        super().__init__(f"{stage}失败: {type(cause).__name__}: {str(cause).strip() or '未提供错误描述'}")


@contextmanager
def rag_operation(stage: str) -> Iterator[None]:
    """
    将解析和构建异常转换为管理接口可显示的错误

    参数:
    - stage: 当前处理阶段

    返回:
    - 操作上下文, 失败时保留异常链并抛出 RagOperationError
    """
    try:
        yield
    except RagOperationError:
        raise
    except Exception as error:
        logger.error(f"[RAG] {stage}失败: {type(error).__name__}: {error}")
        raise RagOperationError(stage, error) from error


def require_stored_session(database: Path, session_id: str) -> None:
    """以数据库身份为准验证会话, 不遍历文件夹也不创建不存在的会话"""
    if not session_id or not database.is_file():
        raise ValueError("会话不存在")
    with closing(sqlite3.connect(str(database))) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column in (("session_configs", "session_id"), ("conversation_meta", "conversation_id")):
            if table in tables and connection.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (session_id,)).fetchone():
                return
    raise ValueError("会话不存在")


def rag_admin_request(service: RagService, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """执行已鉴权管理请求, 文件上传不接受客户端指定的磁盘路径"""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    if method == "GET":
        kb_id = str(payload.get("kb_id") or "")
        return {"ok": True, "knowledge_bases": service.list(), "documents": service.documents(kb_id) if kb_id else [], "model_options": model_options(service.models), "upload": document_capabilities()}
    action = payload.get("action")
    if not isinstance(action, str):
        raise ValueError("action 必须是字符串")
    kb_id = str(payload.get("kb_id") or "")
    name = payload.get("name")
    scope = payload.get("scope")
    revision = payload.get("expected_revision")
    if action in {"create", "update"} and not isinstance(name, str):
        raise ValueError("知识库名称必须是字符串")
    if action == "create" and not isinstance(scope, str):
        raise ValueError("知识库范围必须是字符串")
    if action in {"update", "rebuild"}:
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("expected_revision 必须是正整数")
    if action == "create":
        assert isinstance(name, str) and isinstance(scope, str)
        return {"ok": True, "knowledge_base": service.create(name, scope, payload.get("config", {}))}
    if action == "update":
        assert isinstance(name, str) and isinstance(revision, int)
        return {"ok": True, "knowledge_base": service.update(kb_id, name, payload.get("config", {}), revision, payload.get("is_default", False))}
    if action == "rebuild":
        assert isinstance(revision, int)
        with rag_operation("重建索引"):
            return {"ok": True, "knowledge_base": service.rebuild(kb_id, payload.get("config", {}), revision)}
    if action == "delete":
        service.delete(kb_id)
        return {"ok": True}
    if action == "delete_document":
        return {"ok": service.delete_document(kb_id, str(payload.get("source_id") or ""))}
    if action == "search":
        config = payload.get("config", {})
        config = validate_rag_config(config)
        if not service.session_id and config.get("db_scope") in {"session", "session_global"}:
            ids = config.get("session_db_ids", [])
            owners = {service._get(kb_id)[1]["session_id"] for kb_id in ids}
            if len(owners) != 1 or "" in owners:
                raise ValueError("管理检索必须选择同一会话的知识库")
            service = RagService(service.layout, service.models, service.platform_id, owners.pop())
        return {"ok": True, **service.search(payload.get("query", ""), payload.get("config", {}), payload.get("kb_ids"))}
    if action == "ingest":
        text = payload.get("text", "")
        source = payload.get("source", "")
        encoded = payload.get("file_data")
        if encoded is not None:
            if text or not isinstance(encoded, str) or len(encoded) > 45_000_000:
                raise ValueError("上传内容无效或文件超过 32 MiB")
            content = base64.b64decode(encoded, validate=True)
            if len(content) > 32 * 1024 * 1024:
                raise ValueError("文件超过 32 MiB")
            filename = str(payload.get("file_name") or "document.txt")
            return rag_upload_document(service, kb_id, filename, content, source)
        with rag_operation("向量化或构建索引"):
            return {"ok": True, **service.ingest(kb_id, text, source)}
    raise ValueError("未知 RAG 管理操作")


def rag_upload_document(service: RagService, kb_id: str, filename: str, content: bytes, source: str = "") -> dict[str, Any]:
    """
    解析二进制上传并导入明确的知识库, 临时文件在所有退出路径清理

    参数:
    - service: 已限定平台及会话的 RAG 服务
    - kb_id: 目标知识库 ID
    - filename: 客户端文件名, 只用于识别格式和默认来源
    - content: 原始文件内容, 最大 32 MiB
    - source: 可选来源名称, 默认使用文件名

    返回:
    - 导入或跳过结果, 包含分块数和来源名称
    """
    service._get(kb_id)
    if not filename or len(filename) > 1000 or Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError("文件名无效或文件类型不支持")
    if not content:
        raise ValueError("不能上传空文件")
    if len(content) > DEFAULT_MAX_FILE_SIZE:
        raise ValueError("文件超过 32 MiB")
    source = source or Path(filename.replace("\\", "/")).name
    if not source.strip() or len(source) > 1000:
        raise ValueError("文档来源名称必须为 1 到 1000 个字符")
    root = service.layout.root / "rag" / "imports"
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, suffix=Path(filename).suffix, delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
        with rag_operation("解析文档"):
            text = extract_document(temporary)
        with rag_operation("向量化或构建索引"):
            return {"ok": True, "source": source, **service.ingest(kb_id, text, source)}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
