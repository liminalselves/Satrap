"""分层 RAG 知识库服务, 供管理接口及模型工具共用"""
from __future__ import annotations

from contextlib import closing, nullcontext, contextmanager, ExitStack
from collections.abc import Iterator
import hashlib
from pathlib import Path
import sqlite3
import shutil
from typing import Any, Callable, Generator, cast
import json
import math
import time
import uuid

from satrap.edictum.plugin_resources import build_model_client, named_model_config
from satrap.core.storage.file_lock import FileLease, FileLock, session_storage_lock
from satrap.core.utils.text_utils import TextSplitter
from satrap.core.type import EmbeddingConfig
from satrap.core.database import DataBase
from satrap.core.storage import StorageLayout

from satrap.core.log import logger


DEFAULT_RAG_CONFIG: dict[str, Any] = {
    "db_scope": "session", "global_db_ids": [], "session_db_ids": [], "write_db_id": "", "rerank": "",
    "candidate_k": 20, "top_k": 5, "similarity_threshold": None, "rerank_min_score": None,
    "rerank_failure_policy": "fallback", "max_result_chars": 12000,
}


RagProgress = Callable[[str, str, int, int | None], None]


def ensure_rag_tables(connection: sqlite3.Connection) -> None:
    """知识库身份与配置存于所属数据库, 会话库可随会话归档"""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS rag_knowledge_bases ("
        "id TEXT PRIMARY KEY, name TEXT NOT NULL, scope TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '', "
        "config_json TEXT NOT NULL, embed_identity TEXT NOT NULL, generation TEXT NOT NULL DEFAULT '', "
        "revision INTEGER NOT NULL DEFAULT 1, is_default INTEGER NOT NULL DEFAULT 0, "
        "status TEXT NOT NULL DEFAULT 'ready', last_error TEXT NOT NULL DEFAULT '', "
        "document_count INTEGER NOT NULL DEFAULT 0, chunk_count INTEGER NOT NULL DEFAULT 0, "
        "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS rag_kb_owner ON rag_knowledge_bases(session_id, scope)")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS rag_kb_default ON rag_knowledge_bases(session_id) WHERE is_default=1 AND scope='session'")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS rag_jobs (job_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, fingerprint TEXT NOT NULL, "
        "owner_service TEXT NOT NULL, owner_runtime_id TEXT NOT NULL, platform_id TEXT NOT NULL, session_id TEXT NOT NULL, "
        "kb_id TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL, progress_json TEXT NOT NULL, "
        "created_at REAL NOT NULL, started_at REAL, finished_at REAL, error_json TEXT, result_json TEXT, "
        "request_json TEXT NOT NULL, input_name TEXT NOT NULL DEFAULT '', UNIQUE(kb_id, operation_id))"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS rag_jobs_owner ON rag_jobs(session_id, kb_id, created_at DESC, job_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS rag_jobs_runtime ON rag_jobs(owner_service, status)")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS rag_documents (document_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, source_id TEXT NOT NULL, "
        "session_id TEXT NOT NULL, name TEXT NOT NULL, size_bytes INTEGER, created_at REAL, updated_at REAL NOT NULL, "
        "last_indexed_at REAL, status TEXT NOT NULL, error_json TEXT, job_id TEXT, chunk_count INTEGER NOT NULL DEFAULT 0, "
        "UNIQUE(kb_id, source_id))"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS rag_documents_owner ON rag_documents(session_id, kb_id, document_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS rag_documents_library ON rag_documents(kb_id, document_id)")


def embedding_identity(config: EmbeddingConfig) -> str:
    """索引身份排除 API Key, 更换相同模型凭据不要求重新向量化"""
    return json.dumps({"model": config.model, "base_url": (config.base_url or "").rstrip("/"), "dimensions": config.dimensions}, sort_keys=True)


def validate_rag_config(values: dict[str, Any]) -> dict[str, Any]:
    """检索预算和访问范围由配置决定, 工具参数只能缩小范围"""
    if not isinstance(values, dict):
        raise ValueError("RAG 配置必须是对象")
    if set(values) - set(DEFAULT_RAG_CONFIG):
        raise ValueError("RAG 包含未知配置项")
    result: dict[str, Any] = {**DEFAULT_RAG_CONFIG, **values}
    for key in ("write_db_id", "rerank"):
        if not isinstance(result[key], str):
            raise ValueError(f"{key} 必须是字符串")
    if result["db_scope"] not in {"session", "global", "session_global"}:
        raise ValueError("db_scope 必须是 session/global/session_global")
    for key in ("global_db_ids", "session_db_ids"):
        if not isinstance(result[key], list) or any(not isinstance(item, str) or not item for item in result[key]):
            raise ValueError(f"{key} 必须是知识库 ID 数组")
        result[key] = list(dict.fromkeys(result[key]))
    for key, maximum in (("candidate_k", 1000), ("top_k", 100), ("max_result_chars", 100000)):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"{key} 必须是 1 到 {maximum} 的整数")
    if result["candidate_k"] < result["top_k"]:
        raise ValueError("candidate_k 不能小于 top_k")
    for key in ("similarity_threshold", "rerank_min_score"):
        value = result[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)):
            raise ValueError(f"{key} 必须是有限数字或 null")
    if result["rerank_failure_policy"] not in {"fallback", "error"}:
        raise ValueError("rerank_failure_policy 必须是 fallback/error")
    return result


class RagService:
    """管理全局库与当前平台会话库, 每个操作使用短生命周期连接"""

    def __init__(self, layout: StorageLayout, models: Any, platform_id: str, session_id: str = "") -> None:
        self.layout = layout
        self.models = models
        self.platform_id = platform_id
        self.session_id = session_id
        self.global_database = layout.root / "rag" / "global.db"
        self.session_database = layout.platform_db(platform_id)

    def _connect(self, database: Path) -> sqlite3.Connection:
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(database), timeout=10)
        connection.row_factory = sqlite3.Row
        ensure_rag_tables(connection)
        connection.commit()
        return connection

    @staticmethod
    def _payload(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["config"] = json.loads(result.pop("config_json"))
        result.pop("embed_identity", None)
        return result

    def list(self) -> list[dict[str, Any]]:
        """按管理上下文列出可见库, 不扫描索引目录或读取文档正文"""
        result: list[dict[str, Any]] = []
        for database in (self.global_database, self.session_database):
            if not database.exists():
                continue
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='rag_knowledge_bases'").fetchone():
                    continue
                where = "WHERE session_id=?" if database == self.session_database and self.session_id else ""
                rows = connection.execute(f"SELECT * FROM rag_knowledge_bases {where} ORDER BY created_at", (self.session_id,) if where else ()).fetchall()
                result.extend(self._payload(row) for row in rows)
        return result

    def _get(self, kb_id: str) -> tuple[Path, dict[str, Any]]:
        for database in (self.global_database, self.session_database):
            if not database.exists():
                continue
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='rag_knowledge_bases'").fetchone():
                    continue
                row = connection.execute("SELECT * FROM rag_knowledge_bases WHERE id=?", (kb_id,)).fetchone()
            if row is not None:
                if self.session_id and row["scope"] == "session" and row["session_id"] != self.session_id:
                    raise ValueError("不能访问其他会话的知识库")
                return database, dict(row)
        raise ValueError(f"知识库不存在: {kb_id}")

    def _root(self, row: dict[str, Any]) -> Path:
        if not row["id"].isalnum():
            raise ValueError("知识库 ID 无效")
        return (self.layout.root / "rag" / "indexes" if row["scope"] == "global" else self.layout.session_indexes(self.platform_id, row["session_id"]) / "rag") / row["id"]

    def _lock_path(self, row: dict[str, Any], suffix: str = "") -> Path:
        """
        返回会话移动范围之外的知识库锁路径

        参数:
        - row: 已验证的知识库记录
        - suffix: 可选 generation, 不接受外部路径

        返回:
        - 稳定的写入锁或版本读取锁路径
        """
        base = self.layout.root / "rag" / "locks" if row["scope"] == "global" else self.session_database.parent / "locks"
        return base / f"rag-{row['id']}{'-' + suffix if suffix else ''}.lock"

    @contextmanager
    def _library_lock(self, row: dict[str, Any]) -> Iterator[None]:
        """
        串行写入同库, 允许查询旧版本, 会话维护仍需等待租约释放

        参数:
        - row: 已验证的知识库记录

        返回:
        - 写入期间的维护租约和库锁
        """
        with ExitStack() as stack:
            if row["scope"] == "session":
                stack.enter_context(FileLease(session_storage_lock(self.layout, self.platform_id, row["session_id"]).path, shared=True))
            stack.enter_context(FileLock(self._lock_path(row)))
            yield

    @contextmanager
    def generation(self, kb_id: str) -> Iterator[dict[str, Any]]:
        """
        固定本次读取的已发布版本, 不等待后台向量化

        参数:
        - kb_id: 已限定范围的知识库 ID

        返回:
        - 持有读取租约的版本记录
        """
        with ExitStack() as stack:
            _, initial = self._get(kb_id)
            if initial["scope"] == "session":
                stack.enter_context(FileLease(session_storage_lock(self.layout, self.platform_id, initial["session_id"]).path, shared=True))
            for _ in range(5):
                _, row = self._get(kb_id)
                lease = FileLease(self._lock_path(row, row["generation"] or "empty"), shared=True)
                try:
                    _, current = self._get(kb_id)
                    if current["generation"] == row["generation"]:
                        yield row
                        return
                finally:
                    lease.close()
            raise ValueError("知识库版本正在连续变更, 请稍后重试")

    def _discard_inactive(self, row: dict[str, Any], keep: str) -> None:
        """持有库锁时清理失效版本, 删除失败留待下次写操作重试"""
        try:
            root = self._root(row)
            if not root.exists():
                return
            for path in root.iterdir():
                if path.name == keep or len(path.name) != 32 or any(char not in "0123456789abcdef" for char in path.name):
                    continue
                if path.is_dir() and not path.is_symlink():
                    try:
                        with FileLock(self._lock_path(row, path.name), timeout=0):
                            shutil.rmtree(path)
                    except TimeoutError:
                        continue   # 仍在读取的旧版本由后续写入回收
        except OSError:
            logger.warning(f"知识库 {row['id']} 的失效索引版本清理失败")

    @staticmethod
    def _validate_revision(value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("expected_revision 必须为正整数")

    @staticmethod
    def _validate_kb(values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("知识库配置必须是对象")
        allowed = {"embed", "chunk_size", "chunk_overlap", "batch_size", "duplicate_policy"}
        if set(values) - allowed:
            raise ValueError("知识库包含未知配置项")
        result: dict[str, Any] = {"chunk_size": 800, "chunk_overlap": 120, "batch_size": 32, "duplicate_policy": "skip", **values}
        if not isinstance(result.get("embed"), str) or not result["embed"]:
            raise ValueError("必须选择 Embedding 配置")
        for key in ("chunk_size", "chunk_overlap", "batch_size"):
            if isinstance(result[key], bool) or not isinstance(result[key], int):
                raise ValueError(f"{key} 必须是整数")
        if not 1 <= result["chunk_size"] <= 100000 or not 0 <= result["chunk_overlap"] < result["chunk_size"]:
            raise ValueError("分块大小或重叠大小无效")
        if not 1 <= result["batch_size"] <= 1000:
            raise ValueError("batch_size 必须在 1 到 1000 之间")
        if result["duplicate_policy"] not in {"skip", "replace"}:
            raise ValueError("duplicate_policy 必须是 skip/replace")
        return result

    def create(self, name: str, scope: str, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("知识库名称必须为 1 到 120 个字符")
        if scope not in {"global", "session"} or (scope == "session" and not self.session_id):
            raise ValueError("创建会话库需要明确的会话")
        config = self._validate_kb(config)
        model = named_model_config(self.models, "embed", config["embed"])
        database = self.global_database if scope == "global" else self.session_database
        kb_id = uuid.uuid4().hex
        now = time.time()
        with (session_storage_lock(self.layout, self.platform_id, self.session_id) if scope == "session" else nullcontext()), closing(self._connect(database)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            is_default = scope == "session" and not connection.execute("SELECT 1 FROM rag_knowledge_bases WHERE session_id=? AND is_default=1", (self.session_id,)).fetchone()
            connection.execute(
                "INSERT INTO rag_knowledge_bases (id,name,scope,session_id,config_json,embed_identity,is_default,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (kb_id, name.strip(), scope, self.session_id if scope == "session" else "", json.dumps(config), embedding_identity(model), int(is_default), now, now),
            )
        return next(item for item in self.list() if item["id"] == kb_id)

    def _index(self, row: dict[str, Any]) -> DataBase | None:
        generation = row["generation"]
        if not generation:
            return None
        if not generation.isalnum():
            raise ValueError("索引版本无效")
        path = self._root(row) / generation
        if not (path / "metadata.sqlite").is_file():
            raise ValueError("知识库索引文件缺失, 请恢复备份或重建")
        return DataBase(str(path))

    def documents(self, kb_id: str, *, include_text: bool = False) -> list[dict[str, Any]]:
        """
        读取发布版本的来源目录, 不等待后台重建

        参数:
        - kb_id: 已限定范围的知识库
        - include_text: 显式请求时才读取原文, 默认关闭

        返回:
        - 原同步管理接口的来源记录
        """
        with self.generation(kb_id) as row:
            return list(self._sources(row, include_text=include_text))

    def _sources(self, row: dict[str, Any], *, include_text: bool = True) -> Generator[dict[str, Any], None, None]:
        """
        逐份读取已发布来源, 目录查询不把正文带入 Python

        参数:
        - row: 已持有读取或写入租约的版本记录
        - include_text: 重建时才读取首块中保存的正文

        返回:
        - 每次仅解码一个来源的迭代器
        """
        if not row["generation"]:
            return
        path = self._root(row) / row["generation"] / "metadata.sqlite"
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            expression = "metadata" if include_text else "json_remove(metadata, '$.source_text')"
            for item in connection.execute(f"SELECT {expression} FROM documents WHERE collection_name='chunks' "
                                           "AND json_extract(metadata, '$.chunk_index')=0 ORDER BY id"):
                yield json.loads(item[0])

    def _stage(self, row: dict[str, Any], generation: str) -> DataBase:
        """
        通过 SQLite 在线备份复制已发布元数据, 派生向量缓存按需重建

        参数:
        - row: 当前发布记录
        - generation: 新版本 ID

        返回:
        - 尚未发布且仅由当前写入者修改的索引
        """
        target = self._root(row) / generation
        target.mkdir(parents=True, exist_ok=False)
        if row["generation"]:
            source = self._root(row) / row["generation"] / "metadata.sqlite"
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(target / "metadata.sqlite")) as output:
                original.backup(output)
        return DataBase(str(target))

    @staticmethod
    def _close(client: Any) -> None:
        close = getattr(getattr(client, "client", client), "close", None)
        if callable(close):
            close()

    def _embed_source(self, index: DataBase, config: dict[str, Any], source: dict[str, Any], model: EmbeddingConfig,
                      progress: RagProgress | None = None) -> int:
        """
        分批向量化一个来源后原子写入暂存索引

        参数:
        - index: 当前写入者持有的未发布索引
        - config: 已验证的分块和批量策略
        - source: 包含原文的一个来源
        - model: 与目标向量空间对应的配置
        - progress: 可选阶段回调, 限频由任务持有者负责

        返回:
        - 实际写入的片段数
        """
        if progress:
            progress("splitting", "chunks", 0, None)
        chunks = TextSplitter(config["chunk_size"], config["chunk_overlap"]).split_text(source["source_text"])
        if not chunks:
            raise ValueError("文档没有可索引的文本")
        vectors: list[list[float]] = []
        client = build_model_client("embed", model)
        try:
            if progress:
                progress("embedding", "chunks", 0, len(chunks))
            for start in range(0, len(chunks), config["batch_size"]):
                batch = chunks[start:start + config["batch_size"]]
                result = client.embed(batch)
                if not isinstance(result, list) or len(cast(list[object], result)) != len(batch) or any(not vector for vector in cast(list[object], result)):
                    raise ValueError("文档向量化未完整成功, 原数据保持不变")
                vectors.extend(cast(list[list[float]], result))
                if progress:
                    progress("embedding", "chunks", len(vectors), len(chunks))
        finally:
            self._close(client)
        metadata: list[dict[str, Any]] = [{**{key: value for key, value in source.items() if key != "source_text"}, "chunk_index": number, "chunk_count": len(chunks)} for number in range(len(chunks))]
        metadata[0]["source_text"] = source["source_text"]
        return index.replace_source("chunks", source["source_id"], chunks, vectors, metadata)

    def ingest(self, kb_id: str, text: str, source: str, *, expected_revision: int | None = None,
               progress: RagProgress | None = None) -> dict[str, Any]:
        """
        在独立 generation 导入来源并原子发布, 失败保留原数据

        参数:
        - kb_id: 目标知识库
        - text: 最多一百万字符的来源正文
        - source: 来源名称, 用于构造稳定身份
        - expected_revision: 后台任务固定的配置修订, 旧同步入口可省略
        - progress: 可选的阶段内进度回调

        返回:
        - indexed 或 skipped, 来源 ID 及片段数
        """
        if expected_revision is not None:
            self._validate_revision(expected_revision)
        if not isinstance(text, str) or not text.strip() or len(text) > 1000000:
            raise ValueError("文档正文必须为 1 到 1000000 个字符")
        if not isinstance(source, str) or not source.strip() or len(source) > 1000:
            raise ValueError("必须提供有效的文档来源名称")
        _, initial = self._get(kb_id)
        with self._library_lock(initial):
            database, row = self._get(kb_id)
            if expected_revision is not None and row["revision"] != expected_revision:
                raise ValueError("知识库配置已变化, 请刷新后重试")
            config = json.loads(row["config_json"])
            model = named_model_config(self.models, "embed", config["embed"])
            if embedding_identity(model) != row["embed_identity"]:
                raise ValueError("Embedding 模型已变化, 请先重建知识库")
            source_id = hashlib.sha256(source.strip().encode("utf-8")).hexdigest()
            current = self.documents(kb_id)
            if config["duplicate_policy"] == "skip" and any(item["source_id"] == source_id for item in current):
                return {"status": "skipped", "source_id": source_id, "chunks": 0}
            generation = uuid.uuid4().hex
            index = self._stage(row, generation)
            record: dict[str, Any] = {"source_id": source_id, "source": source.strip(), "source_text": text, "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(), "updated_at": time.time()}
            try:
                count = self._embed_source(index, config, record, model, progress)
                if progress:
                    progress("publishing", "chunks", count, count)
                self._publish(database, row, generation, index)
                self._discard_inactive(row, generation)
                return {"status": "indexed", "source_id": source_id, "chunks": count}
            except Exception as error:
                self._discard_inactive(row, row["generation"])
                self._record_error(database, kb_id, f"索引处理失败 ({type(error).__name__})")
                raise

    def _publish(self, database: Path, row: dict[str, Any], generation: str, index: DataBase, config: dict[str, Any] | None = None, identity: str | None = None) -> None:
        with closing(self._connect(database)) as connection, connection:
            changed = connection.execute(
                "UPDATE rag_knowledge_bases SET generation=?, config_json=?, embed_identity=?, revision=revision+1, status='ready', last_error='', "
                "document_count=?, chunk_count=?, updated_at=? WHERE id=? AND revision=?",
                (generation, json.dumps(config) if config is not None else row["config_json"], identity or row["embed_identity"],
                 index.source_count("chunks"), index.get_collection_stats("chunks")["document_count"], time.time(), row["id"], row["revision"]),
            ).rowcount
            if changed != 1:
                raise ValueError("知识库配置已被其他操作修改, 请刷新后重试")

    def _record_error(self, database: Path, kb_id: str, error: str) -> None:
        with closing(self._connect(database)) as connection, connection:
            connection.execute("UPDATE rag_knowledge_bases SET status='error', last_error=?, updated_at=? WHERE id=?", (error, time.time(), kb_id))

    def rebuild(self, kb_id: str, config: dict[str, Any], expected_revision: int, *, progress: RagProgress | None = None) -> dict[str, Any]:
        """
        新版本完成后原子切换指针, 失败保留原索引及原配置

        参数:
        - kb_id: 目标知识库
        - config: 新的分块与向量配置
        - expected_revision: 开始及发布时核验的修订
        - progress: 可选阶段内进度回调

        返回:
        - 已成功发布的知识库元数据
        """
        self._validate_revision(expected_revision)
        _, initial = self._get(kb_id)
        with self._library_lock(initial):
            database, row = self._get(kb_id)
            if row["revision"] != expected_revision:
                raise ValueError("知识库配置已变化, 请刷新后重试")
            config = self._validate_kb(config)
            model = named_model_config(self.models, "embed", config["embed"])
            generation = uuid.uuid4().hex
            index = DataBase(str(self._root(row) / generation))
            index.create_collection("chunks")
            try:
                with closing(self._sources(row)) as sources:
                    for source in sources:
                        self._embed_source(index, config, source, model, progress)
                if progress:
                    progress("publishing", "documents", row["document_count"], row["document_count"])
                self._publish(database, row, generation, index, config, embedding_identity(model))
                self._discard_inactive(row, generation)
            except Exception as error:
                self._discard_inactive(row, row["generation"])
                self._record_error(database, kb_id, f"索引处理失败 ({type(error).__name__})")
                raise
        return next(item for item in self.list() if item["id"] == kb_id)

    def delete_document(self, kb_id: str, source_id: str) -> bool:
        _, initial = self._get(kb_id)
        with self._library_lock(initial):
            database, row = self._get(kb_id)
            index = self._index(row)
            if index is None or not any(item["source_id"] == source_id for item in index.source_documents("chunks")):
                return False
            generation = uuid.uuid4().hex
            index = self._stage(row, generation)
            try:
                index.replace_source("chunks", source_id, [], [], [])
                self._publish(database, row, generation, index)
                with closing(self._connect(database)) as connection, connection:
                    connection.execute("DELETE FROM rag_documents WHERE kb_id=? AND source_id=?", (kb_id, source_id))
            finally:
                _, current = self._get(kb_id)
                self._discard_inactive(current, current["generation"])
            return True

    def delete(self, kb_id: str) -> None:
        """先隔离索引再提交删除, 清理范围限定在该知识库的私有目录"""
        _, row = self._get(kb_id)
        with self._library_lock(row):
            database, row = self._get(kb_id)
            root = self._root(row)
            if root.is_symlink() or not root.resolve().is_relative_to(self.layout.root.resolve()):
                raise ValueError("知识库索引目录越出存储范围")
            isolated = root.with_name(f".deleted-{root.name}-{uuid.uuid4().hex}")
            moved = False
            try:
                with ExitStack() as readers, closing(self._connect(database)) as connection, connection:
                    if root.exists():
                        for generation in root.iterdir():
                            if len(generation.name) == 32 and generation.is_dir():
                                readers.enter_context(FileLock(self._lock_path(row, generation.name)))
                    readers.enter_context(FileLock(self._lock_path(row, "empty")))
                    if root.exists():
                        root.rename(isolated)
                        moved = True
                    connection.execute("DELETE FROM rag_knowledge_bases WHERE id=?", (kb_id,))
                    connection.execute("DELETE FROM rag_jobs WHERE kb_id=?", (kb_id,))
                    connection.execute("DELETE FROM rag_documents WHERE kb_id=?", (kb_id,))
            except BaseException:
                if moved:
                    isolated.rename(root)
                raise
            if moved:
                shutil.rmtree(isolated)

    def update(self, kb_id: str, name: str, config: dict[str, Any], expected_revision: int, is_default: bool = False) -> dict[str, Any]:
        """更新展示和导入策略, 向量空间及分块变更必须走显式重建"""
        self._validate_revision(expected_revision)
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("知识库名称无效")
        if not isinstance(is_default, bool):
            raise ValueError("is_default 必须是布尔值")
        config = self._validate_kb(config)
        _, initial = self._get(kb_id)
        with self._library_lock(initial):
            database, row = self._get(kb_id)
            previous = json.loads(row["config_json"])
            if any(config[key] != previous[key] for key in ("embed", "chunk_size", "chunk_overlap")):
                raise ValueError("模型或分块参数变更需要显式重建知识库")
            with closing(self._connect(database)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                if row["scope"] == "session" and is_default:
                    connection.execute("UPDATE rag_knowledge_bases SET is_default=0, revision=revision+1, updated_at=? WHERE session_id=? AND is_default=1 AND id<>?", (time.time(), row["session_id"], kb_id))
                changed = connection.execute(
                    "UPDATE rag_knowledge_bases SET name=?, config_json=?, is_default=?, revision=revision+1, updated_at=? WHERE id=? AND revision=?",
                    (name.strip(), json.dumps(config), int(is_default and row["scope"] == "session"), time.time(), kb_id, expected_revision),
                ).rowcount
                if changed != 1:
                    raise ValueError("知识库已更新, 请刷新后重试")
        return next(item for item in self.list() if item["id"] == kb_id)

    def validate_references(self, values: dict[str, Any]) -> dict[str, Any]:
        """保存及安装时验证显式引用, 允许尚未建库的会话继承默认选择"""
        config = validate_rag_config(values)
        for key, scope in (("global_db_ids", "global"), ("session_db_ids", "session")):
            for kb_id in config[key]:
                _, row = self._get(kb_id)
                if row["scope"] != scope:
                    raise ValueError(f"{key} 包含层级不匹配的知识库")
                if scope == "session" and not self.session_id:
                    raise ValueError("会话库引用只能配置在具体会话的参数覆盖中")
        target = config["write_db_id"]
        if target:
            _, row = self._get(target)
            if row["scope"] == "global":
                permitted = config["db_scope"] in {"global", "session_global"} and target in config["global_db_ids"]
            else:
                permitted = bool(self.session_id) and config["db_scope"] in {"session", "session_global"} and (target in config["session_db_ids"] or (not config["session_db_ids"] and row["is_default"]))
            if not permitted:
                raise ValueError("写入目标不在允许的知识库范围内")
        return config

    def allowed_ids(self, values: dict[str, Any]) -> list[str]:
        config = validate_rag_config(values)
        ids: list[str] = []
        if config["db_scope"] in {"global", "session_global"}:
            if not config["global_db_ids"]:
                raise ValueError("未选择全局知识库")
            for kb_id in config["global_db_ids"]:
                _, row = self._get(kb_id)
                if row["scope"] != "global":
                    raise ValueError("全局库选择包含会话库")
                ids.append(kb_id)
        if config["db_scope"] in {"session", "session_global"}:
            if not self.session_id:
                raise ValueError("会话检索需要会话身份")
            session_ids = config["session_db_ids"] or [item["id"] for item in self.list() if item["scope"] == "session" and item["is_default"]]
            if not session_ids:
                raise ValueError("当前会话未配置知识库")
            for kb_id in session_ids:
                _, row = self._get(kb_id)
                if row["scope"] != "session":
                    raise ValueError("会话库选择包含全局库")
                ids.append(kb_id)
        return list(dict.fromkeys(ids))

    def write_target(self, config: dict[str, Any], requested: str = "") -> str:
        allowed = self.allowed_ids(config)
        selected = requested or config.get("write_db_id", "")
        if selected:
            if selected not in allowed:
                raise ValueError("写入目标不在允许的知识库范围内")
            return selected
        local = [item for item in self.list() if item["id"] in allowed and item["scope"] == "session"]
        default = next((item["id"] for item in local if item["is_default"]), None)
        if default:
            return default
        if len(local) == 1:
            return local[0]["id"]
        if len(allowed) == 1:
            return allowed[0]
        raise ValueError("有多个可写库, 请指定目标知识库")

    def search(self, query: str, values: dict[str, Any], requested_ids: list[str] | None = None) -> dict[str, Any]:
        started = time.monotonic()
        if not isinstance(query, str) or not query.strip() or len(query) > 32000:
            raise ValueError("检索问题必须为 1 到 32000 个字符")
        config = validate_rag_config(values)
        allowed = self.allowed_ids(config)
        selected = requested_ids if requested_ids is not None else allowed
        if not selected or not isinstance(selected, list) or any(item not in allowed for item in selected):
            raise ValueError("检索目标不在配置允许的范围内")
        candidates: dict[str, dict[str, Any]] = {}
        retrieved = 0
        for kb_id in dict.fromkeys(selected):
            with self.generation(kb_id) as row:
                kb_config = json.loads(row["config_json"])
                model = named_model_config(self.models, "embed", kb_config["embed"])
                if embedding_identity(model) != row["embed_identity"]:
                    raise ValueError(f"知识库 {row['name']} 的 Embedding 已变化, 请重建")
                index = self._index(row)
                if index is None or not index.get_collection_stats("chunks")["document_count"]:
                    continue
                client = build_model_client("embed", model)
                try:
                    vector = client.embed(query)
                finally:
                    self._close(client)
                if not vector:
                    raise ValueError("问题向量化失败")
                threshold = config["similarity_threshold"]
                results = index.search("chunks", vector, config["candidate_k"], -1.0)
                retrieved += len(results)
                if threshold is not None:
                    results = [result for result in results if result["score"] >= threshold]
                for rank, result in enumerate(results):
                    key = hashlib.sha256(result["document"].encode("utf-8")).hexdigest()
                    meta = result["metadata"]
                    source: dict[str, Any] = {"kb_id": kb_id, "kb_name": row["name"], "scope": row["scope"], "source_id": meta["source_id"], "source": meta["source"], "chunk_index": meta["chunk_index"], "similarity": result["score"]}
                    if key not in candidates:
                        candidates[key] = {"text": result["document"], "sources": [], "fusion_score": 0.0}
                    candidates[key]["sources"].append(source)
                    candidates[key]["fusion_score"] += 1 / (60 + rank + 1)
        ordered = sorted(candidates.values(), key=lambda item: item["fusion_score"], reverse=True)
        warnings: list[str] = []
        rerank_status = "not_requested" if not config["rerank"] else "not_needed"
        if config["rerank"] and ordered:
            client = None
            try:
                model = named_model_config(self.models, "rerank", config["rerank"])
                client = build_model_client("rerank", model)
                client.suppress_error = False
                ranks = client.call(query, [item["text"] for item in ordered], top_k=len(ordered), min_score=config["rerank_min_score"])
                seen: set[int] = set()
                reranked: list[dict[str, Any]] = []
                for item in ranks:
                    position = item.get("original_index")
                    if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position < len(ordered) or position in seen:
                        raise ValueError("重排响应包含无效片段索引")
                    seen.add(position)
                    score = item.get("score")
                    if isinstance(score, bool) or not isinstance(score, (float, int)) or not math.isfinite(score):
                        raise ValueError("重排响应包含无效分数")
                    reranked.append({**ordered[position], "rerank_score": item["score"]})
                ordered = sorted(reranked, key=lambda item: item["rerank_score"], reverse=True)
                rerank_status = "applied"
            except Exception:
                if config["rerank_failure_policy"] == "error":
                    raise ValueError("重排失败") from None
                warnings.append("重排失败, 已使用检索排名融合")
                rerank_status = "fallback"
            finally:
                if client is not None:
                    self._close(client)
        remaining = config["max_result_chars"]
        output: list[dict[str, Any]] = []
        for item in ordered[:config["top_k"]]:
            if remaining <= 0:
                break
            text = item["text"][:remaining]
            output.append({**item, "text": text, "truncated": len(text) < len(item["text"])})
            remaining -= len(text)
        return {"status": "found" if output else "empty", "results": output, "warnings": warnings,
                "elapsed_ms": (time.monotonic() - started) * 1000, "candidates": retrieved,
                "returned_count": len(output), "rerank_status": rerank_status,
                "truncated": any(item["truncated"] for item in output) or len(output) < min(len(ordered), config["top_k"])}
