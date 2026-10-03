"""平台原始消息档案; 与模型上下文分开保存, 查询和删除"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
import base64
import hashlib
import json
import math
import sqlite3
import time
import uuid

from satrap.core.config.platform_schema import PLATFORM_SCHEMA_VERSION, ensure_platform_tables


class MessageArchiveError(RuntimeError):
    """携带公共错误码的档案错误, 由请求或消息接收边界记录和捕获"""

    def __init__(self, code: str, message: str) -> None:
        """
        初始化档案错误

        参数:
        - code: 对外稳定的错误码
        - message: 不包含原始消息正文的错误说明
        """
        super().__init__(message)
        self.code = code


def _identity(value: object) -> str:
    """
    校验平台身份标识

    参数:
    - value: 不依赖平台数字格式的字符串标识

    返回:
    - 原字符串, 非法输入抛出 ValueError
    """
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("消息档案身份必须是长度不超过 256 的非空字符串")
    return value


@dataclass(frozen=True)
class MessageScope:
    """平台实例, 账号和实际对话组成的档案身份, 不包含 Agent 配置"""

    adapter_id: str
    self_id: str
    conversation_kind: str
    chat_id: str

    def __post_init__(self) -> None:
        """校验全部身份字段, 不接受空账号或未知对话身份"""
        for value in (self.adapter_id, self.self_id, self.conversation_kind, self.chat_id):
            _identity(value)

    @property
    def key(self) -> str:
        """
        编码不受分隔符冲突影响的版本化对话键

        返回:
        - 包含全部档案身份的 JSON 对话键
        """
        return "messages:v1:" + json.dumps(
            [self.adapter_id, self.self_id, self.conversation_kind, self.chat_id], ensure_ascii=False, separators=(",", ":"),
        )


@dataclass(frozen=True)
class ArchiveMessage:
    """已经由适配器归一和核验归属的消息; 不持有原始事件或本地文件内容"""

    message_id: str
    sender_id: str
    message_time: float
    text: str
    nickname: str = ""
    card: str = ""
    direction: str = "inbound"
    components: Sequence[Mapping[str, object]] = field(default_factory=tuple)
    reply_to_message_id: str | None = None
    mentions: Sequence[str] = field(default_factory=tuple)
    media: Sequence[Mapping[str, object]] = field(default_factory=tuple)
    source: str = "platform_event"
    verified: bool = True
    truncated: bool = False
    time_source: str = "platform"


def archive_time(value: str | None) -> float | None:
    """
    将含时区的 ISO 8601 查询时间统一转换为 UTC 时间戳

    参数:
    - value: 含时区的时间字符串, None 表示没有该范围限制

    返回:
    - UTC 时间戳或 None, 缺少时区和非法时间抛出 ValueError
    """
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("查询时间必须是含时区的 ISO 8601 字符串")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("查询时间必须包含时区")
    return parsed.timestamp()


class PlatformMessageStore:
    """平台数据库中的消息档案, 无档案时读取不创建数据库"""

    def __init__(self, database: str | Path, adapter_id: str, *, retention_days: int = 30,
                 clock: Callable[[], float] = time.time) -> None:
        """
        绑定已有平台数据库和保留策略

        参数:
        - database: 由宿主解析的平台数据库路径
        - adapter_id: 数据库归属的平台实例 ID
        - retention_days: 正文和删除备份的保留天数, 1 到 3650
        - clock: 用于保留期和删除时间的本地时钟
        """
        _identity(adapter_id)
        if type(retention_days) is not int or not 1 <= retention_days <= 3650:
            raise ValueError("消息档案保留期必须是 1 到 3650 天")
        self.database = Path(database).absolute()
        self.adapter_id = adapter_id
        self.retention_days = retention_days
        self._clock = clock

    def _check_scope(self, scope: MessageScope) -> None:
        """
        拒绝将另一平台的身份放入当前平台数据库

        参数:
        - scope: 由可信宿主或已授权管理接口解析的身份
        """
        if scope.adapter_id != self.adapter_id:
            raise MessageArchiveError("wrong_conversation", "消息档案不属于当前平台实例")

    def _connect(self, *, create: bool = False) -> sqlite3.Connection | None:
        """
        打开或迁移档案表, 未采集过消息的读取不创建文件

        参数:
        - create: 是否允许为首次写入创建数据库和目录

        返回:
        - 平台连接, 无数据库或旧版尚无档案时返回 None
        """
        if not self.database.exists() and not create:
            return None
        if create:
            self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database.as_uri() + ("?mode=rwc" if create else "?mode=rw"), uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            if create:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    ensure_platform_tables(connection)
            else:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version < 4:
                    connection.close()
                    return None
                if version >= PLATFORM_SCHEMA_VERSION:
                    ensure_platform_tables(connection)
                else:
                    with connection:
                        connection.execute("BEGIN IMMEDIATE")
                        ensure_platform_tables(connection)
            return connection
        except Exception:
            connection.close()
            raise

    def _save_retention(self, connection: sqlite3.Connection) -> None:
        """
        保留最后生效的档案保留期, 平台移除后仍可按原策略清理

        参数:
        - connection: 当前档案写事务连接
        """
        connection.execute(
            "INSERT INTO platform_message_policy(adapter_id, retention_days) VALUES(?, ?) "
            "ON CONFLICT(adapter_id) DO UPDATE SET retention_days=excluded.retention_days",
            (self.adapter_id, self.retention_days),
        )

    def saved_retention_days(self) -> int | None:
        """
        读取最后生效的保留策略, 没有消息档案时不创建数据库

        返回:
        - 已保存的保留天数, 未保存策略时返回 None
        """
        connection = self._connect()
        if connection is None:
            return None
        with closing(connection):
            row = connection.execute(
                "SELECT retention_days FROM platform_message_policy WHERE adapter_id=?", (self.adapter_id,),
            ).fetchone()
            return int(row[0]) if row else None

    def _chat(self, connection: sqlite3.Connection, scope: MessageScope, label: str = "") -> sqlite3.Row:
        """
        在写事务中确保对话身份存在

        参数:
        - connection: 当前写事务连接
        - scope: 档案归属身份
        - label: 已确认的群名称或对话名称快照

        返回:
        - 当前对话状态行
        """
        self._save_retention(connection)
        connection.execute(
            "INSERT INTO platform_message_chats(scope_key, adapter_id, self_id, conversation_kind, chat_id, label) "
            "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(scope_key) DO UPDATE SET "
            "label=CASE WHEN excluded.label<>'' THEN excluded.label ELSE label END",
            (scope.key, scope.adapter_id, scope.self_id, scope.conversation_kind, scope.chat_id, label[:512]),
        )
        row = connection.execute("SELECT * FROM platform_message_chats WHERE scope_key=?", (scope.key,)).fetchone()
        assert row is not None
        return row

    @staticmethod
    def _message_values(message: ArchiveMessage) -> tuple[object, ...]:
        """
        校验已归一消息并限制正文和组件大小

        参数:
        - message: 已核验的入站或确认出站消息

        返回:
        - 对应数据库列的参数元组
        """
        _identity(message.message_id)
        _identity(message.sender_id)
        if (message.direction not in {"inbound", "outbound"} or message.verified is not True
                or message.time_source not in {"platform", "local"}
                or isinstance(message.message_time, bool) or not math.isfinite(message.message_time)
                or message.message_time < 0 or not isinstance(message.text, str)):
            raise ValueError("消息档案只接受有有效时间和归属依据的消息")
        if message.reply_to_message_id is not None:
            _identity(message.reply_to_message_id)
        mentions = list(dict.fromkeys(_identity(item) for item in message.mentions))
        if len(mentions) > 256:
            raise ValueError("消息提及成员数量超过档案限制")
        components = json.dumps(list(message.components[:128]), ensure_ascii=False)
        media = json.dumps(list(message.media[:128]), ensure_ascii=False)
        if len(components) + len(media) > 131072:
            raise ValueError("消息组件摘要超过档案大小限制")
        truncated = message.truncated or len(message.text) > 64000 or len(message.components) > 128 or len(message.media) > 128
        return (message.message_id, message.sender_id, message.nickname[:512], message.card[:512],
                message.message_time, message.direction, message.text[:64000], components, message.reply_to_message_id,
                json.dumps(mentions, ensure_ascii=False), media, _identity(message.source), int(truncated), message.time_source)

    def record(self, scope: MessageScope, message: ArchiveMessage, *, label: str = "") -> bool:
        """
        采集消息并合并重复上报, 不覆盖删除或撤回标记

        参数:
        - scope: 当前已准入对话身份
        - message: 已归一并核验的真实消息
        - label: 采集时可用的对话名称

        返回:
        - 新消息入档返回 True, 重复或被删除范围阻止时返回 False
        """
        self._check_scope(scope)
        values = self._message_values(message)
        connection = self._connect(create=True)
        assert connection is not None
        with closing(connection), connection:
            connection.execute("BEGIN IMMEDIATE")
            chat = self._chat(connection, scope, label)
            if chat["delete_before"] is not None and message.message_time <= chat["delete_before"]:
                return False
            now = self._clock()
            result = connection.execute(
                "INSERT INTO platform_messages(scope_key, message_id, sender_id, nickname, card, message_time, received_at, "
                "direction, text, components_json, reply_to_message_id, mentions_json, media_json, source, truncated, time_source) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(scope_key, message_id) DO NOTHING",
                (scope.key, *values[:5], now, *values[5:]),
            )
            if result.rowcount == 0:
                previous = connection.execute(
                    "SELECT sender_id, status FROM platform_messages WHERE scope_key=? AND message_id=?",
                    (scope.key, message.message_id),
                ).fetchone()
                if previous is not None and previous["status"] == "active" and previous["sender_id"] != message.sender_id:
                    raise MessageArchiveError("unverified_target", "重复消息的发送者与已核验记录不一致")
                if message.direction == "outbound":
                    connection.execute(
                        "UPDATE platform_messages SET direction='outbound' WHERE scope_key=? AND message_id=? AND status='active'",
                        (scope.key, message.message_id),
                    )
                return False
            connection.execute(
                "UPDATE platform_message_chats SET captured_from=MIN(COALESCE(captured_from, ?), ?), "
                "captured_to=MAX(COALESCE(captured_to, ?), ?) WHERE scope_key=?",
                (message.message_time, message.message_time, message.message_time, message.message_time, scope.key),
            )
            if message.message_time < now - self.retention_days * 86400:
                self._erase(connection, scope.key, [message.message_id], "expired", "retention")
            return True

    def get(self, scope: MessageScope, message_id: str) -> dict[str, Any] | None:
        """
        读取一条消息, 返回删除状态供回源服务防止自动补回

        参数:
        - scope: 当前对话身份
        - message_id: 当前对话内的平台消息 ID

        返回:
        - 消息及状态, 未采集时返回 None
        """
        self._check_scope(scope)
        _identity(message_id)
        connection = self._connect()
        if connection is None:
            return None
        with closing(connection):
            row = connection.execute(
                "SELECT * FROM platform_messages WHERE scope_key=? AND message_id=?", (scope.key, message_id),
            ).fetchone()
            if row is None:
                return None
            return self._item(row)

    def _item(self, row: sqlite3.Row) -> dict[str, Any]:
        """
        将消息行转换为公共结果, 不公开删除备份和内部身份键

        参数:
        - row: 已限定当前对话的消息行

        返回:
        - 消息身份, 状态及允许检索的内容
        """
        status = str(row["status"])
        if status == "active" and row["message_time"] < self._clock() - self.retention_days * 86400:
            status = "expired"
        active = status == "active"
        return {"message_id": row["message_id"], "sender_id": row["sender_id"] if active else "",
                "nickname": row["nickname"] if active else "", "card": row["card"] if active else "",
                "message_time": row["message_time"], "time_source": row["time_source"],
                "received_at": row["received_at"], "direction": row["direction"],
                "text": row["text"] if active else "", "components": json.loads(row["components_json"]) if active else [],
                "reply_to_message_id": row["reply_to_message_id"] if active else None,
                "mentions": json.loads(row["mentions_json"]) if active else [],
                "media": json.loads(row["media_json"]) if active else [], "status": status,
                "source": row["source"], "verified": bool(row["verified"]), "truncated": bool(row["truncated"])}

    @staticmethod
    def _cursor(fingerprint: str, message_time: float, message_id: str) -> str:
        """
        生成绑定身份与筛选条件的稳定分页游标

        参数:
        - fingerprint: 当前对话与筛选条件的摘要
        - message_time: 上一页最旧消息时间
        - message_id: 同时间消息的稳定排序字段

        返回:
        - URL 安全的版本化游标
        """
        payload = json.dumps([1, fingerprint, message_time, message_id], separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    def query(self, scope: MessageScope, *, keyword: str | None = None, sender_id: str | None = None,
              start_time: str | None = None, end_time: str | None = None, limit: int = 20,
              cursor: str | None = None, before_message_id: str | None = None, text_budget: int = 12000) -> dict[str, Any]:
        """
        按当前对话检索消息, 倒序分页并按时间正序展示每页

        参数:
        - scope: 当前对话身份
        - keyword: 普通文本子串, 不作为 SQL 通配符或正则
        - sender_id: 精确发送者 ID
        - start_time: 含时区的起始时间, 包含边界
        - end_time: 含时区的结束时间, 包含边界
        - limit: 返回条数, 1 到 100
        - cursor: 与当前身份和筛选条件绑定的上一页游标
        - before_message_id: 排除该消息及其后续消息
        - text_budget: 本页正文的字符预算, 128 到 100000

        返回:
        - 消息列表, 分页, 截断和实际采集范围
        """
        # Step.1 校验筛选条件并将游标绑定到当前对话
        self._check_scope(scope)
        if type(limit) is not int or not 1 <= limit <= 100 or type(text_budget) is not int or not 128 <= text_budget <= 100000:
            raise ValueError("查询条数或文本预算超出允许范围")
        if keyword is not None and (not isinstance(keyword, str) or len(keyword) > 1024):
            raise ValueError("查询关键词必须是不超过 1024 字符的文本")
        if sender_id is not None:
            _identity(sender_id)
        if before_message_id is not None:
            _identity(before_message_id)
        start, end = archive_time(start_time), archive_time(end_time)
        if start is not None and end is not None and start > end:
            raise ValueError("查询起始时间不能晚于结束时间")
        fingerprint = hashlib.sha256(json.dumps(
            [scope.key, keyword or "", sender_id, start, end, before_message_id], ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        anchor: tuple[float, str] | None = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError
                decoded = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
                if (not isinstance(decoded, list) or len(decoded) != 4 or decoded[:2] != [1, fingerprint]
                        or type(decoded[2]) not in {int, float} or not math.isfinite(decoded[2]) or decoded[2] < 0):
                    raise ValueError
                anchor = (float(decoded[2]), _identity(decoded[3]))
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ValueError("查询游标无效或不属于当前对话及条件") from exc
        empty: dict[str, Any] = {"ok": True, "items": [], "source": "local_archive", "has_more": False,
                                "next_cursor": None, "truncated": False, "revision": 0,
                                "coverage": {"archived_from": None, "archived_to": None, "complete": False,
                                             "retention_days": self.retention_days}}
        # Step.2 在同一读取快照中取得档案覆盖范围与管理修订
        connection = self._connect()
        if connection is None:
            if before_message_id is not None:
                raise MessageArchiveError("message_not_found", "分页来源消息不在当前对话档案中")
            return empty
        with closing(connection):
            connection.execute("BEGIN")
            chat = connection.execute("SELECT * FROM platform_message_chats WHERE scope_key=?", (scope.key,)).fetchone()
            cutoff = self._clock() - self.retention_days * 86400
            coverage = connection.execute(
                "SELECT MIN(message_time), MAX(message_time) FROM platform_messages "
                "WHERE scope_key=? AND status='active' AND message_time>=?", (scope.key, cutoff),
            ).fetchone()
            empty["coverage"].update({"archived_from": coverage[0], "archived_to": coverage[1]})
            empty["revision"] = int(chat["revision"]) if chat else 0
            where, args = ["scope_key=?", "status='active'", "message_time>=?"], [scope.key, cutoff]
            if keyword:
                where.append("instr(text, ?)>0")
                args.append(keyword)
            if sender_id is not None:
                where.append("sender_id=?")
                args.append(sender_id)
            for operator, value in ((">=", start), ("<=", end)):
                if value is not None:
                    where.append(f"message_time{operator}?")
                    args.append(value)
            anchors = [anchor] if anchor else []
            if before_message_id is not None:
                before = connection.execute(
                    "SELECT message_time, message_id FROM platform_messages WHERE scope_key=? AND message_id=?",
                    (scope.key, before_message_id),
                ).fetchone()
                if before is None:
                    raise MessageArchiveError("message_not_found", "分页来源消息不在当前对话档案中")
                anchors.append((float(before[0]), str(before[1])))
            for point in anchors:
                where.append("(message_time<? OR (message_time=? AND message_id<?))")
                args.extend([point[0], point[0], point[1]])
            # Step.3 使用稳定时间与消息 ID 次序检索有界分页
            rows = connection.execute(
                "SELECT * FROM platform_messages WHERE " + " AND ".join(where)
                + " ORDER BY message_time DESC, message_id DESC LIMIT ?", (*args, limit + 1),
            ).fetchall()
            more = len(rows) > limit
            page = rows[:limit]
            # Step.4 按时间正序展示, 截断正文时保留消息身份和引用
            items = [self._item(row) for row in reversed(page)]
            remaining = text_budget
            for item in items:
                text = str(item["text"])
                if len(text) > remaining:
                    item["text"] = text[:remaining]
                    item["truncated"] = True
                remaining -= len(str(item["text"]))
            empty.update({"items": items, "has_more": more, "truncated": any(item["truncated"] for item in items),
                          "next_cursor": self._cursor(fingerprint, page[-1]["message_time"], page[-1]["message_id"]) if more else None})
            return empty

    @staticmethod
    def _erase(connection: sqlite3.Connection, scope_key: str, message_ids: Sequence[str], status: str, token: str) -> None:
        """
        保留最小去重标记并擦除可检索正文和成员资料

        参数:
        - connection: 当前写事务连接
        - scope_key: 已核验的内部对话键
        - message_ids: 需要擦除的消息 ID
        - status: deleted, recalled 或 expired
        - token: 删除动作或保留策略的标记
        """
        connection.executemany(
            "UPDATE platform_messages SET status=?, delete_token=?, sender_id='', nickname='', card='', text='', "
            "components_json='[]', reply_to_message_id=NULL, mentions_json='[]', media_json='[]' "
            "WHERE scope_key=? AND message_id=?", ((status, token, scope_key, item) for item in message_ids),
        )

    def recall(self, scope: MessageScope, message_id: str) -> None:
        """
        记录可信撤回事件, 即使原文尚未入档也阻止重放恢复

        参数:
        - scope: 撤回事件确认所属的对话
        - message_id: 已由适配器核验的消息 ID
        """
        self._check_scope(scope)
        _identity(message_id)
        connection = self._connect(create=True)
        assert connection is not None
        with closing(connection), connection:
            connection.execute("BEGIN IMMEDIATE")
            self._chat(connection, scope)
            now = self._clock()
            connection.execute(
                "INSERT INTO platform_messages(scope_key, message_id, message_time, received_at, direction, source, status, time_source) "
                "VALUES(?, ?, ?, ?, 'inbound', 'platform_recall', 'recalled', 'local') ON CONFLICT(scope_key, message_id) DO NOTHING",
                (scope.key, message_id, now, now),
            )
            self._erase(connection, scope.key, [message_id], "recalled", "recall")
            connection.execute("UPDATE platform_message_chats SET revision=revision+1 WHERE scope_key=?", (scope.key,))

    def delete(self, scope: MessageScope, *, message_ids: Sequence[str] | None = None,
               expected_revision: int) -> dict[str, Any]:
        """
        删除选定消息或清空对话档案, 保存有限期恢复备份

        参数:
        - scope: 已授权管理的对话
        - message_ids: 指定消息列表, None 表示清空当前对话档案
        - expected_revision: 界面读取的删除状态修订, 防止覆盖并发管理动作

        返回:
        - 删除数量, 新修订, 备份 ID 和恢复截止时间
        """
        self._check_scope(scope)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("删除修订必须是非负整数")
        if message_ids is not None:
            if isinstance(message_ids, (str, bytes)) or not 1 <= len(message_ids) <= 100:
                raise ValueError("每次删除必须指定 1 到 100 条消息")
            message_ids = list(dict.fromkeys(_identity(item) for item in message_ids))
        connection = self._connect(create=True)
        assert connection is not None
        with closing(connection), connection:
            connection.execute("BEGIN IMMEDIATE")
            chat = self._chat(connection, scope)
            if chat["revision"] != expected_revision:
                raise MessageArchiveError("revision_conflict", "档案已被其他管理操作修改, 请刷新")
            selection = " AND message_id IN (" + ",".join("?" for _ in message_ids) + ")" if message_ids else ""
            rows = connection.execute(
                "SELECT * FROM platform_messages WHERE scope_key=? AND status IN ('active', 'deleted')" + selection,
                (scope.key, *(message_ids or [])),
            ).fetchall()
            token, now = uuid.uuid4().hex, self._clock()
            known = {str(row["message_id"]) for row in rows}
            unknown_ids = [item for item in (message_ids or []) if item not in known and connection.execute(
                "SELECT 1 FROM platform_messages WHERE scope_key=? AND message_id=?", (scope.key, item),
            ).fetchone() is None]
            payload = {"rows": [dict(row) for row in rows], "delete_before": chat["delete_before"],
                       "delete_token": chat["delete_token"], "unknown_ids": unknown_ids}
            expires = now + self.retention_days * 86400
            connection.execute(
                "INSERT INTO platform_message_backups VALUES(?, ?, ?, ?, ?, ?)",
                (token, scope.key, "clear" if message_ids is None else "delete", now, expires,
                 json.dumps(payload, ensure_ascii=False)),
            )
            ids = [str(row["message_id"]) for row in rows]
            self._erase(connection, scope.key, ids, "deleted", token)
            if message_ids is not None:
                for message_id in message_ids:
                    connection.execute(
                        "INSERT INTO platform_messages(scope_key, message_id, message_time, received_at, direction, source, "
                        "status, delete_token, time_source) VALUES(?, ?, ?, ?, 'inbound', 'local_delete', 'deleted', ?, 'local') "
                        "ON CONFLICT(scope_key, message_id) DO NOTHING", (scope.key, message_id, now, now, token),
                    )
            else:
                connection.execute(
                    "UPDATE platform_message_chats SET delete_before=MAX(COALESCE(delete_before, ?), ?), delete_token=? "
                    "WHERE scope_key=?", (now, now, token, scope.key),
                )
            connection.execute("UPDATE platform_message_chats SET revision=revision+1 WHERE scope_key=?", (scope.key,))
            return {"deleted_count": sum(row["status"] == "active" for row in rows), "revision": expected_revision + 1,
                    "backup_id": token, "expires_at": expires}

    def restore(self, scope: MessageScope, backup_id: str, *, expected_revision: int) -> dict[str, Any]:
        """
        恢复仍归属该删除动作的消息, 不覆盖新消息或后续撤回

        参数:
        - scope: 已授权管理的对话
        - backup_id: 该对话删除操作返回的备份 ID
        - expected_revision: 管理界面读取的当前修订

        返回:
        - 实际恢复条数, 跳过条数和新修订
        """
        self._check_scope(scope)
        _identity(backup_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("恢复修订必须是非负整数")
        connection = self._connect()
        if connection is None:
            raise MessageArchiveError("backup_not_found", "档案恢复备份不存在")
        with closing(connection), connection:
            connection.execute("BEGIN IMMEDIATE")
            chat = connection.execute("SELECT * FROM platform_message_chats WHERE scope_key=?", (scope.key,)).fetchone()
            backup = connection.execute(
                "SELECT * FROM platform_message_backups WHERE scope_key=? AND backup_id=?", (scope.key, backup_id),
            ).fetchone()
            if (backup is None or backup["expires_at"] <= self._clock()
                    or backup["created_at"] < self._clock() - self.retention_days * 86400):
                raise MessageArchiveError("backup_not_found", "档案恢复备份不存在或已过期")
            if chat is None or chat["revision"] != expected_revision:
                raise MessageArchiveError("revision_conflict", "档案已被其他管理操作修改, 请刷新")
            targets = connection.execute(
                "SELECT 1 FROM platform_messages WHERE scope_key=? AND status='deleted' AND delete_token=? LIMIT 1",
                (scope.key, backup_id),
            ).fetchone()
            if targets is None and not (backup["action"] == "clear" and chat["delete_token"] == backup_id):
                raise MessageArchiveError("backup_superseded", "该删除已被后续操作覆盖, 请先处理后续操作")
            payload = json.loads(backup["payload_json"])
            restored = 0
            for row in payload["rows"]:
                if row["status"] == "active" and row["message_time"] < self._clock() - self.retention_days * 86400:
                    continue
                result = connection.execute(
                    "UPDATE platform_messages SET sender_id=?, nickname=?, card=?, text=?, components_json=?, "
                    "reply_to_message_id=?, mentions_json=?, media_json=?, status=?, delete_token=? "
                    "WHERE scope_key=? AND message_id=? AND status='deleted' AND delete_token=?",
                    (row["sender_id"], row["nickname"], row["card"], row["text"], row["components_json"],
                     row["reply_to_message_id"], row["mentions_json"], row["media_json"], row["status"], row["delete_token"],
                     scope.key, row["message_id"], backup_id),
                )
                restored += result.rowcount
            for message_id in payload["unknown_ids"]:
                connection.execute(
                    "DELETE FROM platform_messages WHERE scope_key=? AND message_id=? AND status='deleted' AND delete_token=?",
                    (scope.key, message_id, backup_id),
                )
            if backup["action"] == "clear" and chat["delete_token"] == backup_id:
                connection.execute(
                    "UPDATE platform_message_chats SET delete_before=?, delete_token=? WHERE scope_key=?",
                    (payload["delete_before"], payload["delete_token"], scope.key),
                )
            connection.execute("DELETE FROM platform_message_backups WHERE backup_id=?", (backup_id,))
            connection.execute("UPDATE platform_message_chats SET revision=revision+1 WHERE scope_key=?", (scope.key,))
            return {"restored_count": restored, "skipped_count": len(payload["rows"]) - restored,
                    "revision": expected_revision + 1}

    def backfill_allowed(self, scope: MessageScope, message_id: str, message_time: float | None = None) -> bool:
        """
        在适配器回源前后检查本地删除和保留范围

        参数:
        - scope: 当前对话身份
        - message_id: 拟读取的消息 ID
        - message_time: 回源后核验的原始时间, 未读取时为 None

        返回:
        - 没有删除标记且不违反已知范围时返回 True
        """
        self._check_scope(scope)
        _identity(message_id)
        if message_time is not None and (not math.isfinite(message_time) or message_time < 0):
            raise ValueError("消息回源时间无效")
        if message_time is not None and message_time < self._clock() - self.retention_days * 86400:
            return False
        connection = self._connect()
        if connection is None:
            return True
        with closing(connection):
            row = connection.execute(
                "SELECT status, message_time FROM platform_messages WHERE scope_key=? AND message_id=?", (scope.key, message_id),
            ).fetchone()
            if row is not None and (row[0] != "active" or row[1] < self._clock() - self.retention_days * 86400):
                return False
            chat = connection.execute("SELECT delete_before FROM platform_message_chats WHERE scope_key=?", (scope.key,)).fetchone()
            return chat is None or chat[0] is None or message_time is None or message_time > chat[0]

    def purge(self) -> dict[str, int]:
        """
        擦除过期正文与恢复备份, 保留阻止重放的最小消息标记

        返回:
        - 正文过期条数和备份清理条数
        """
        connection = self._connect()
        if connection is None:
            return {"expired_count": 0, "backup_count": 0}
        with closing(connection), connection:
            connection.execute("BEGIN IMMEDIATE")
            self._save_retention(connection)
            now = self._clock()
            result = connection.execute(
                "UPDATE platform_messages SET status='expired', delete_token='retention', sender_id='', nickname='', card='', "
                "text='', components_json='[]', reply_to_message_id=NULL, mentions_json='[]', media_json='[]' "
                "WHERE status='active' AND message_time<?", (now - self.retention_days * 86400,),
            )
            backups = connection.execute(
                "DELETE FROM platform_message_backups WHERE expires_at<=? OR created_at<?",
                (now, now - self.retention_days * 86400),
            )
            return {"expired_count": result.rowcount, "backup_count": backups.rowcount}
