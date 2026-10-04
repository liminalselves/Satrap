"""持久一次性提醒与发送状态, 不持有入站事件或会话对象"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any
import base64
import hashlib
import json
import sqlite3
import time
import uuid
import math

from satrap.core.config.platform_messages import MessageScope
from satrap.core.config.platform_schema import ensure_platform_tables
from satrap.core.platform.receipt import SendReceipt
from satrap.core.platform.scheduled import ScheduledTarget
from satrap.core.group_chat.reminder_time import resolve_reminder_time


class ReminderError(ValueError):
    """携带稳定错误码的提醒拒绝"""

    def __init__(self, code: str, message: str) -> None:
        """
        构造提醒业务错误

        参数:
        - code: 稳定错误码
        - message: 对外说明
        """
        super().__init__(message)
        self.code = code


ACTIVE_STATES = frozenset({"scheduled", "waiting_delivery", "paused", "sending"})
FINAL_STATES = frozenset({"sent", "partial", "failed", "unknown", "missed", "cancelled"})


class ReminderStore:
    """平台数据库中的提醒, 条件事务解决取消与发送竞争"""

    def __init__(self, database: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        """
        绑定宿主确定的平台数据库

        参数:
        - database: 当前平台数据库路径
        - clock: UTC 时间来源
        """
        self.database = Path(database)
        self.clock = clock
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            ensure_platform_tables(connection)

    def _connect(self) -> sqlite3.Connection:
        """
        创建独立事务连接

        返回:
        - 按名称读取字段的数据库连接
        """
        connection = sqlite3.connect(str(self.database), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        """
        将持久记录转为可展示结果

        参数:
        - row: 数据库记录

        返回:
        - 含解析后的范围, 时间与发送结果的提醒
        """
        result = dict(row)
        result["scope"] = json.loads(result.pop("scope_json"))
        result["source_agent"] = json.loads(result.pop("source_agent_json"))
        result.pop("scope_key")
        result["creator_user_id"] = result.pop("creator_id")
        result["mention_user_ids"] = json.loads(result.pop("mentions_json"))
        result["delivery"] = json.loads(result.pop("delivery_json")) if row["delivery_json"] else None
        result["due_at_utc"] = datetime.fromtimestamp(row["due_at"], timezone.utc).isoformat()
        result["due_at"] = datetime.fromtimestamp(row["due_at"], timezone.utc).astimezone().isoformat()
        result["due_timestamp"] = row["due_at"]
        result["schema_version"] = 1
        return result

    def _lookup(self, connection: sqlite3.Connection, scope: MessageScope, identity: str, actor: str = "") -> sqlite3.Row:
        """
        在授权范围内读取任务

        参数:
        - connection: 当前连接
        - scope: 当前群范围
        - identity: 提醒 ID
        - actor: 非空时限定为该成员创建

        返回:
        - 任务记录, 缺失或不属于本人时拒绝
        """
        row = connection.execute("SELECT * FROM group_chat_reminders WHERE scope_key=? AND reminder_id=?" + (" AND creator_id=?" if actor else ""),
                                 (scope.key, identity, *([actor] if actor else []))).fetchone()
        if row is None:
            raise ReminderError("not_found", "提醒不存在或不属于当前可管理范围")
        return row

    def create(self, scope: MessageScope, *, actor: str, text: str, mentions: list[str], source_message_id: str,
               operation_id: str, due_at: float | None = None, time_spec: dict[str, Any] | None = None,
               creator_kind: str = "model", member_limit: int = 20, group_limit: int = 200,
               source_agent: dict[str, Any] | None = None, authorize: Callable[[], None] | None = None) -> dict[str, Any]:
        """
        有限配额下幂等创建一次性任务

        参数:
        - scope: 宿主确定的当前群
        - actor: 可信创建者
        - text: 固定提醒正文
        - mentions: 已核验的提及成员
        - source_message_id: 模型来源消息, 人工可为空
        - operation_id: 请求幂等标识
        - due_at: 直接传入 UTC 截止时间时使用, 与 time_spec 二选一
        - time_spec: 在事务接受时间解析的原始时间选择, 重试不会延后截止时间
        - creator_kind: model 或 operator
        - member_limit: 每成员活动配额
        - group_limit: 每群活动配额
        - source_agent: 可信来源会话 ID, 命名配置和路由代次, 不含会话对象或上下文
        - authorize: 取得写事务后再次检查权限, None 仅供可信存储内部调用

        返回:
        - created 与任务详情, 重复提交返回同一任务
        """
        if scope.conversation_kind != "group" or not actor or creator_kind not in {"model", "operator"}:
            raise ReminderError("invalid_argument", "提醒范围或创建者无效")
        if creator_kind == "model" and (not isinstance(source_message_id, str) or not source_message_id):
            raise ReminderError("invalid_source", "模型创建提醒需要真实来源消息 ID")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
            raise ReminderError("invalid_argument", "提醒正文需要 1 至 2000 字")
        if (not isinstance(mentions, list) or len(mentions) > 10
                or any(not isinstance(item, str) or not item or len(item) > 256 or item == "all" or any(ord(char) < 32 for char in item) for item in mentions)
                or len(set(mentions)) != len(mentions)):
            raise ReminderError("invalid_argument", "提及成员需要 0 至 10 个不重复的成员 ID")
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 512:
            raise ReminderError("invalid_argument", "提醒操作标识无效")
        if (due_at is None) == (time_spec is None):
            raise ReminderError("invalid_time", "提醒时间只能选择一种填写方式")
        if type(member_limit) is not int or not 1 <= member_limit <= 100 or type(group_limit) is not int or not 1 <= group_limit <= 1000:
            raise ReminderError("invalid_argument", "提醒配额无效")
        source_agent = dict(source_agent or {})
        if (set(source_agent) - {"session_id", "config_name", "route_generation", "instance_id"}
                or any(not isinstance(source_agent.get(key, ""), str) or len(source_agent.get(key, "")) > 1024 for key in ("session_id", "config_name", "instance_id"))
                or type(source_agent.get("route_generation", 0)) is not int):
            raise ReminderError("invalid_argument", "提醒来源 Agent 标识无效")
        fingerprint = hashlib.sha256(json.dumps([actor, creator_kind, text, time_spec if time_spec is not None else due_at,
                                                 mentions, source_message_id, source_agent], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT * FROM reminder_operations WHERE scope_key=? AND operation_id=?", (scope.key, operation_id)).fetchone()
            if authorize is not None:
                authorize()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ReminderError("idempotency_conflict", "同一提醒操作不能提交不同内容")
                return {"ok": True, "status": "created", "reminder": self._record(self._lookup(connection, scope, previous["reminder_id"]))}
            now = self.clock()
            if time_spec is not None:
                try:
                    due_at = resolve_reminder_time(time_spec, now)
                except (ValueError, OverflowError, OSError) as exc:
                    raise ReminderError(getattr(exc, "code", "invalid_time"), str(exc)) from exc
            if isinstance(due_at, bool) or not isinstance(due_at, (int, float)) or not math.isfinite(due_at) or not now + 10 <= due_at <= now + 31536000:
                raise ReminderError("invalid_time", "执行时间需要在至少 10 秒后, 且不超过一年")
            states = "('scheduled','waiting_delivery','paused','sending')"
            count = connection.execute(f"SELECT COUNT(*) FROM group_chat_reminders WHERE scope_key=? AND state IN {states}", (scope.key,)).fetchone()[0]
            own = connection.execute(f"SELECT COUNT(*) FROM group_chat_reminders WHERE scope_key=? AND creator_id=? AND state IN {states}", (scope.key, actor)).fetchone()[0]
            if count >= group_limit or own >= member_limit:
                raise ReminderError("quota_exceeded", "当前群或创建者的活动提醒已达上限")
            identity = "rem_" + uuid.uuid4().hex
            connection.execute("INSERT INTO group_chat_reminders (reminder_id, scope_key, scope_json, creator_id, creator_kind, source_message_id, text, mentions_json, due_at, created_at, source_agent_json) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (identity, scope.key, json.dumps({"adapter_id": scope.adapter_id, "self_id": scope.self_id, "conversation_kind": scope.conversation_kind, "chat_id": scope.chat_id}, ensure_ascii=False),
                                actor, creator_kind, source_message_id, text.strip(), json.dumps(mentions, ensure_ascii=False), due_at, now, json.dumps(source_agent, ensure_ascii=False)))
            connection.execute("INSERT INTO reminder_operations VALUES (?, ?, ?, ?)", (scope.key, operation_id, fingerprint, identity))
            return {"ok": True, "status": "created", "reminder": self._record(self._lookup(connection, scope, identity))}

    def get(self, scope: MessageScope, identity: str, *, actor: str = "") -> dict[str, Any]:
        """
        读取一条授权范围内的提醒

        参数:
        - scope: 当前群
        - identity: 提醒 ID
        - actor: 模型限定本人, 人工管理为空

        返回:
        - 提醒详情
        """
        with closing(self._connect()) as connection:
            return {"ok": True, "reminder": self._record(self._lookup(connection, scope, identity, actor))}

    def list(self, scope: MessageScope, *, actor: str = "", state: str = "", limit: int = 20, cursor: str = "") -> dict[str, Any]:
        """
        有界查询当前群的提醒, 模型调用始终限定本人

        参数:
        - scope: 当前群范围
        - actor: 可管理的创建者, 人工为空
        - state: 可选状态筛选
        - limit: 每页 1 至 50 条
        - cursor: 同一范围和筛选下的分页游标

        返回:
        - items, has_more 和 next_cursor
        """
        if state and state not in ACTIVE_STATES | FINAL_STATES or type(limit) is not int or not 1 <= limit <= 50:
            raise ReminderError("invalid_argument", "提醒状态或分页无效")
        if not isinstance(cursor, str) or len(cursor) > 4096:
            raise ReminderError("invalid_cursor", "提醒游标无效")
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM group_chat_reminders WHERE scope_key=?" + (" AND creator_id=?" if actor else "")
                                      + (" AND state=?" if state else "") + " ORDER BY due_at, reminder_id", (scope.key, *([actor] if actor else []), *([state] if state else []))).fetchall()
            signature = hashlib.sha256(json.dumps([scope.key, actor, state, limit, [(row["reminder_id"], row["revision"]) for row in rows]]).encode()).hexdigest()
            offset = 0
            if cursor:
                try:
                    token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                    if token[0] != signature or type(token[1]) is not int or token[1] < 0:
                        raise ValueError()
                    offset = token[1]
                except Exception as exc:
                    raise ReminderError("invalid_cursor", "任务或筛选已变化, 请重新查询") from exc
            more = offset + limit < len(rows)
            return {"ok": True, "items": [self._record(row) for row in rows[offset:offset + limit]], "has_more": more,
                    "next_cursor": base64.urlsafe_b64encode(json.dumps([signature, offset + limit]).encode()).decode() if more else None}

    def change(self, scope: MessageScope, identity: str, action: str, expected_revision: int, *, actor: str = "", grace: int = 600,
               authorize: Callable[[], None] | None = None) -> dict[str, Any]:
        """
        取消或明确恢复任务, 与发送占用在同一事务中竞争

        参数:
        - scope: 当前群
        - identity: 提醒 ID
        - action: cancel 或 resume
        - expected_revision: 最近读到的任务修订
        - actor: 模型只能处理本人任务
        - grace: 过期补发宽限
        - authorize: 取得写事务后立即复核权限

        返回:
        - 实际新状态, 已开始发送时明确返回 too_late_to_cancel
        """
        if action not in {"cancel", "resume"} or type(expected_revision) is not int:
            raise ReminderError("invalid_argument", "任务动作或修订无效")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if authorize is not None:
                authorize()
            row = self._lookup(connection, scope, identity, actor)
            if action == "cancel" and row["state"] == "cancelled":
                return {"ok": True, "status": "cancelled", "reminder": self._record(row)}
            if row["revision"] != expected_revision:
                raise ReminderError("revision_conflict", "任务已变化, 请重新查询")
            if action == "cancel" and row["state"] not in {"scheduled", "waiting_delivery", "paused"}:
                return {"ok": False, "status": "too_late_to_cancel", "reminder": self._record(row)}
            if action == "resume" and row["state"] != "paused":
                raise ReminderError("invalid_state", "只能明确恢复已暂停的提醒")
            state = "cancelled" if action == "cancel" else "missed" if self.clock() > row["due_at"] + grace else "scheduled"
            connection.execute("UPDATE group_chat_reminders SET state=?, revision=revision+1, reason='', retry_at=NULL, paused_at=NULL, settled_at=? WHERE scope_key=? AND reminder_id=?",
                               (state, self.clock() if state in FINAL_STATES else None, scope.key, identity))
            return {"ok": True, "status": state, "reminder": self._record(self._lookup(connection, scope, identity))}

    def recover(self) -> int:
        """
        启动时将未确认的发送标记为未知, 不恢复网络发送

        返回:
        - 转换为未知的任务数量
        """
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute("SELECT reminder_id FROM group_chat_reminders WHERE state='sending'").fetchall()
            for row in pending:
                attempt = connection.execute("SELECT plan_json FROM group_chat_reminder_attempts WHERE reminder_id=? ORDER BY created_at DESC LIMIT 1", (row["reminder_id"],)).fetchone()
                plan = json.loads(attempt["plan_json"]) if attempt else []
                ids = [identity for segment in plan for identity in segment.get("receipt", {}).get("message_ids", [])]
                detail = json.dumps({"status": "unknown", "message_ids": ids, "reason": "interrupted_send", "failed_index": None})
                connection.execute("UPDATE group_chat_reminders SET state='unknown', revision=revision+1, reason='interrupted_send', settled_at=?, delivery_json=? WHERE reminder_id=?",
                                   (self.clock(), detail, row["reminder_id"]))
            connection.execute("UPDATE group_chat_reminder_attempts SET status='unknown' WHERE status IN ('planned','sending')")
            return len(pending)

    def due(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """
        返回尚未取得发送权的到期任务

        参数:
        - limit: 每轮最多处理数量

        返回:
        - 到期且重试等待结束的任务
        """
        with closing(self._connect()) as connection:
            return [self._record(row) for row in connection.execute("SELECT * FROM group_chat_reminders WHERE state IN ('scheduled','waiting_delivery') "
                    "AND due_at<=? AND (retry_at IS NULL OR retry_at<=?) ORDER BY due_at LIMIT ?", (self.clock(), self.clock(), limit))]

    def transition(self, identity: str, expected_states: tuple[str, ...], state: str, reason: str = "", retry_at: float | None = None) -> bool:
        """
        以条件更新占用或改变状态, 失败表示另一操作已经取得控制权

        参数:
        - identity: 当前存储中的任务 ID
        - expected_states: 允许的原状态
        - state: 目标状态
        - reason: 暂停或失败原因
        - retry_at: 尚未网络发送时的下次检查时间

        返回:
        - 是否成功改变状态
        """
        if state not in ACTIVE_STATES | FINAL_STATES or not expected_states:
            raise ReminderError("invalid_state", "任务状态变化无效")
        with closing(self._connect()) as connection, connection:
            result = connection.execute("UPDATE group_chat_reminders SET state=?, revision=revision+1, reason=?, retry_at=?, paused_at=?, "
                                        "retry_count=retry_count+?, settled_at=? "
                                        "WHERE reminder_id=? AND state IN (" + ",".join("?" for _ in expected_states) + ")",
                                        (state, reason, retry_at, self.clock() if state == "paused" else None,
                                         int(state == "waiting_delivery"), self.clock() if state in FINAL_STATES else None, identity, *expected_states))
            return result.rowcount == 1

    def active(self) -> list[dict[str, Any]]:
        """
        读取所有尚未发送的任务以检查停用, 包括未来到期的任务

        返回:
        - scheduled 与 waiting_delivery 任务, 已暂停任务不会静默恢复
        """
        with closing(self._connect()) as connection:
            return [self._record(row) for row in connection.execute(
                "SELECT * FROM group_chat_reminders WHERE state IN ('scheduled','waiting_delivery') ORDER BY due_at",
            )]

    def cleanup(self, retention_days: int = 30) -> int:
        """
        清理过期终态正文与发送证据, 保留不含正文的操作索引

        参数:
        - retention_days: 终态默认保留 30 天, 活动任务不参与清理

        返回:
        - 清理的终态任务数量
        """
        if type(retention_days) is not int or not 1 <= retention_days <= 3650:
            raise ReminderError("invalid_argument", "提醒保留天数无效")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            identities = [row[0] for row in connection.execute(
                "SELECT reminder_id FROM group_chat_reminders WHERE state IN ('sent','partial','failed','unknown','missed','cancelled') AND settled_at<?",
                (self.clock() - retention_days * 86400,),
            )]
            for identity in identities:
                connection.execute("DELETE FROM group_chat_reminder_attempts WHERE reminder_id=?", (identity,))
                connection.execute("DELETE FROM group_chat_reminders WHERE reminder_id=?", (identity,))
            return len(identities)


class ReminderRecorder:
    """单任务独享的记录器, 计划落盘与发送占用不可分开"""

    def __init__(self, store: ReminderStore, reminder: dict[str, Any], attempt_id: str) -> None:
        """
        绑定本次已读取的任务修订

        参数:
        - store: 宿主确定的任务存储
        - reminder: 调度器最近读到的完整任务
        - attempt_id: 本次发送尝试 ID
        """
        self.store = store
        self.identity = reminder["reminder_id"]
        self.revision = reminder["revision"]
        self.scope = MessageScope(**reminder["scope"])
        self.attempt_id = attempt_id

    def plan(self, target: ScheduledTarget, segments: list[dict[str, Any]]) -> bool:
        """
        与取消竞争并在同一事务中写入分段计划

        参数:
        - target: 本次发送的冻结目标
        - segments: 原生适配器确定的实际分段摘要

        返回:
        - 任务仍可发送且修订相同才返回 True
        """
        if (target.scope != self.scope or target.reminder_id != self.identity or target.attempt_id != self.attempt_id
                or not segments or len(segments) > 100):
            return False
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            claimed = connection.execute("UPDATE group_chat_reminders SET state='sending', revision=revision+1, retry_at=NULL, reason='' "
                                         "WHERE reminder_id=? AND scope_key=? AND revision=? AND state IN ('scheduled','waiting_delivery') AND due_at<=?",
                                         (self.identity, self.scope.key, self.revision, self.store.clock()))
            if claimed.rowcount != 1:
                return False
            plan = [{**segment, "state": "planned"} for segment in segments]
            connection.execute("INSERT INTO group_chat_reminder_attempts VALUES (?, ?, ?, 'planned', ?, NULL)",
                               (self.attempt_id, self.identity, self.store.clock(), json.dumps(plan, ensure_ascii=False)))
            return True

    def _segment(self, index: int, receipt: SendReceipt | None = None) -> bool:
        """
        条件推进一个实际分段, 不能覆盖已有确认

        参数:
        - index: 分段索引
        - receipt: None 表示提交前标记, 否则保存实际结果

        返回:
        - 当前发送尝试允许该变化且成功落盘时返回 True
        """
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM group_chat_reminder_attempts WHERE attempt_id=? AND reminder_id=? AND status IN ('planned','sending')",
                                     (self.attempt_id, self.identity)).fetchone()
            if row is None:
                return False
            plan = json.loads(row["plan_json"])
            if type(index) is not int or not 0 <= index < len(plan):
                return False
            segment = plan[index]
            if receipt is None:
                if segment["state"] != "planned":
                    return False
                segment["state"] = "submitted"
            else:
                if segment["state"] != "submitted":
                    return False
                segment["state"] = "sent" if receipt.status == "success" else receipt.status
                segment["receipt"] = {"status": receipt.status, "message_ids": list(receipt.message_ids), "reason": receipt.reason, "failed_index": receipt.failed_index}
            connection.execute("UPDATE group_chat_reminder_attempts SET plan_json=?, status='sending' WHERE attempt_id=?",
                               (json.dumps(plan, ensure_ascii=False), self.attempt_id))
            return True

    def submitted(self, index: int) -> bool:
        """
        发送前标记网络提交, 失败必须阻止该段发送

        参数:
        - index: 实际分段序号

        返回:
        - 提交证据已落盘时返回 True
        """
        return self._segment(index)

    def result(self, index: int, receipt: SendReceipt) -> bool:
        """
        保存实际分段回执和已确认消息 ID

        参数:
        - index: 实际分段序号
        - receipt: 平台真实回执

        返回:
        - 分段结果落盘后返回 True
        """
        return self._segment(index, receipt)

    def complete(self, receipt: SendReceipt) -> bool:
        """
        在同一事务中结算发送证据与任务, 防止恢复时重复领取

        参数:
        - receipt: 所有实际分段的汇总结果

        返回:
        - 尝试与任务均已结算时返回 True
        """
        state = "sent" if receipt.status == "success" else receipt.status
        detail = json.dumps({"status": receipt.status, "message_ids": list(receipt.message_ids), "reason": receipt.reason, "failed_index": receipt.failed_index})
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute("SELECT * FROM group_chat_reminder_attempts WHERE attempt_id=? AND reminder_id=? AND status IN ('planned','sending')",
                                         (self.attempt_id, self.identity)).fetchone()
            if attempt is None:
                return False
            plan = json.loads(attempt["plan_json"])
            for segment in plan:
                if segment["state"] == "planned":
                    segment["state"] = "skipped"
                elif segment["state"] == "submitted":
                    segment["state"] = "unknown" if state == "unknown" else "failed"
            changed = connection.execute("UPDATE group_chat_reminders SET state=?, revision=revision+1, reason=?, delivery_json=?, settled_at=? "
                                         "WHERE reminder_id=? AND scope_key=? AND state='sending'", (state, receipt.reason, detail, self.store.clock(), self.identity, self.scope.key))
            if changed.rowcount != 1:
                return False
            connection.execute("UPDATE group_chat_reminder_attempts SET status=?, plan_json=?, result_json=? WHERE attempt_id=?",
                               (state, json.dumps(plan, ensure_ascii=False), detail, self.attempt_id))
            return True
