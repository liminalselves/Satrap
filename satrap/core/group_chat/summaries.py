"""
群摘要的有界来源快照与持久出处

直接使用平台唯一数据库冻结本地记录, 区分本地选取完成和平台历史完整;
保存前核验已读出处, 来源失效时清除派生正文, 不调用模型或发送平台消息
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator
import hashlib
import sqlite3
import base64
import json
import uuid

from satrap.core.config.platform_messages import PlatformMessageStore, MessageScope, archive_time
from satrap.core.group_chat.types import GroupChatError


def _digest(item: Mapping[str, Any]) -> str:
    """
    摘要出处的内容身份, 不使用采集时间或当前显示截断

    参数:
    - item: 未经过查询预算截断的档案记录

    返回:
    - 发送者, 时间和正文的稳定摘要
    """
    return hashlib.sha256(json.dumps([item["sender_id"], item["message_time"], item["text"], item["truncated"]],
                                    ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    """
    生成用于持久化与幂等比较的规范 JSON

    参数:
    - value: 可序列化的业务数据

    返回:
    - UTF-8 兼容的稳定字符串
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cursor(values: list[object]) -> str:
    """
    编码不公开存储路径的有限分页位置

    参数:
    - values: 包含范围指纹的分页字段

    返回:
    - URL 安全的 opaque 游标
    """
    return base64.urlsafe_b64encode(_json(values).encode("utf-8")).decode("ascii").rstrip("=")


def _decode(value: object) -> list[Any]:
    """
    校验游标长度并解码, 不接受隐式类型转换

    参数:
    - value: 客户端回传的游标

    返回:
    - 游标字段; 格式损坏抛出 invalid_argument
    """
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 4096:
            raise ValueError
        result = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if not isinstance(result, list):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError) as exc:
        raise GroupChatError("invalid_argument", "摘要分页位置无效, 请重新查询") from exc


def invalidate_summaries(connection: sqlite3.Connection, *, now: float, cutoff: float,
                         scope_key: str | None = None) -> None:
    """
    在档案写事务内清除来源已失效的摘要与快照

    参数:
    - connection: 已迁移且位于调用方事务的连接
    - now: 当前本地 UTC 时间戳
    - cutoff: 生效的档案保留边界
    - scope_key: 可选当前对话, None 表示维护整个平台
    """
    where, args = (" AND scope_key=?", (scope_key,)) if scope_key else ("", ())
    connection.execute("DELETE FROM group_chat_summary_snapshots WHERE expires_at<=?" + where, (now, *args))
    connection.execute(
        "UPDATE group_chat_summaries SET title='', points_json='[]', state='source_unavailable', revision=revision+1 "
        "WHERE state='active'" + where + " AND (expires_at<=? OR EXISTS(SELECT 1 FROM group_chat_summary_refs r "
        "LEFT JOIN platform_messages m ON m.scope_key=r.scope_key AND m.message_id=r.message_id "
        "WHERE r.summary_id=group_chat_summaries.summary_id AND "
        "(m.message_id IS NULL OR m.status<>'active' OR m.message_time<? OR m.verified<>1)))",
        (*args, now, cutoff),
    )
    connection.execute("DELETE FROM group_chat_summary_refs WHERE summary_id IN "
                       "(SELECT summary_id FROM group_chat_summaries WHERE state<>'active')")
    connection.execute("DELETE FROM group_chat_summaries WHERE state<>'active' AND expires_at<?", (now - 30 * 86400,))
    connection.execute(
        "DELETE FROM group_chat_summary_snapshots WHERE EXISTS(SELECT 1 FROM "
        "json_each(json_extract(group_chat_summary_snapshots.payload_json, '$.digests')) d "
        "LEFT JOIN platform_messages m ON m.scope_key=group_chat_summary_snapshots.scope_key AND m.message_id=d.key "
        "WHERE m.message_id IS NULL OR m.status<>'active' OR m.message_time<? OR m.verified<>1)", (cutoff,),
    )
    if scope_key:
        connection.execute("DELETE FROM group_chat_summary_snapshots WHERE scope_key=?", (scope_key,))
        # 档案删除与撤回会撤销本群全部短期快照, 不将旧正文继续交给模型


@dataclass(frozen=True)
class SummaryLimits:
    """摘要选取, 翻页与保留的有限预算"""

    message_limit: int = 500
    text_budget: int = 60000
    page_limit: int = 100
    page_budget: int = 12000
    retention_days: int = 30
    input_budget: int = 1000000

    def __post_init__(self) -> None:
        """拒绝非整数与无界资源预算"""
        for value, lower, upper in ((self.message_limit, 1, 2000), (self.text_budget, 1000, 200000),
                                    (self.page_limit, 1, 100), (self.page_budget, 128, 100000),
                                    (self.retention_days, 1, 3650), (self.input_budget, 1000, 1000000)):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("摘要预算超出允许范围")


class SummaryStore:
    """依附平台档案的摘要存储, 所有方法均由工具或管理边界捕获失败"""

    def __init__(self, archive: PlatformMessageStore) -> None:
        """
        使用宿主已核验的平台存储

        参数:
        - archive: 当前平台档案, 共用路径, 时钟和生效保留期
        """
        self.archive = archive

    @contextmanager
    def _transaction(self, scope: MessageScope) -> Iterator[sqlite3.Connection]:
        """
        开启短写事务, 冷读取不创建没有消息的数据库

        参数:
        - scope: 可信当前对话

        返回:
        - 可回滚的迁移后连接
        """
        self.archive._check_scope(scope)
        connection = self.archive._connect()
        if connection is None:
            raise GroupChatError("not_found", "当前群尚无消息档案")
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    def _maintain(self, connection: sqlite3.Connection) -> None:
        """
        按最后生效档案策略清除已过期的派生正文

        参数:
        - connection: 当前事务连接
        """
        now = self.archive._clock()
        invalidate_summaries(connection, now=now, cutoff=now - self.archive.retention_days * 86400)

    def prepare(self, scope: MessageScope, owner: str, *, start_time: str, end_time: str,
                keyword: str | None = None, include_bot: bool = False,
                limits: SummaryLimits | None = None, allow_new: bool = True) -> dict[str, Any]:
        """
        冻结指定时段的本地记录, 立即返回并登记第一批已读来源

        参数:
        - scope: 可信当前群
        - owner: 有效请求和主工作流的宿主身份
        - start_time: 含时区的起始边界
        - end_time: 含时区的结束边界
        - keyword: 可选普通文本筛选
        - include_bot: 是否纳入本机器人出站消息, 默认 False
        - limits: 可选选取和输出预算
        - allow_new: 宿主全局额度允许新建时为 True, 否则仅复用已有快照

        返回:
        - 快照, 覆盖与第一页; 空档案返回零条, 不生成摘要
        """
        self.archive._check_scope(scope)
        start, end = archive_time(start_time), archive_time(end_time)
        if start is None or end is None or start > end or type(include_bot) is not bool:
            raise ValueError("摘要需要有效的开始和结束时间, 开始不能晚于结束")
        if keyword is not None and (not isinstance(keyword, str) or not 1 <= len(keyword) <= 256):
            raise ValueError("摘要关键词必须是 1 到 256 字符")
        bounds = limits or SummaryLimits()
        fingerprint = hashlib.sha256(_json([scope.key, start, end, keyword, include_bot,
                                           {key: value for key, value in bounds.__dict__.items() if key != "input_budget"}]).encode()).hexdigest()
        if not self.archive.database.exists():
            return {"ok": True, "schema_version": 1, "snapshot_id": None, "items": [], "next_cursor": None,
                    "selection": {"selected_count": 0, "all_local_matches_selected": True, "truncated": False, "reasons": []},
                    "archive_coverage": {"platform_history_complete": False, "retention_days": self.archive.retention_days}}
        with self._transaction(scope) as connection:
            self._maintain(connection)
            cached = connection.execute("SELECT * FROM group_chat_summary_snapshots WHERE scope_key=? AND owner=? AND fingerprint=?",
                                        (scope.key, owner, fingerprint)).fetchone()
            if cached:
                self._snapshot(connection, scope, owner, cached["snapshot_id"])
                return self._page(connection, cached, 0, bounds)
            if not allow_new:
                raise GroupChatError("quota_exceeded", "宿主活动摘要快照过多, 请稍后再试")
            if (connection.execute("SELECT COUNT(*) FROM group_chat_summary_snapshots").fetchone()[0] >= 20
                    or connection.execute("SELECT COUNT(*) FROM group_chat_summary_snapshots WHERE scope_key=?", (scope.key,)).fetchone()[0] >= 5):
                raise GroupChatError("quota_exceeded", "活动摘要快照过多, 请稍后再试")
            now, cutoff = self.archive._clock(), self.archive._clock() - self.archive.retention_days * 86400
            where = "scope_key=? AND status='active' AND verified=1 AND message_time>=? AND message_time>=? AND message_time<=?"
            params: list[object] = [scope.key, cutoff, start, end]
            if not include_bot:
                where += " AND direction='inbound' AND sender_id<>?"
                params.append(scope.self_id)
            if keyword:
                where += " AND instr(text, ?)>0"
                params.append(keyword)
            rows = connection.execute("SELECT * FROM platform_messages WHERE " + where +
                                      " ORDER BY message_time, message_id LIMIT ?", (*params, bounds.message_limit + 1)).fetchall()
            items, hashes, remaining, reasons = [], {}, bounds.text_budget, []
            input_remaining = bounds.input_budget
            for row in rows[:bounds.message_limit]:
                item = self.archive._item(row)
                if remaining == 0 and item["text"]:
                    reasons.append("text_budget")
                    break
                digest = _digest(item)
                if len(item["text"]) > remaining:
                    item["text"] = item["text"][:remaining]
                    item["truncated"] = True
                    reasons.append("text_budget")
                remaining -= len(item["text"])
                if item["truncated"]:
                    reasons.append("source_truncated")
                item["media"] = [{"type": media.get("type", "unknown")} for media in item["media"]]
                size = len(_json(item).encode("utf-8"))
                if size > input_remaining:
                    if not items:
                        raise GroupChatError("quota_exceeded", "当前模型剩余上下文不足以读取摘要来源, 请缩短讨论范围或清理上下文")
                    reasons.append("context_budget")
                    break
                input_remaining -= size
                hashes[item["message_id"]] = digest
                items.append(item)
            if len(rows) > bounds.message_limit:
                reasons.append("message_limit")
            coverage = connection.execute("SELECT MIN(message_time), MAX(message_time) FROM platform_messages "
                                          "WHERE scope_key=? AND status='active' AND message_time>=?", (scope.key, cutoff)).fetchone()
            payload = {"items": items, "digests": hashes,
                       "resolved_range": {"start_time": start_time, "end_time": end_time},
                       "selection": {"selected_count": len(items), "all_local_matches_selected": len(items) == len(rows),
                                     "truncated": bool(reasons), "reasons": sorted(set(reasons))},
                       "archive_coverage": {"platform_history_complete": False, "archived_from": coverage[0],
                                            "archived_to": coverage[1], "retention_days": self.archive.retention_days},
                       "page_limit": bounds.page_limit, "page_budget": bounds.page_budget}
            snapshot_id = "ss_" + uuid.uuid4().hex
            connection.execute("INSERT INTO group_chat_summary_snapshots VALUES(?, ?, ?, ?, ?, 0, ?, ?)",
                               (snapshot_id, scope.key, owner, fingerprint, _json(payload), now, now + 900))
            snapshot = connection.execute("SELECT * FROM group_chat_summary_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            assert snapshot is not None
            return self._page(connection, snapshot, 0, bounds)

    def active_count(self) -> int:
        """
        统计平台未过期快照, 供宿主跨实例限制总资源

        返回:
        - 活动快照数, 冷平台返回零且不创建文件
        """
        connection = self.archive._connect()
        if connection is None:
            return 0
        try:
            return int(connection.execute("SELECT COUNT(*) FROM group_chat_summary_snapshots WHERE expires_at>?", (self.archive._clock(),)).fetchone()[0])
        finally:
            connection.close()

    def _page(self, connection: sqlite3.Connection, snapshot: sqlite3.Row, offset: int,
              limits: SummaryLimits) -> dict[str, Any]:
        """
        返回连续来源页, 截断正文仍标记而不掩盖预算

        参数:
        - connection: 当前事务连接
        - snapshot: 当前请求可读的快照
        - offset: 已证明连续读取的起点
        - limits: 当前输出预算, 不能扩大冻结时的预算

        返回:
        - 页内容, 下一游标与原快照覆盖信息
        """
        payload = json.loads(snapshot["payload_json"])
        if type(offset) is not int or offset < 0 or offset > snapshot["read_until"] or offset > len(payload["items"]):
            raise GroupChatError("invalid_argument", "不能跳过尚未读取的摘要来源")
        items, budget = [], min(limits.page_budget, payload["page_budget"])
        for original in payload["items"][offset:offset + min(limits.page_limit, payload["page_limit"])]:
            if items and len(original["text"]) > budget:
                break
            item = dict(original)
            if len(item["text"]) > budget:
                item["text"], item["truncated"] = item["text"][:budget], True
                payload["selection"]["truncated"] = True
                payload["selection"]["reasons"] = sorted(set(payload["selection"]["reasons"] + ["page_budget"]))
                # 极长单条消息作为明确不完整出处, 不能声称其全文已读取
            budget -= len(item["text"])
            items.append(item)
        until = offset + len(items)
        connection.execute("UPDATE group_chat_summary_snapshots SET read_until=MAX(read_until, ?), payload_json=? WHERE snapshot_id=?",
                           (until, _json(payload), snapshot["snapshot_id"]))
        return {"ok": True, "schema_version": 1, "snapshot_id": snapshot["snapshot_id"], "items": items,
                **{key: payload[key] for key in ("resolved_range", "selection", "archive_coverage")},
                "next_cursor": _cursor([1, snapshot["snapshot_id"], until]) if until < len(payload["items"]) else None,
                "expires_at": datetime.fromtimestamp(snapshot["expires_at"], timezone.utc).isoformat()}

    def read_sources(self, scope: MessageScope, owner: str, snapshot_id: str, cursor: str,
                     *, limits: SummaryLimits | None = None) -> dict[str, Any]:
        """
        读取当前主工作流的来源快照, 不允许跨请求或伪造跳页

        参数:
        - scope: 可信当前群
        - owner: 当前请求身份
        - snapshot_id: 准备工具返回的快照 ID
        - cursor: 上一页返回的游标
        - limits: 当前翻页预算

        返回:
        - 后续来源页, 来源失效时返回明确错误
        """
        parts = _decode(cursor)
        if len(parts) != 3 or parts[:2] != [1, snapshot_id]:
            raise GroupChatError("invalid_argument", "游标不属于当前摘要快照")
        with self._transaction(scope) as connection:
            self._maintain(connection)
            snapshot = self._snapshot(connection, scope, owner, snapshot_id)
            return self._page(connection, snapshot, parts[2], limits or SummaryLimits())

    def _snapshot(self, connection: sqlite3.Connection, scope: MessageScope, owner: str, snapshot_id: str) -> sqlite3.Row:
        """
        取得当前来源仍可使用的快照

        参数:
        - connection: 当前事务
        - scope: 当前群
        - owner: 当前请求身份
        - snapshot_id: 快照 ID

        返回:
        - 仍有效且内容未变化的快照
        """
        snapshot = connection.execute("SELECT * FROM group_chat_summary_snapshots WHERE snapshot_id=? AND scope_key=? AND owner=?",
                                      (snapshot_id, scope.key, owner)).fetchone()
        if snapshot is None:
            raise GroupChatError("snapshot_expired", "摘要快照已过期或不属于当前请求, 请重新读取")
        payload = json.loads(snapshot["payload_json"])
        cutoff = self.archive._clock() - self.archive.retention_days * 86400
        for message_id, digest in payload["digests"].items():
            row = connection.execute("SELECT * FROM platform_messages WHERE scope_key=? AND message_id=?", (scope.key, message_id)).fetchone()
            if row is None or row["status"] != "active" or row["message_time"] < cutoff or _digest(self.archive._item(row)) != digest:
                raise GroupChatError("source_unavailable", "摘要出处已变化或不可用, 请重新读取")
        return snapshot

    def save(self, scope: MessageScope, owner: str, snapshot_id: str, title: str, points: object,
             *, limits: SummaryLimits | None = None) -> dict[str, Any]:
        """
        在同一事务核验出处与幂等保存, 不要求平台发送成功

        参数:
        - scope: 当前群
        - owner: 当前主工作流请求身份
        - snapshot_id: 已读来源快照
        - title: 1 到 120 字符的摘要标题
        - points: 每条含正文和出处 ID 的列表
        - limits: 保存期限预算

        返回:
        - 已提交摘要, 同一快照同一内容重放返回相同记录
        """
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
            raise ValueError("摘要标题必须是 1 到 120 字符")
        if not isinstance(points, list) or not 1 <= len(points) <= 20:
            raise ValueError("摘要必须有 1 到 20 条结论")
        refs, text_size = set(), 0
        for point in points:
            if (not isinstance(point, dict) or set(point) != {"text", "source_message_ids"}
                    or not isinstance(point["text"], str) or not 1 <= len(point["text"]) <= 2000
                    or not isinstance(point["source_message_ids"], list) or not 1 <= len(point["source_message_ids"]) <= 10
                    or any(not isinstance(value, str) or not 1 <= len(value) <= 256 for value in point["source_message_ids"])):
                raise ValueError("每条摘要需要正文和 1 到 10 个来源消息 ID")
            refs.update(point["source_message_ids"])
            text_size += len(point["text"])
        if text_size > 12000:
            raise ValueError("摘要总正文超过 12000 字符")
        operation = hashlib.sha256(_json([scope.key, owner, snapshot_id, title, points]).encode()).hexdigest()
        with self._transaction(scope) as connection:
            self._maintain(connection)
            previous = connection.execute("SELECT * FROM group_chat_summaries WHERE operation_key=? AND scope_key=?", (operation, scope.key)).fetchone()
            if previous and previous["state"] != "active":
                raise GroupChatError("source_unavailable", "这份摘要已删除或来源失效, 不重放旧保存结果")
            if previous:
                return {"ok": True, "status": "saved", "summary": self._summary(previous), "replayed": True}
            snapshot = self._snapshot(connection, scope, owner, snapshot_id)
            payload = json.loads(snapshot["payload_json"])
            if snapshot["read_until"] < len(payload["items"]):
                raise GroupChatError("sources_not_read", "请先读取摘要快照的全部分页")
            if not refs.issubset(payload["digests"]):
                raise GroupChatError("source_unavailable", "摘要引用了本次快照之外的消息")
            now, bounds = self.archive._clock(), limits or SummaryLimits()
            expires = min(now + bounds.retention_days * 86400,
                          min(item["message_time"] + self.archive.retention_days * 86400
                              for item in payload["items"] if item["message_id"] in refs))
            summary_id = "gs_" + uuid.uuid4().hex
            metadata = {key: payload[key] for key in ("resolved_range", "selection", "archive_coverage")}
            connection.execute("INSERT INTO group_chat_summaries(summary_id, scope_key, title, points_json, metadata_json, operation_key, created_at, expires_at) "
                               "VALUES(?, ?, ?, ?, ?, ?, ?, ?)", (summary_id, scope.key, title.strip(), _json(points), _json(metadata), operation, now, expires))
            connection.executemany("INSERT INTO group_chat_summary_refs VALUES(?, ?, ?, ?)",
                                   ((summary_id, scope.key, message_id, payload["digests"][message_id]) for message_id in sorted(refs)))
            row = connection.execute("SELECT * FROM group_chat_summaries WHERE summary_id=?", (summary_id,)).fetchone()
            assert row is not None
            return {"ok": True, "status": "saved", "summary": self._summary(row), "replayed": False}

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict[str, Any]:
        """
        返回不暴露内部幂等键的摘要对象

        参数:
        - row: 当前可读的摘要记录

        返回:
        - 带状态, 修订和出处的公开对象
        """
        return {"schema_version": 1, **{key: row[key] for key in ("summary_id", "title", "revision", "state", "created_at", "expires_at")},
                "points": json.loads(row["points_json"]), **json.loads(row["metadata_json"])}

    def get(self, scope: MessageScope, summary_id: str) -> dict[str, Any]:
        """
        查看摘要, 对来源失效记录仅返回清除正文后的状态

        参数:
        - scope: 已授权当前群
        - summary_id: 摘要 ID

        返回:
        - 摘要详情, 其它范围或不存在时抛出 not_found
        """
        with self._transaction(scope) as connection:
            self._maintain(connection)
            row = connection.execute("SELECT * FROM group_chat_summaries WHERE scope_key=? AND summary_id=?", (scope.key, summary_id)).fetchone()
            if row is None or row["state"] == "deleted":
                raise GroupChatError("not_found", "摘要不存在或不属于当前群")
            return {"ok": True, "summary": self._summary(row)}

    def list(self, scope: MessageScope, *, keyword: str = "", limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        """
        按固定时间顺序查询当前群摘要, 游标绑定范围和筛选

        参数:
        - scope: 已授权当前群
        - keyword: 可选普通文本关键词
        - limit: 1 到 100 条
        - cursor: 可选上一页位置

        返回:
        - 有界摘要列表, 空档案不创建文件
        """
        if type(limit) is not int or not 1 <= limit <= 100 or not isinstance(keyword, str) or len(keyword) > 256:
            raise ValueError("摘要列表参数无效")
        fingerprint = hashlib.sha256(_json([scope.key, keyword]).encode()).hexdigest()
        anchor = None
        if cursor is not None:
            parts = _decode(cursor)
            if len(parts) != 4 or parts[:2] != [1, fingerprint] or type(parts[2]) not in {int, float} or not isinstance(parts[3], str):
                raise ValueError("摘要列表游标不属于当前群和筛选条件")
            anchor = parts[2:]
        self.archive._check_scope(scope)
        if not self.archive.database.exists():
            return {"ok": True, "items": [], "has_more": False, "next_cursor": None}
        with self._transaction(scope) as connection:
            self._maintain(connection)
            where, args = "scope_key=? AND state<>'deleted'", [scope.key]
            if keyword:
                where += " AND (instr(title, ?)>0 OR instr(points_json, ?)>0)"
                args.extend([keyword, keyword])
            if anchor:
                where += " AND (created_at<? OR (created_at=? AND summary_id<?))"
                args.extend([anchor[0], anchor[0], anchor[1]])
            rows = connection.execute("SELECT * FROM group_chat_summaries WHERE " + where + " ORDER BY created_at DESC, summary_id DESC LIMIT ?",
                                      (*args, limit + 1)).fetchall()
            page, more = rows[:limit], len(rows) > limit
            return {"ok": True, "items": [self._summary(row) for row in page], "has_more": more,
                    "next_cursor": _cursor([1, fingerprint, page[-1]["created_at"], page[-1]["summary_id"]]) if more else None}

    def delete(self, scope: MessageScope, summary_id: str, expected_revision: int,
               idempotency_key: str | None = None) -> dict[str, Any]:
        """
        管理接口删除派生摘要, 不修改档案或平台消息

        参数:
        - scope: 已授权当前群
        - summary_id: 摘要 ID
        - expected_revision: 已读取的修订
        - idempotency_key: 管理界面一次删除意图的可选幂等键

        返回:
        - 删除成功结果, 修订冲突时不覆盖
        """
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("删除摘要需要有效修订")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128):
            raise ValueError("删除幂等键无效")
        with self._transaction(scope) as connection:
            self._maintain(connection)
            row = connection.execute("SELECT * FROM group_chat_summaries WHERE scope_key=? AND summary_id=?", (scope.key, summary_id)).fetchone()
            if row is None:
                raise GroupChatError("not_found", "摘要不存在")
            if row["state"] == "deleted":
                if row["revision"] == expected_revision + 1 and json.loads(row["metadata_json"]).get("delete_key") == idempotency_key:
                    return {"ok": True, "status": "deleted", "summary_id": summary_id, "replayed": True}
                raise GroupChatError("not_found", "摘要已被删除")
            if row["revision"] != expected_revision:
                raise GroupChatError("revision_conflict", "摘要状态已变化, 请刷新后删除")
            connection.execute("UPDATE group_chat_summaries SET state='deleted', title='', points_json='[]', metadata_json=?, revision=revision+1 "
                               "WHERE scope_key=? AND summary_id=?", (_json({"delete_key": idempotency_key}), scope.key, summary_id))
            connection.execute("DELETE FROM group_chat_summary_refs WHERE summary_id=?", (summary_id,))
            return {"ok": True, "status": "deleted", "summary_id": summary_id}
