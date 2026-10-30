"""会话实例的只读路由与展示标签查询"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any, TypedDict
import math
import sqlite3

from satrap.core.config.conversation_catalog import route_metadata
from satrap.core.log import logger
from satrap.core.platform import registry


class SessionRoute(TypedDict):
    """会话实例的实际路由身份, 不持久化展示资料"""

    scope: str
    self_id: str
    group_id: str
    user_id: str
    conversation_kind: str
    conversation_id: str
    target_label: str | None
    kind_label: str | None
    current: bool


def _route_order(row: dict[str, Any]) -> tuple[float, float, str]:
    """按有效活动时间和稳定路由键选取同类引用, 损坏时间按未知处理"""
    def timestamp(value: object) -> float:
        try:
            result = float(value) if isinstance(value, (int, float, str)) else 0.0
            return result if math.isfinite(result) else 0.0
        except ValueError:
            return 0.0

    created = timestamp(row.get("created_at"))
    return timestamp(row.get("last_used_at")) or created, created, str(row.get("context_key") or "")


def read_session_routes(
    database: str | Path,
    platform: Mapping[str, object] | None,
    session_ids: Sequence[str],
) -> dict[str, SessionRoute | None]:
    """
    批量读取实例的优先路由, 在同一只读快照内补充适配器标签

    参数:
    - database: 平台数据库路径, 不创建缺失的数据库或表
    - platform: 含 type 的平台配置, 缺失时只返回通用身份
    - session_ids: 本次列表中的实例 ID, 去重后每批最多 500 个

    返回:
    - 实例 ID 到路由的映射, 无引用时为 None; 当前绑定始终优先于历史
    """
    identities = list(dict.fromkeys(session_ids))
    result: dict[str, SessionRoute | None] = dict.fromkeys(identities)
    path = Path(database)
    if not identities or not path.is_file():
        return result
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")   # 两类绑定和标签使用同一数据库快照
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        selected: dict[str, dict[str, Any]] = {}

        def select(statement: str, requested: list[str]) -> None:
            for offset in range(0, len(requested), 500):
                batch = requested[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                for item in connection.execute(statement.format(placeholders=placeholders), batch):
                    row = dict(item)
                    identity = str(row["session_id"])
                    if identity not in selected or _route_order(row) > _route_order(selected[identity]):
                        selected[identity] = row

        if "context_sessions" in tables:
            select("SELECT * FROM context_sessions WHERE session_id IN ({placeholders})", identities)
        current = set(selected)
        missing = [identity for identity in identities if identity not in current]
        if missing and "context_session_history" in tables:
            statement = "SELECT h.context_key, h.session_id, h.first_used_at AS created_at, h.last_used_at"
            join = ""
            if "context_sessions" in tables:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(context_sessions)")}
                statement += "".join(", " + (f"c.{name}" if name in columns else f"'' AS {name}")
                                     for name in ("user_id", "platform", "provider_name", "session_type"))
                join = " LEFT JOIN context_sessions c ON c.context_key=h.context_key"
            select(statement + " FROM context_session_history h" + join + " WHERE h.session_id IN ({placeholders})", missing)
        routes = [route_metadata(row) for row in selected.values()]
        adapter = registry.get(str((platform or {}).get("type") or ""))
        labels: list[dict[str, str]] = [{} for _ in routes]
        if adapter and routes:
            try:
                labels = adapter.conversation_catalog_metadata_batch(connection, routes)
                if not isinstance(labels, list) or len(labels) != len(routes):
                    raise ValueError("适配器批量标签必须与路由一一对应")
            except Exception as error:
                labels = [{} for _ in routes]
                logger.error(f"[会话实例] 适配器标签读取失败: {error}")
        for route, extra in zip(routes, labels):
            identity = route["session_id"]
            if not isinstance(extra, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in extra.items()):
                logger.warning(f"[会话实例] 适配器标签格式无效: {identity}")
                extra = {}
            kind = route.get("conversation_kind", "")
            result[identity] = SessionRoute(
                scope=route["scope"], self_id=route.get("self_id", ""), group_id=route.get("group_id", ""),
                user_id=route.get("user_id", ""), conversation_kind=kind, conversation_id=route.get("conversation_id", ""),
                target_label=extra.get("target") or None,
                kind_label=adapter.conversation_kinds.get(kind) if adapter else None, current=identity in current,
            )
    return result
