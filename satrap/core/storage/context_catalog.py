"""
上下文归属和活动时间目录

随消息写入记录时间, 由会话框架登记主上下文和工作流归属,
纯内存上下文跳过全部存储操作, 浏览目录不会创建表
"""
from __future__ import annotations

from contextlib import closing
from typing import Any
from pathlib import Path
import traceback
import sqlite3
import time

from satrap.core.log import logger


CATALOG_SCHEMA = """CREATE TABLE IF NOT EXISTS context_catalog (
    context_id TEXT PRIMARY KEY, session_id TEXT, context_kind TEXT NOT NULL DEFAULT 'standalone',
    workflow_name TEXT, source_kind TEXT NOT NULL DEFAULT 'unknown',
    created_at REAL, last_message_at REAL, modified_at REAL
)"""

CATALOG_WRITE = """INSERT INTO context_catalog
    (context_id, session_id, context_kind, workflow_name, source_kind, created_at, last_message_at, modified_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(context_id) DO UPDATE SET
    session_id=COALESCE(excluded.session_id, context_catalog.session_id),
    context_kind=CASE WHEN excluded.session_id IS NULL THEN context_catalog.context_kind ELSE excluded.context_kind END,
    workflow_name=COALESCE(excluded.workflow_name, context_catalog.workflow_name),
    source_kind=CASE WHEN excluded.source_kind='unknown' THEN context_catalog.source_kind ELSE excluded.source_kind END,
    created_at=COALESCE(context_catalog.created_at, excluded.created_at),
    last_message_at=COALESCE(excluded.last_message_at, context_catalog.last_message_at),
    modified_at=COALESCE(excluded.modified_at, context_catalog.modified_at)
"""


def catalog_values(context: Any, *, appended: bool = False, modified: bool = False) -> tuple[object, ...]:
    """
    生成目录写入参数, 旧消息首次加载不伪造创建和消息时间

    参数:
    - context: 持久化上下文管理器
    - appended: 是否正在保存新追加消息
    - modified: 是否正在修改已有消息

    返回:
    - 目录写入参数, 不能从旧数据确定的时间使用 None
    """
    now = time.time()
    return (
        context.conversation_id, getattr(context, "catalog_session_id", None),
        getattr(context, "catalog_kind", "standalone"), getattr(context, "catalog_workflow", None),
        getattr(context, "catalog_source", "unknown"),
        now if appended and context._saved_count == 0 else None,
        now if appended else None, now if modified else None,
    )


def bind_context(context: Any, session_id: str, kind: str, workflow_name: str | None = None, source: str = "unknown") -> None:
    """
    登记精确的会话归属, 初始化前的异步上下文延迟到消息写入

    参数:
    - context: 上下文实例, 测试替身和纯内存实例不访问数据库
    - session_id: 所属会话 ID
    - kind: 主上下文或工作流类别
    - workflow_name: 工作流名称, 主上下文使用 None
    - source: 来源标识, 默认 unknown
    """
    if not getattr(context, "persistent", False):
        return
    context.catalog_session_id = session_id
    context.catalog_kind = kind
    context.catalog_workflow = workflow_name
    context.catalog_source = source
    database = Path(context.db_path)
    if not database.is_file():
        return
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)) as connection, connection:
            connection.execute(CATALOG_SCHEMA)
            connection.execute(CATALOG_WRITE, catalog_values(context))
    except Exception as error:
        logger.error(f"[上下文目录] 登记归属失败: {session_id}/{context.conversation_id}, {error}\n{traceback.format_exc()}")
