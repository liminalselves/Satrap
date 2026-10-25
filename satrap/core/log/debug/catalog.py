"""分页读取已有对象归属, 允许在尚无高级日志时开启采集"""
from __future__ import annotations

from contextlib import closing
import base64
import json
import sqlite3

from satrap.core.storage.layout import StorageLayout
from .contracts import DebugObject


def available_contexts(layout: StorageLayout, platform_id: str = "chat", *, cursor: str = "", limit: int = 50) -> dict[str, object]:
    """
    读取冷存储的真实路由页, 不创建表或恢复 Session

    参数:
    - layout: Control 当前配置对应的数据布局
    - platform_id: 已登记平台, 默认 Chat
    - cursor: 上页的复合键游标
    - limit: 1 至 100 条

    返回:
    - 可确认归属的上下文页, 平台选项和未确认数量; 旧式猜测归属不提供开关
    """
    if type(limit) is not int or not 1 <= limit <= 100 or len(cursor) > 16384:
        raise ValueError("已有上下文分页参数无效")
    platforms = {"chat"}
    for path in layout.platforms_root.glob("*/platform.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            identity = value.get("platform_id")
            if isinstance(identity, str) and identity and identity != "local":
                platforms.add(identity)
        except (OSError, ValueError, AttributeError):
            continue
    if platform_id not in platforms:
        raise ValueError("平台未登记或不支持对象级 Debug")
    after_key, after_session = "", ""
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor))
            if not isinstance(value, list) or len(value) != 3 or value[0] != platform_id or any(not isinstance(item, str) for item in value):
                raise ValueError("游标不属于当前平台")
            _, after_key, after_session = value
        except (ValueError, UnicodeError) as error:
            raise ValueError("已有上下文游标无效") from error
    result: dict[str, object] = {"contexts": [], "has_more": False, "next_cursor": None, "platforms": sorted(platforms), "unconfirmed": 0}
    path = layout.platform_db(platform_id)
    if not path.is_file():
        return result
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if platform_id == "chat" and "conversation_meta" in tables:
            rows = db.execute("SELECT conversation_id context_key,conversation_id session_id,created_at updated_at FROM conversation_meta WHERE conversation_id>? ORDER BY conversation_id LIMIT ?", (after_key, limit+1)).fetchall()
        else:
            sources = [table for table in ("context_sessions", "context_session_history") if table in tables]
            if not sources:
                return result
            sql = " UNION ALL ".join(f"SELECT context_key,session_id,last_used_at updated_at FROM {table} WHERE (context_key,session_id)>(?,?)" for table in sources)
            rows = db.execute(sql + " ORDER BY context_key,session_id LIMIT ?", (*[item for _ in sources for item in (after_key, after_session)], limit+1)).fetchall()
        items: list[dict[str, object]] = []
        skipped = 0
        seen: set[tuple[str, str]] = set()
        for row in rows[:limit]:
            try:
                if platform_id == "chat":
                    obj = DebugObject(platform_id, "local", "chat", row["session_id"])
                else:
                    route_key = row["context_key"]
                    if not isinstance(route_key, str) or not route_key.startswith(("scoped:v1:", "scoped:v2:")):
                        raise ValueError("旧归属无法确认")
                    route = json.loads(route_key.split(":", 2)[2])
                    if not isinstance(route, list) or len(route) not in (7, 8, 11) or route[0] != platform_id:
                        raise ValueError("路由字段不匹配")
                    if route_key.startswith("scoped:v2:") and len(route) == 11:
                        kind, target = route[8], route[9]
                    elif route[3] in ("group", "group_member"):
                        kind, target = "group", route[5]
                    else:
                        raise ValueError("旧私聊缺少完整账号身份")
                    obj = DebugObject(platform_id, route[4], kind, target)
                identity = obj.key, row["session_id"]
                if identity in seen:
                    continue
                seen.add(identity)
                items.append({"object_key": obj.key, "object": obj.payload(), "context_id": row["session_id"], "title": row["session_id"],
                              "service": "chat" if platform_id == "chat" else "backend", "updated_at": row["updated_at"], "request_count": 0, "error_count": 0})
            except (ValueError, TypeError, IndexError):
                skipped += 1
        more = len(rows) > limit
        last = rows[limit-1] if more else None
        result.update(contexts=items, has_more=more, unconfirmed=skipped,
                      next_cursor=base64.urlsafe_b64encode(json.dumps([platform_id, last["context_key"], last["session_id"]]).encode()).decode() if last else None)
    return result
