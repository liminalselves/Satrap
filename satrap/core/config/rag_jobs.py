"""
持久知识库后台任务

只管理 RAG 导入和重建, 复用知识库修订及 generation 发布, 不自动重放供应商请求
"""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import closing, nullcontext
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Literal, TypedDict, cast
from uuid import uuid4

from satrap.core.config.model_snapshot import ModelConfigReader
from satrap.core.log import logger
from satrap.core.log.managed import LogFileLock
from satrap.core.rag import RagService, ensure_rag_tables
from satrap.core.storage import StorageLayout
from satrap.core.storage.file_lock import FileLease, session_storage_lock
from satrap.core.utils.async_worker import BoundedAsyncWorker
from satrap.core.utils.async_worker import RAG_WORKERS
from satrap.core.utils.documents import DEFAULT_MAX_FILE_SIZE, SUPPORTED_EXTENSIONS, extract_document


JobStatus = Literal["queued", "running", "succeeded", "failed", "interrupted"]
DocumentStatus = Literal["queued", "indexing", "ready", "failed"]
Json = None | bool | int | float | str | list["Json"] | dict[str, "Json"]


class Progress(TypedDict):
    """阶段内进度, 未知工作量不伪造百分比"""

    unit: str
    done: int
    total: int | None
    percent: float | None


class JobError(TypedDict):
    """不含原始供应商凭据的任务失败资料"""

    phase: str
    code: str
    message: str


class RagJob(TypedDict):
    """公开任务记录, 不返回内部输入路径和请求正文"""

    job_id: str
    operation_id: str
    owner_service: str
    owner_runtime_id: str
    platform_id: str
    session_id: str
    kb_id: str
    action: str
    status: JobStatus
    phase: str
    progress: Progress
    created_at: float
    started_at: float | None
    finished_at: float | None
    error: JobError | None
    result: dict[str, Json] | None


class RagDocument(TypedDict):
    """当前来源状态与最后成功索引时间"""

    document_id: str
    source_id: str
    name: str
    size_bytes: int | None
    created_at: float | None
    updated_at: float
    last_indexed_at: float | None
    status: DocumentStatus
    error: JobError | None
    job_id: str | None
    chunk_count: int


class RagPage(TypedDict):
    """有界目录分页, 当前页只读存储快照"""

    items: list[dict[str, object]]
    has_more: bool
    next_cursor: str | None


def _paging(query: Mapping[str, str], scope: tuple[str, ...]) -> tuple[int, list[object] | None, str]:
    """
    校验页大小并将游标限定在同一知识库和筛选

    参数:
    - query: 已解析的单值参数
    - scope: 路由和身份构成的固定筛选

    返回:
    - 页大小, 继续键和筛选摘要
    """
    try:
        limit = int(query.get("limit", "50"))
    except ValueError as error:
        raise RagJobError(400, "invalid_limit", "limit 必须为整数") from error
    if not 1 <= limit <= 100:
        raise RagJobError(400, "invalid_limit", "limit 必须为 1 到 100")
    fingerprint = hashlib.sha256(json.dumps(scope).encode()).hexdigest()
    cursor = query.get("cursor", "")
    if not cursor:
        return limit, None, fingerprint
    try:
        if len(cursor) > 4096:
            raise ValueError()
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(value, dict):
            raise ValueError()
        value = cast(dict[str, object], value)
        after = value.get("after")
        if value.get("scope") != fingerprint or not isinstance(after, list) or not 1 <= len(cast(list[object], after)) <= 2:
            raise ValueError()
        return limit, cast(list[object], value["after"]), fingerprint
    except (ValueError, TypeError, UnicodeError) as error:
        raise RagJobError(400, "invalid_cursor", "游标格式错误或筛选已变化") from error


def _cursor(scope: str, after: list[object]) -> str:
    """
    编码稳定排序继续键

    参数:
    - scope: 筛选摘要
    - after: 最后返回项的排序键

    返回:
    - 不含正文的有界游标
    """
    return base64.urlsafe_b64encode(json.dumps({"scope": scope, "after": after}).encode()).decode().rstrip("=")


def rag_jobs_read(service: RagService, resource: str, identity: str, query: Mapping[str, str]) -> dict[str, object]:
    """
    在只读连接中分页任务, 文档或片段, 路径身份不能越过库范围

    参数:
    - service: 宿主校验的平台及会话范围
    - resource: jobs, documents 或 chunks
    - identity: 路径中的任务或文档 ID, 列表时为空
    - query: 知识库, 筛选和分页参数

    返回:
    - 任务详情或有界 Page, 旧库查询不升级表
    """
    kb_id = query.get("kb_id", "")
    if not kb_id:
        raise RagJobError(400, "missing_scope", "必须提供 kb_id 和所属平台/会话范围")
    database, owner = service._get(kb_id)
    if owner["scope"] == "session" and service.session_id != owner["session_id"]:
        raise RagJobError(400, "missing_scope", "会话知识库必须提供所属 session_id")
    if identity and re.fullmatch(r"[a-f0-9]{32,64}", identity) is None:
        raise RagJobError(404, "not_found", "任务或文档不存在")
    operation = query.get("operation_id", "")
    if len(operation) > 128:
        raise RagJobError(400, "invalid_operation_id", "operation_id 过长")
    include_text = query.get("include_text", "false")
    if include_text not in {"true", "false"}:
        raise RagJobError(400, "invalid_request", "include_text 必须为 true/false")
    limit, after, scope = _paging(query, (resource, identity, kb_id, service.platform_id, service.session_id, operation, include_text))
    with closing(_connect(database)) as connection:
        if resource == "jobs":
            if not _exists(connection, "rag_jobs"):
                if identity:
                    raise RagJobError(404, "not_found", "任务不存在")
                return {"items": [], "has_more": False, "next_cursor": None}
            clauses = ["kb_id=?", "platform_id=?", "session_id=?"]
            values: list[object] = [kb_id, service.platform_id, owner["session_id"]]
            if identity:
                clauses.append("job_id=?")
                values.append(identity)
            if operation:
                clauses.append("operation_id=?")
                values.append(operation)
            if after:
                if (len(after) != 2 or not isinstance(after[0], (int, float)) or isinstance(after[0], bool)
                        or not math.isfinite(after[0]) or after[0] < 0 or not isinstance(after[1], str)):
                    raise RagJobError(400, "invalid_cursor", "任务游标排序键无效")
                clauses.append("(created_at,job_id)<(?,?)")
                values.extend(after)
            rows = connection.execute("SELECT * FROM rag_jobs WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC,job_id DESC LIMIT ?", (*values, limit + 1)).fetchall()
            if identity:
                if not rows:
                    raise RagJobError(404, "not_found", "任务不存在")
                return {"job": _job(rows[0])}
            page = rows[:limit]
            return {"items": [_job(row) for row in page], "has_more": len(rows) > limit,
                    "next_cursor": _cursor(scope, [page[-1]["created_at"], page[-1]["job_id"]]) if len(rows) > limit else None}
    with service.generation(kb_id) as row, closing(_connect(database)) as connection:
        if row["generation"]:
            index = service._root(row) / row["generation"] / "metadata.sqlite"
            connection.execute("ATTACH DATABASE ? AS published", (index.as_uri() + "?mode=ro",))
            legacy = ("SELECT json_extract(metadata,'$.source_id') AS source_id, json_extract(metadata,'$.source') AS name, "
                      "json_extract(metadata,'$.updated_at') AS updated_at,json_extract(metadata,'$.chunk_count') AS chunk_count "
                      "FROM published.documents WHERE collection_name='chunks' AND json_extract(metadata,'$.chunk_index')=0")
        else:
            legacy = "SELECT NULL AS source_id,NULL AS name,NULL AS updated_at,0 AS chunk_count WHERE 0"
        if resource == "chunks":
            if not row["generation"]:
                return {"items": [], "has_more": False, "next_cursor": None}
            if not identity:
                raise RagJobError(400, "missing_document", "必须指定文档 ID")
            source_id: str | None = None
            if _exists(connection, "rag_documents"):
                found = connection.execute("SELECT source_id FROM rag_documents WHERE kb_id=? AND document_id=?", (kb_id, identity)).fetchone()
                source_id = found[0] if found else None
            if source_id is None:
                for found in connection.execute("WITH legacy AS (" + legacy + ") SELECT source_id FROM legacy"):
                    if _document_id(kb_id, found[0]) == identity:
                        source_id = found[0]
                        break
            if source_id is None:
                raise RagJobError(404, "not_found", "文档不存在")
            if after and (len(after) != 1 or type(after[0]) is not int or cast(int, after[0]) < 0):
                raise RagJobError(400, "invalid_cursor", "片段游标无效")
            expression = ",document AS text" if include_text == "true" else ""
            rows = connection.execute("SELECT id,json_extract(metadata,'$.chunk_index') AS chunk_index,"
                                      "json_extract(metadata,'$.chunk_count') AS chunk_count" + expression +
                                      " FROM published.documents WHERE collection_name='chunks' AND json_extract(metadata,'$.source_id')=? "
                                      "AND json_extract(metadata,'$.chunk_index')>? ORDER BY json_extract(metadata,'$.chunk_index'),id LIMIT ?",
                                      (source_id, after[0] if after else -1, limit + 1)).fetchall()
            page = rows[:limit]
            return {"items": [{"record_id": item["id"], "document_id": identity, **dict(item)} for item in page],
                    "has_more": len(rows) > limit, "next_cursor": _cursor(scope, [page[-1]["chunk_index"]]) if len(rows) > limit else None}
        stored = ("SELECT document_id,source_id,name,size_bytes,created_at,updated_at,last_indexed_at,status,error_json,job_id,chunk_count FROM rag_documents WHERE kb_id=?"
                  if _exists(connection, "rag_documents") else "SELECT NULL AS document_id,NULL AS source_id,NULL AS name,NULL AS size_bytes,NULL AS created_at,"
                  "NULL AS updated_at,NULL AS last_indexed_at,NULL AS status,NULL AS error_json,NULL AS job_id,0 AS chunk_count WHERE 0")
        def document_identity(source: str) -> str:
            """
            将 SQLite 已保存的来源标识转换为稳定文档身份

            参数:
            - source: 索引中真实保存的来源标识

            返回:
            - 当前知识库内的文档身份
            """
            return _document_id(kb_id, source)
        connection.create_function("document_identity", 1, document_identity, deterministic=True)
        sql = ("WITH legacy AS (" + legacy + "), stored AS (" + stored + "), combined AS ("
               "SELECT s.document_id,s.source_id,s.name,s.size_bytes,s.created_at,s.updated_at,s.last_indexed_at,s.status,s.error_json,s.job_id,"
               "COALESCE(l.chunk_count,s.chunk_count) AS chunk_count FROM stored s LEFT JOIN legacy l USING(source_id) "
               "UNION ALL SELECT document_identity(l.source_id),l.source_id,l.name,NULL,NULL,l.updated_at,NULL,'ready',NULL,NULL,l.chunk_count "
               "FROM legacy l WHERE NOT EXISTS (SELECT 1 FROM stored s WHERE s.source_id=l.source_id)) SELECT * FROM combined")
        values: list[object] = [kb_id] if _exists(connection, "rag_documents") else []
        if after:
            if len(after) != 1 or not isinstance(after[0], str):
                raise RagJobError(400, "invalid_cursor", "文档游标无效")
            sql += " WHERE document_id>?"
            values.extend(after)
        rows = connection.execute(sql + " ORDER BY document_id LIMIT ?", (*values, limit + 1)).fetchall()
        page = rows[:limit]
        items = [{key: item[key] for key in RagDocument.__annotations__ if key != "error"} | {"error": json.loads(item["error_json"]) if item["error_json"] else None} for item in page]
        return {"items": items, "has_more": len(rows) > limit,
                "next_cursor": _cursor(scope, [page[-1]["document_id"]]) if len(rows) > limit else None}


class RagJobError(ValueError):
    """具有稳定 HTTP 错误码的任务入口异常"""

    def __init__(self, status: int, code: str, message: str) -> None:
        """
        保存接口可直接返回的有限错误资料

        参数:
        - status: HTTP 状态
        - code: 稳定错误码
        - message: 宿主生成的说明
        """
        super().__init__(message)
        self.status, self.code = status, code


@dataclass(frozen=True)
class _Queued:
    database: Path
    platform_id: str
    session_id: str
    kb_id: str
    job_id: str
    input_name: str = ""


def _connect(path: Path, *, write: bool = False) -> sqlite3.Connection:
    """
    打开已确认所属的数据库, 只读查询不创建库或迁移

    参数:
    - path: 宿主解析的现有知识库数据库
    - write: 状态写入时补齐本模块的表

    返回:
    - 调用者负责关闭的连接
    """
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    if write:
        ensure_rag_tables(connection)
        connection.commit()
    return connection


def _exists(connection: sqlite3.Connection, table: str) -> bool:
    """
    检查旧库是否具有某张表

    参数:
    - connection: 已打开的只读连接
    - table: 内部固定表名

    返回:
    - 表是否存在
    """
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _job(row: sqlite3.Row) -> RagJob:
    """
    投影公开任务字段

    参数:
    - row: 持久任务行

    返回:
    - 不包含正文或磁盘路径的任务
    """
    return RagJob(job_id=row["job_id"], operation_id=row["operation_id"], owner_service=row["owner_service"],
                  owner_runtime_id=row["owner_runtime_id"], platform_id=row["platform_id"], session_id=row["session_id"],
                  kb_id=row["kb_id"], action=row["action"], status=row["status"], phase=row["phase"],
                  progress=json.loads(row["progress_json"]), created_at=row["created_at"], started_at=row["started_at"],
                  finished_at=row["finished_at"], error=json.loads(row["error_json"]) if row["error_json"] else None,
                  result=json.loads(row["result_json"]) if row["result_json"] else None)


def _document_id(kb_id: str, source_id: str) -> str:
    """
    从知识库和来源构造稳定文档身份

    参数:
    - kb_id: 知识库 ID
    - source_id: 规范来源 ID

    返回:
    - 跨 generation 稳定的 SHA-256
    """
    return hashlib.sha256((kb_id + ":" + source_id).encode()).hexdigest()


async def rag_jobs_response(manager: RagJobManager | None, service: RagService, method: str, route: str,
                            query: Mapping[str, str], payload: Mapping[str, object], content: bytes | None = None) -> tuple[int, dict[str, object]]:
    """
    分派已经完成宿主鉴权和范围校验的 RAG 新接口

    参数:
    - manager: 当前服务唯一任务持有者, 只读请求允许为 None
    - service: 已限定平台和会话的业务服务
    - method: HTTP 方法
    - route: 去除服务前缀后的 /jobs 或 /documents 路径
    - query: 单值查询参数
    - payload: 已解析的任务请求体
    - content: 上传的原始正文, 其他请求为 None

    返回:
    - 保持同步旧接口独立的 HTTP 状态和响应
    """
    if method == "POST" and route in {"/jobs", "/jobs/upload"}:
        if manager is None or manager.layout.root != service.layout.root:
            raise RagJobError(503, "rag_jobs_unavailable", "知识库任务服务尚未就绪或数据根已变更")
        request: dict[str, object] = dict(payload) if route == "/jobs" else {**query, "action": "ingest"}
        if route == "/jobs/upload":
            try:
                request["expected_revision"] = int(query.get("expected_revision", ""))
            except ValueError as error:
                raise RagJobError(400, "invalid_revision", "expected_revision 必须是正整数") from error
        job = await manager.submit(service, request, content)
        return 202, {"job": job}
    if method == "GET" and route == "/jobs":
        return 200, await RAG_WORKERS.run(rag_jobs_read, service, "jobs", "", query)
    if method == "GET" and re.fullmatch(r"/jobs/[a-f0-9]{32}", route):
        return 200, await RAG_WORKERS.run(rag_jobs_read, service, "jobs", route.rsplit("/", 1)[-1], query)
    if method == "GET" and route == "/documents":
        return 200, await RAG_WORKERS.run(rag_jobs_read, service, "documents", "", query)
    match = re.fullmatch(r"/documents/([a-f0-9]{64})/chunks", route)
    if method == "GET" and match:
        return 200, await RAG_WORKERS.run(rag_jobs_read, service, "chunks", match[1], query)
    raise RagJobError(404, "not_found", "知识库任务接口不存在")


class RagJobManager:
    """单服务唯一拥有者, 持久队列与有界工作线程的生命周期边界"""

    def __init__(self, layout: StorageLayout, owner_service: Literal["control", "chat"], runtime_id: str,
                 models: Callable[[], ModelConfigReader]) -> None:
        """
        创建尚未启动的任务管理器

        参数:
        - layout: 当前宿主的数据根
        - owner_service: 任务持有服务
        - runtime_id: 本次服务启动身份
        - models: 工作线程内才读取的可信模型配置来源
        """
        self.layout, self.runtime_id, self.models = layout, runtime_id, models
        self.owner_service: Literal["control", "chat"] = owner_service
        self._lease = LogFileLock(layout.root / "rag" / "jobs" / f"{owner_service}.lock")
        self._worker = BoundedAsyncWorker(f"rag-jobs-{owner_service}", 2, 2)
        self._pending: deque[_Queued] = deque()
        self._active: dict[str, asyncio.Task[None]] = {}
        self._guard = asyncio.Lock()
        self._admissions: set[asyncio.Task[RagJob]] = set()
        self._closing, self._started = False, False
        self._imports = layout.root / "rag" / "imports" / "jobs"

    async def start(self, platform_ids: list[str]) -> None:
        """
        获取服务租约后中断本服务遗留任务, 不接管其他服务

        参数:
        - platform_ids: 宿主配置允许扫描的平台
        """
        if self._started:
            return
        await asyncio.to_thread(self._startup, platform_ids)
        self._started = True

    def _startup(self, platforms: list[str]) -> None:
        """
        在工作线程内修复异常退出的持久状态

        参数:
        - platforms: 已授权的平台范围
        """
        if not self._lease.acquire():
            raise RagJobError(503, "rag_owner_busy", "同一数据根已有该服务的知识库任务持有者")
        try:
            for database in {self.layout.root / "rag" / "global.db", *(self.layout.platform_db(item) for item in platforms)}:
                if not database.is_file():
                    continue
                with closing(_connect(database)) as connection:
                    if not _exists(connection, "rag_jobs"):
                        continue
                    jobs = connection.execute("SELECT job_id,platform_id,session_id,kb_id FROM rag_jobs "
                                              "WHERE owner_service=? AND status IN ('queued','running')", (self.owner_service,)).fetchall()
                for row in jobs:
                    item = _Queued(database, row["platform_id"], row["session_id"], row["kb_id"], row["job_id"])
                    self._finish(item, "interrupted", JobError(phase="finished", code="service_restarted", message="服务重启, 未自动重放任务"))
            if self._imports.is_dir():
                for path in self._imports.glob(f"{self.owner_service}-*"):
                    self._cleanup_input(path.name)
        except BaseException:
            self._lease.release()
            raise

    async def submit(self, service: RagService, payload: Mapping[str, object], content: bytes | None = None) -> RagJob:
        """
        持久保存输入后接受任务, 同操作 ID 不重复执行

        参数:
        - service: 已校验平台和会话的知识库服务
        - payload: 文本导入, 上传或重建参数
        - content: 可选二进制上传正文

        返回:
        - 新任务或完全相同请求对应的原任务
        """
        task = asyncio.create_task(self._admit(service, payload, content))
        self._admissions.add(task)

        def observed(done: asyncio.Task[RagJob]) -> None:
            """
            即使客户端离开也回收接收任务的异常

            参数:
            - done: 已完成的内部接收任务
            """
            self._admissions.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(observed)
        return await asyncio.shield(task)

    async def _admit(self, service: RagService, payload: Mapping[str, object], content: bytes | None) -> RagJob:
        """
        将持久登记与本进程排队作为不受请求取消影响的单个接收流程

        参数:
        - service: 可信知识库范围
        - payload: 已收到的请求参数
        - content: 完整上传内容或 None

        返回:
        - 已登记且安排执行的任务
        """
        async with self._guard:
            if not self._started or self._closing:
                raise RagJobError(503, "rag_jobs_unavailable", "知识库任务服务尚未就绪")
            job, queued = await asyncio.to_thread(self._prepare, service, payload, content, len(self._pending) >= 32)
            if queued is not None:
                self._pending.append(queued)
                self._pump()
            return job

    def _prepare(self, service: RagService, payload: Mapping[str, object], content: bytes | None,
                 full: bool) -> tuple[RagJob, _Queued | None]:
        """
        校验输入并在同一事务登记操作身份与文档

        参数:
        - service: 已限定范围的业务服务
        - payload: 固定字段请求
        - content: 上传正文或 None
        - full: 当前等待队列是否已满

        返回:
        - 公开任务与需要调度的新任务, 幂等命中时后者为 None
        """
        allowed = {"operation_id", "kb_id", "expected_revision", "action", "text", "source", "config", "file_name", "platform_id", "session_id"}
        if set(payload) - allowed:
            raise RagJobError(400, "invalid_request", "任务包含未知参数")
        operation, kb_id, action = payload.get("operation_id"), payload.get("kb_id"), payload.get("action")
        revision = payload.get("expected_revision")
        if not isinstance(operation, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation) is None:
            raise RagJobError(400, "invalid_operation_id", "operation_id 必须为 1 到 128 个英文字符, 数字, 下划线或连字符")
        if not isinstance(kb_id, str) or not kb_id or not isinstance(action, str) or action not in {"ingest", "rebuild"}:
            raise RagJobError(400, "invalid_request", "必须指定知识库和 ingest/rebuild 操作")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise RagJobError(400, "invalid_revision", "expected_revision 必须是正整数")
        database, row = service._get(kb_id)
        if payload.get("platform_id", service.platform_id) != service.platform_id or payload.get("session_id", service.session_id) != service.session_id:
            raise RagJobError(400, "scope_mismatch", "正文和请求路径的知识库范围不一致")
        if row["scope"] == "session" and service.session_id != row["session_id"]:
            raise RagJobError(400, "missing_scope", "会话知识库必须提供所属 session_id")
        source = payload.get("source", "")
        filename = payload.get("file_name", "")
        text = payload.get("text", "")
        config = payload.get("config", {})
        if not isinstance(source, str) or not isinstance(filename, str):
            raise RagJobError(400, "invalid_document", "来源和文件名必须是字符串")
        if action == "rebuild":
            if content is not None or text or source or filename:
                raise RagJobError(400, "invalid_request", "重建操作不接受文档输入")
            if not isinstance(config, dict):
                raise RagJobError(400, "invalid_config", "重建配置必须是对象")
            service._validate_kb(cast(dict[str, object], config))
        elif content is not None:
            if not content or len(content) > DEFAULT_MAX_FILE_SIZE or text:
                raise RagJobError(400, "invalid_document", "上传必须为非空且不超过 32 MiB 的单个文件")
            if not filename or len(filename) > 1000 or Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise RagJobError(400, "invalid_document", "文件类型或文件名无效")
            source = source or Path(filename.replace("\\", "/")).name
        elif not isinstance(text, str) or not text.strip() or len(text) > 1_000_000:
            raise RagJobError(400, "invalid_document", "正文必须为 1 到 1000000 个字符")
        if action == "ingest" and (not source.strip() or len(source) > 1000):
            raise RagJobError(400, "invalid_document", "文档来源必须为 1 到 1000 个字符")
        source = source.strip()
        binary = content if content is not None else cast(str, text).encode("utf-8")
        request: dict[str, Json] = {"expected_revision": revision, "source": source, "config": cast(dict[str, Json], config), "upload": content is not None}
        fingerprint = hashlib.sha256(json.dumps({**request, "action": action, "file_name": filename,
                                               "platform_id": service.platform_id, "session_id": row["session_id"],
                                               "content": hashlib.sha256(binary).hexdigest()}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        job_id = uuid4().hex
        temporary: Path | None = None
        legacy: tuple[int, float | None] | None = None
        if action == "ingest":
            source_id = hashlib.sha256(source.encode()).hexdigest()
            with service.generation(kb_id) as published:
                if published["generation"]:
                    index_path = service._root(published) / published["generation"] / "metadata.sqlite"
                    with closing(_connect(index_path)) as index:
                        previous = index.execute("SELECT json_extract(metadata,'$.chunk_count'),json_extract(metadata,'$.updated_at') FROM documents "
                                                 "WHERE collection_name='chunks' AND json_extract(metadata,'$.source_id')=? "
                                                 "AND json_extract(metadata,'$.chunk_index')=0 LIMIT 1", (source_id,)).fetchone()
                        if previous:
                            legacy = (int(previous[0]), previous[1])
        lease = FileLease(session_storage_lock(self.layout, service.platform_id, row["session_id"]).path, shared=True) if row["scope"] == "session" else nullcontext()
        with lease, closing(_connect(database, write=True)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM rag_jobs WHERE kb_id=? AND operation_id=?", (kb_id, operation)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise RagJobError(409, "operation_conflict", "operation_id 已用于不同请求")
                return _job(existing), None
            if full:
                raise RagJobError(503, "rag_queue_full", "知识库等待队列已满")
            current = connection.execute("SELECT revision FROM rag_knowledge_bases WHERE id=?", (kb_id,)).fetchone()
            if current is None or current[0] != revision:
                raise RagJobError(409, "revision_conflict", "知识库修订已变化")
            now = time.time()
            try:
                if action == "ingest":
                    self._imports.mkdir(parents=True, exist_ok=True)
                    temporary = self._imports / (self.owner_service + "-" + job_id + (Path(filename).suffix.lower() if content is not None else ".txt"))
                    with temporary.open("xb") as file:
                        file.write(binary)
                        file.flush()
                        import os
                        os.fsync(file.fileno())
                connection.execute("INSERT INTO rag_jobs (job_id,operation_id,fingerprint,owner_service,owner_runtime_id,platform_id,session_id,"
                                   "kb_id,action,status,phase,progress_json,created_at,request_json,input_name) VALUES (?,?,?,?,?,?,?,?,?,'queued','queued',?,?,?,?)",
                                   (job_id, operation, fingerprint, self.owner_service, self.runtime_id, service.platform_id, row["session_id"], kb_id,
                                    action, json.dumps(Progress(unit="documents", done=0, total=None, percent=None)), now,
                                    json.dumps(request, ensure_ascii=False), temporary.name if temporary else ""))
                if action == "ingest":
                    source_id = hashlib.sha256(source.encode()).hexdigest()
                    connection.execute("INSERT INTO rag_documents(document_id,kb_id,source_id,session_id,name,size_bytes,created_at,updated_at,status,job_id,chunk_count) "
                                       "VALUES (?,?,?,?,?,?,?,?, 'queued',?,?) ON CONFLICT(kb_id,source_id) DO UPDATE SET name=excluded.name,"
                                       "size_bytes=excluded.size_bytes,updated_at=excluded.updated_at,status='queued',error_json=NULL,job_id=excluded.job_id",
                                       (_document_id(kb_id, source_id), kb_id, source_id, row["session_id"], source, len(binary), None if legacy else now,
                                        now, job_id, legacy[0] if legacy else 0))
                connection.commit()
            except BaseException:
                if temporary:
                    temporary.unlink(missing_ok=True)
                raise
            result = connection.execute("SELECT * FROM rag_jobs WHERE job_id=?", (job_id,)).fetchone()
            assert result is not None
            return _job(result), _Queued(database, service.platform_id, row["session_id"], kb_id, job_id, temporary.name if temporary else "")

    def _pump(self) -> None:
        """只调度不同知识库, 同库等待不占用另一个工作槽"""
        if self._closing:
            return
        for item in tuple(self._pending):
            if len(self._active) >= 2:
                break
            if item.kb_id in self._active:
                continue
            self._pending.remove(item)
            task = asyncio.create_task(self._execute(item))
            self._active[item.kb_id] = task

    async def _execute(self, item: _Queued) -> None:
        """
        等待真实工作线程结束后才释放调度槽

        参数:
        - item: 持久队列身份
        """
        try:
            await self._worker.run(self._run, item)
        except Exception as error:
            logger.error(f"[RAG 任务] 状态写入失败, job={item.job_id}, type={type(error).__name__}")
        finally:
            if item.input_name:
                await asyncio.to_thread(self._cleanup_input, item.input_name)
            self._active.pop(item.kb_id, None)
            self._pump()

    def _run(self, item: _Queued) -> None:
        """
        执行一个已登记任务, 失败保留旧索引且不自动重试

        参数:
        - item: 所属数据库和任务身份
        """
        with closing(_connect(item.database, write=True)) as connection, connection:
            changed = connection.execute("UPDATE rag_jobs SET status='running',started_at=? WHERE job_id=? AND status='queued' "
                                         "AND owner_runtime_id=?", (time.time(), item.job_id, self.runtime_id)).rowcount
            if not changed:
                if item.input_name:
                    self._input(item.input_name).unlink(missing_ok=True)
                return
            row = connection.execute("SELECT * FROM rag_jobs WHERE job_id=?", (item.job_id,)).fetchone()
            assert row is not None
            connection.execute("UPDATE rag_documents SET status='indexing' WHERE job_id=?", (item.job_id,))
        request = json.loads(row["request_json"])
        phase, last_write = "queued", 0.0

        def progress(next_phase: str, unit: str, done: int, total: int | None) -> None:
            """
            限频写入阶段进度

            参数:
            - next_phase: 新阶段
            - unit: documents 或 chunks
            - done: 阶段内完成数量
            - total: 已知总量或 None
            """
            nonlocal phase, last_write
            now = time.monotonic()
            if phase == next_phase and now - last_write < 1:
                return
            phase, last_write = next_phase, now
            state = Progress(unit=unit, done=done, total=total, percent=100 * done / total if total else None)
            with closing(_connect(item.database, write=True)) as connection, connection:
                connection.execute("UPDATE rag_jobs SET phase=?,progress_json=? WHERE job_id=? AND status='running'",
                                   (phase, json.dumps(state), item.job_id))

        try:
            service = RagService(self.layout, self.models(), item.platform_id, item.session_id)
            if row["action"] == "ingest":
                progress("parsing", "documents", 0, 1)
                path = self._input(row["input_name"])
                text = extract_document(path) if request["upload"] else path.read_text(encoding="utf-8")
                result = service.ingest(item.kb_id, text, request["source"], expected_revision=request["expected_revision"], progress=progress)
            else:
                result = service.rebuild(item.kb_id, request["config"], request["expected_revision"], progress=progress)
                result = {"id": result["id"], "revision": result["revision"], "document_count": result["document_count"], "chunk_count": result["chunk_count"]}
            self._finish(item, "succeeded", result=cast(dict[str, Json], result))
        except Exception as error:
            code = "revision_conflict" if isinstance(error, ValueError) and ("修订" in str(error) or "配置已" in str(error)) else "rag_processing_failed"
            message = "知识库修订已变化, 请刷新" if code == "revision_conflict" else f"{phase} 阶段失败 ({type(error).__name__})"
            if code != "revision_conflict":
                from satrap.core.APICall.LLMCall.errors import model_error_info

                info = model_error_info(error)
                if info.code != "internal_error":
                    code, message = info.code, info.message
                elif phase == "parsing" and isinstance(error, ValueError):
                    code, message = "document_parse_failed", "文档解析失败, 请检查格式, 文本编码和文件大小"
            logger.warning(f"[RAG 任务] 处理失败, job={item.job_id}, phase={phase}, type={type(error).__name__}")
            self._finish(item, "failed", JobError(phase=phase, code=code, message=message))

    def _input(self, name: str) -> Path:
        """
        验证内部暂存名称, 不信任归档中的路径

        参数:
        - name: 服务生成的输入文件名

        返回:
        - 私有暂存文件路径
        """
        if not re.fullmatch(re.escape(self.owner_service) + r"-[a-f0-9]{32}\.[A-Za-z0-9]+", name):
            raise ValueError("任务输入名称无效")
        path = self._imports / name
        if path.is_symlink() or path.resolve().parent != self._imports.resolve():
            raise ValueError("任务输入路径无效")
        return path

    def _cleanup_input(self, name: str) -> None:
        """
        清理本服务输入, 文件失败不改写已提交的任务结果

        参数:
        - name: 内部暂存名称
        """
        try:
            self._input(name).unlink(missing_ok=True)
        except (OSError, ValueError) as error:
            logger.warning(f"[RAG 任务] 暂存清理失败, service={self.owner_service}, type={type(error).__name__}")

    def _finish(self, item: _Queued, status: JobStatus, error: JobError | None = None,
                result: dict[str, Json] | None = None) -> None:
        """
        提交最终状态后发布活动, 所有终止路径清理输入文件

        参数:
        - item: 持久任务身份
        - status: 真实终止状态
        - error: 固定错误资料或 None
        - result: 成功发布的摘要或 None
        """
        now = time.time()
        with closing(_connect(item.database, write=True)) as connection, connection:
            row = connection.execute("SELECT * FROM rag_jobs WHERE job_id=?", (item.job_id,)).fetchone()
            if row is None:
                return
            if row["status"] not in {"queued", "running"}:
                return
            encoded = json.dumps(error, ensure_ascii=False) if error else None
            connection.execute("UPDATE rag_jobs SET status=?,phase='finished',finished_at=?,error_json=?,result_json=? WHERE job_id=?",
                               (status, now, encoded, json.dumps(result) if result is not None else None, item.job_id))
            if status == "succeeded":
                documents = cast(int, (result or {}).get("document_count") or 0) if row["action"] == "rebuild" else 1
                connection.execute("UPDATE rag_jobs SET progress_json=? WHERE job_id=?",
                                   (json.dumps(Progress(unit="documents", done=documents, total=documents, percent=100)), item.job_id))
                if row["action"] == "ingest":
                    skipped = result is not None and result.get("status") == "skipped"
                    connection.execute("UPDATE rag_documents SET status='ready',error_json=NULL,updated_at=?,"
                                       "last_indexed_at=CASE WHEN ? THEN last_indexed_at ELSE ? END,"
                                       "chunk_count=CASE WHEN ? THEN chunk_count ELSE ? END WHERE job_id=?",
                                       (now, skipped, now, skipped, cast(int, (result or {}).get("chunks") or 0), item.job_id))
                else:
                    connection.execute("UPDATE rag_documents SET last_indexed_at=?,updated_at=? WHERE kb_id=? AND status='ready'", (now, now, item.kb_id))
            else:
                connection.execute("UPDATE rag_documents SET status='failed',error_json=?,updated_at=? WHERE job_id=?", (encoded, now, item.job_id))
        if row["input_name"]:
            self._cleanup_input(row["input_name"])
        from satrap.core.telemetry.capture import bind_scope, record_activity
        from satrap.core.telemetry.contracts import MetricScope

        with bind_scope(service=self.owner_service):
            record_activity("rag.completed" if status == "succeeded" else f"rag.{status}",
                            "知识库任务完成" if status == "succeeded" else "知识库任务未完成",
                            scope=MetricScope(platform_id=item.platform_id, session_id=item.session_id),
                            tone="info" if status == "succeeded" else "warning", event_key=f"rag:{item.job_id}:{status}",
                            reference={"kind": "rag_job", "job_id": item.job_id, "kb_id": item.kb_id, "platform_id": item.platform_id, "session_id": item.session_id})

    async def close(self) -> None:
        """停止接收并中断未开始项, 等待实际线程结束后释放服务租约"""
        self._closing = True
        await asyncio.gather(*tuple(self._admissions), return_exceptions=True)
        async with self._guard:
            pending = tuple(self._pending)
            self._pending.clear()
        try:
            for item in pending:
                try:
                    await asyncio.to_thread(self._finish, item, "interrupted", JobError(phase="queued", code="service_stopped", message="服务停止, 任务尚未执行"))
                except Exception as error:
                    logger.error(f"[RAG 任务] 停止状态保存失败, job={item.job_id}, type={type(error).__name__}")
                    if item.input_name:
                        await asyncio.to_thread(self._cleanup_input, item.input_name)
            await asyncio.gather(*tuple(self._active.values()), return_exceptions=True)
        finally:
            try:
                await asyncio.to_thread(self._worker.close)
            finally:
                await asyncio.to_thread(self._lease.release)
                self._started = False
