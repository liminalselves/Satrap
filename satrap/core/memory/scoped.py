"""群记忆与成员偏好的事务存储, 复用已有 memories 表"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import base64
import hashlib
import json
import sqlite3
import unicodedata
import uuid
from typing import Any

from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore
from satrap.core.memory.store import MemoryStore
from satrap.core.memory.lifecycle import erase_memory

_MEMORY_ORDER = "importance DESC, updated_at DESC, id"
"""当前群记忆的确定性分页次序, 末位 ID 保证同重要度与同时间记录的稳定顺序"""


class MemoryError(ValueError):
    """带稳定错误码的记忆业务错误"""

    def __init__(self, code: str, message: str) -> None:
        """
        初始化业务错误

        参数:
        - code: 稳定错误码
        - message: 可以向用户展示的说明
        """
        super().__init__(message)
        self.code = code


class ScopedMemories:
    """以真实对话范围管理记忆, 不绑定当前 Agent 实例"""

    def __init__(self, archive: PlatformMessageStore, scope: MessageScope) -> None:
        """
        绑定可信范围与档案存储

        参数:
        - archive: 同一平台的消息档案
        - scope: 宿主解析的真实对话身份
        """
        archive._check_scope(scope)
        if scope.conversation_kind != "group":
            raise MemoryError("wrong_conversation", "该记忆范围必须属于群聊")
        self.archive = archive
        self.scope = scope
        self.store = MemoryStore(db_path=archive.database, scope=scope.key)

    def _sources(self, ids: list[str], actor: str = "", current: str = "") -> None:
        """
        核验有效来源与可选的本人及本轮要求

        参数:
        - ids: 证据消息 ID
        - actor: 非空时要求所有来源属于该成员
        - current: 非空时要求证据包含本轮消息
        """
        if not isinstance(ids, list) or not 1 <= len(ids) <= 10 or any(not isinstance(item, str) or not item for item in ids):
            raise MemoryError("invalid_source", "需要 1 至 10 条有效来源消息")
        if len(set(ids)) != len(ids) or current and current not in ids:
            raise MemoryError("invalid_source", "来源不能重复且必须包含本轮请求消息")
        for identity in ids:
            record = self.archive.get(self.scope, identity)
            if (record is None or record.get("status") != "active" or not record.get("verified")
                    or record.get("direction") != "inbound" or actor and record.get("sender_id") != actor):
                raise MemoryError("invalid_source", "来源不可用, 不属于当前群或不是该成员的发言")

    def _lookup(self, connection: sqlite3.Connection, memory_id: str) -> sqlite3.Row:
        """
        按完整 ID 在当前范围定位记录

        参数:
        - connection: 当前事务连接
        - memory_id: 完整记忆 ID

        返回:
        - 当前范围内的记录, 缺失时抛出业务错误
        """
        row = connection.execute("SELECT * FROM memories WHERE scope=? AND id=?", (self.scope.key, memory_id)).fetchone()
        if row is None:
            raise MemoryError("not_found", "记忆不存在或不属于当前群")
        return row

    def _source_ids(self, connection: sqlite3.Connection, memory_id: str) -> list[str]:
        """
        在同一事务连接内读取记忆的来源消息 ID

        参数:
        - connection: 当前事务连接
        - memory_id: 完整记忆 ID

        返回:
        - 按消息 ID 排序的来源列表
        """
        return [str(item[0]) for item in connection.execute("SELECT message_id FROM memory_refs WHERE scope_key=? AND memory_id=? ORDER BY message_id", (self.scope.key, memory_id))]

    def _present(self, row: sqlite3.Row, ids: list[str]) -> dict[str, Any]:
        """
        按此刻的档案状态组装公共记录, 可在读事务结束后调用

        参数:
        - row: 已限定范围的数据库记录
        - ids: 同一读事务内取得的来源消息 ID

        返回:
        - 不含内部路径或原文副本的记忆记录
        """
        available = all((source := self.archive.get(self.scope, identity)) is not None and source.get("status") == "active" for identity in ids)
        return {"schema_version": 1, "memory_id": row["id"], "id": row["id"], "kind": row["kind"],
                "owner_user_id": row["owner_user_id"], "key": row["purpose_key"], "title": row["title"],
                "content": row["content"], "tags": json.loads(row["tags"]), "importance": row["importance"],
                "revision": row["revision"], "state": "active", "origin": row["origin"],
                "source_message_ids": ids, "source_status": "available" if ids and available else "operator" if not ids and row["origin"] == "operator" else "unavailable",
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def _record(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        """
        构造带所有权和来源状态的公共记录

        参数:
        - connection: 当前读取连接
        - row: 已限定范围的数据库记录

        返回:
        - 不含内部路径或原文副本的记忆记录
        """
        return self._present(row, self._source_ids(connection, row["id"]))

    @staticmethod
    def _revision(row: sqlite3.Row, revision: object) -> None:
        """
        拒绝覆盖已经发生变化的记录

        参数:
        - row: 当前真实记录
        - revision: 调用方读到的修订号
        """
        if type(revision) is not int or revision != row["revision"]:
            raise MemoryError("revision_conflict", "记忆已更新, 请重新查询后再修改")

    def get(self, memory_id: str) -> dict[str, Any]:
        """
        查看一条当前群记忆

        参数:
        - memory_id: 完整记忆 ID

        返回:
        - 记忆详情
        """
        with closing(self.store._connect()) as connection:
            return {"ok": True, "memory": self._record(connection, self._lookup(connection, memory_id))}

    def list(self, *, kind: str = "", user_id: str = "", keyword: str = "", limit: int = 20, cursor: str = "", viewer: str = "") -> dict[str, Any]:
        """
        在当前群内确定性筛选和分页, 变化后的游标必须重查

        参数:
        - kind: 可选的记忆类型
        - user_id: 可选的成员所有者
        - keyword: 普通文本关键词, 按 Unicode 折叠在读取后匹配
        - limit: 返回条数, 1 至 50
        - cursor: 上次相同筛选返回的游标
        - viewer: 模型默认列表仅显示本群记忆和该成员偏好, 人工管理为空

        返回:
        - items, has_more 与 next_cursor
        """
        if kind not in {"", "group_rule", "member_preference"} or type(limit) is not int or not 1 <= limit <= 50:
            raise MemoryError("invalid_argument", "记忆类型或分页条数无效")
        if not isinstance(keyword, str) or len(keyword) > 256 or not isinstance(user_id, str) or len(user_id) > 256:
            raise MemoryError("invalid_argument", "筛选字段过长或类型错误")
        if not isinstance(cursor, str) or len(cursor) > 4096:
            raise MemoryError("invalid_cursor", "分页游标无效")
        clause, arguments = self._filters(kind=kind, user_id=user_id, viewer=viewer)
        with closing(self.store._connect()) as connection:
            # 签名, 当页正文与来源 ID 必须出自同一读事务; 来源状态在事务外按此刻的档案组装,
            # 不能在持有读锁时经 archive.get 新开连接, 否则会被等待提交的写方挡住
            connection.execute("BEGIN")
            if keyword:
                rows = connection.execute(f"SELECT * FROM memories WHERE {clause} ORDER BY {_MEMORY_ORDER}", arguments).fetchall()
                rows = [row for row in rows if keyword.casefold() in (row["title"] + "\n" + row["content"]).casefold()]
            else:
                rows = connection.execute(f"SELECT id, revision FROM memories WHERE {clause} ORDER BY {_MEMORY_ORDER}", arguments).fetchall()
            signature = hashlib.sha256(json.dumps([self.scope.key, kind, user_id, keyword, limit, viewer,
                [(row["id"], row["revision"]) for row in rows]], ensure_ascii=False).encode()).hexdigest()
            offset = self._offset(cursor, signature)
            if keyword:
                page = rows[offset:offset + limit]
            else:
                page = connection.execute(f"SELECT * FROM memories WHERE {clause} ORDER BY {_MEMORY_ORDER} LIMIT ? OFFSET ?",
                                          (*arguments, limit, offset)).fetchall()
            pending = [(row, self._source_ids(connection, str(row["id"]))) for row in page]
        items = [self._present(row, ids) for row, ids in pending]
        more = offset + limit < len(rows)
        next_cursor = base64.urlsafe_b64encode(json.dumps([signature, offset + limit]).encode()).decode() if more else None
        return {"ok": True, "items": items, "has_more": more, "next_cursor": next_cursor}

    def _filters(self, *, kind: str, user_id: str, viewer: str) -> tuple[str, tuple[str, ...]]:
        """
        生成当前群的 SQL 侧筛选条件

        参数:
        - kind: 可选的记忆类型
        - user_id: 可选的成员所有者
        - viewer: 非空时只保留本群记忆与该成员偏好

        返回:
        - WHERE 子句与位置参数, scope 固定在第一个条件
        """
        where = ["scope=?"]
        arguments = [self.scope.key]
        if viewer:
            where.append("(kind='group_rule' OR owner_user_id=?)")
            arguments.append(viewer)
        if kind:
            where.append("kind=?")
            arguments.append(kind)
        if user_id:
            where.extend(("kind='member_preference'", "owner_user_id=?"))
            arguments.append(user_id)
        return " AND ".join(where), tuple(arguments)

    @staticmethod
    def _offset(cursor: str, signature: str) -> int:
        """
        校验游标仍属于同一筛选与内容版本

        参数:
        - cursor: 上次返回的游标, 空串表示第一页
        - signature: 当前筛选与内容签名

        返回:
        - 当前页起始偏移, 游标无效或内容已变化时抛出 invalid_cursor
        """
        if not cursor:
            return 0
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if token[0] != signature or type(token[1]) is not int or token[1] < 0:
                raise ValueError()
            return int(token[1])
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise MemoryError("invalid_cursor", "筛选或记忆内容已变化, 请重新查询") from exc

    def mutate(self, operation: str, values: dict[str, Any], *, actor: str, current_message: str = "", operator: bool = False, operation_id: str) -> dict[str, Any]:
        """
        事务保存本人偏好或群记忆提案, 人工管理可直接应用

        参数:
        - operation: create, update 或 delete
        - values: 已限定字段的操作参数
        - actor: 宿主确认的发起者
        - current_message: 当前可信来源消息
        - operator: 是否由已认证管理入口调用
        - operation_id: 绑定当前范围的幂等标识

        返回:
        - saved, deleted, already_exists 或 pending 及真实记录状态
        """
        if operation not in {"create", "update", "delete"} or not actor or not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 512:
            raise MemoryError("invalid_argument", "记忆操作或发起者无效")
        fingerprint = hashlib.sha256(json.dumps([operation, values, actor, operator], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            old_op = connection.execute("SELECT * FROM memory_operations WHERE scope_key=? AND operation_id=?", (self.scope.key, operation_id)).fetchone()
            if old_op is not None:
                if old_op["fingerprint"] != fingerprint:
                    raise MemoryError("idempotency_conflict", "同一操作标识不能提交不同内容")
                result = json.loads(old_op["result_json"])
                if result.get("memory_id") and result.get("status") != "deleted":
                    result["memory"] = self._record(connection, self._lookup(connection, result["memory_id"]))
                return result
            self._expire(connection)
            row = self._lookup(connection, str(values.get("memory_id", ""))) if operation != "create" else None
            if row is not None:
                self._revision(row, values.get("expected_revision"))
                kind, owner, key = row["kind"], row["owner_user_id"], row["purpose_key"]
                if not operator and kind == "member_preference" and owner != actor:
                    raise MemoryError("not_owner", "只能修改你本人在当前群的偏好")
            else:
                kind = values.get("kind")
                owner = str(values.get("owner_user_id", "")) if operator else actor if kind == "member_preference" else ""
                key = values.get("key", "")
                if not isinstance(key, str):
                    raise MemoryError("invalid_argument", "记忆用途键必须是文字")
                key = unicodedata.normalize("NFC", key.strip())
                if kind not in {"member_preference", "group_rule"} or not 1 <= len(key) <= 64 or any(ord(char) < 32 for char in key):
                    raise MemoryError("invalid_argument", "记忆类型或用途键无效")
                if kind == "group_rule":
                    owner = ""
                if kind == "member_preference" and not owner:
                    raise MemoryError("invalid_argument", "成员偏好需要明确所有者")
            content = values.get("content", row["content"] if row is not None else "")
            title = values.get("title", row["title"] if row is not None else key)
            if operation != "delete" and (not isinstance(content, str) or not 1 <= len(content.strip()) <= 2000 or not isinstance(title, str) or not 1 <= len(title.strip()) <= 120):
                raise MemoryError("invalid_argument", "标题需要 1 至 120 字, 正文需要 1 至 2000 字")
            sources = values.get("source_message_ids", [])
            if not operator:
                if operation == "delete":
                    if values.get("request_message_id") != current_message:
                        raise MemoryError("invalid_source", "删除请求必须来自本轮消息")
                    sources = [current_message]
                self._sources(sources, actor if kind == "member_preference" else "", current_message)
            elif sources:
                self._sources(sources)
            existing = connection.execute("SELECT * FROM memories WHERE scope=? AND kind=? AND owner_user_id=? AND purpose_key=?", (self.scope.key, kind, owner, key)).fetchone() if operation == "create" else None
            if existing is not None:
                return {"ok": True, "status": "already_exists", "memory": self._record(connection, existing)}
            payload = {"title": title, "content": content, "kind": kind, "owner_user_id": owner, "key": key, "source_message_ids": sources}
            if kind == "group_rule" and not operator:
                if connection.execute("SELECT COUNT(*) FROM memory_proposals WHERE scope_key=? AND state='pending'", (self.scope.key,)).fetchone()[0] >= 50:
                    raise MemoryError("quota_exceeded", "当前群待审批提案已达上限")
                proposal = "mp_" + uuid.uuid4().hex
                now = self.archive._clock()
                connection.execute("INSERT INTO memory_proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL)",
                                   (proposal, self.scope.key, operation, row["id"] if row else None, key, row["revision"] if row else 0, json.dumps(payload, ensure_ascii=False), actor, now, now + 86400))
                result = {"ok": True, "status": "pending", "proposal_id": proposal, "operation": operation, "base_revision": row["revision"] if row else 0}
            else:
                result = self._apply(connection, operation, row, payload, "operator" if operator else "model", actor=actor)
            minimal = {key: value for key, value in result.items() if key != "memory"}
            connection.execute("INSERT INTO memory_operations VALUES (?, ?, ?, ?)", (self.scope.key, operation_id, fingerprint, json.dumps(minimal, ensure_ascii=False)))
            return result

    def _apply(self, connection: sqlite3.Connection, operation: str, row: sqlite3.Row | None, payload: dict[str, Any], origin: str, *, actor: str) -> dict[str, Any]:
        """
        在现有事务中应用经过授权的变化

        参数:
        - connection: 已取得写权限的事务
        - operation: create, update 或 delete
        - row: 更新或删除的当前记录
        - payload: 已核验的拟修改内容
        - origin: model 或 operator
        - actor: 已核验的操作发起者, 人工审批记录管理入口身份

        返回:
        - 已保存或已删除的真实结果
        """
        if operation == "delete":
            assert row is not None
            erase_memory(connection, self.scope.key, row["id"], actor=actor, now=self.archive._clock())
            return {"ok": True, "status": "deleted", "memory_id": row["id"], "revision": row["revision"] + 1}
        now = datetime.fromtimestamp(self.archive._clock(), timezone.utc).isoformat()
        if operation == "create":
            count = connection.execute("SELECT COUNT(*) FROM memories WHERE scope=? AND kind=? AND owner_user_id=?", (self.scope.key, payload["kind"], payload["owner_user_id"])).fetchone()[0]
            if count >= (200 if payload["kind"] == "group_rule" else 50):
                raise MemoryError("quota_exceeded", "当前范围的长期记忆已达上限")
            memory_id = "mm_" + uuid.uuid4().hex
            connection.execute("INSERT INTO memories (id, scope, title, content, tags, importance, created_at, updated_at, kind, owner_user_id, purpose_key, revision, origin) "
                               "VALUES (?, ?, ?, ?, '[]', 1, ?, ?, ?, ?, ?, 1, ?)",
                               (memory_id, self.scope.key, payload["title"].strip(), payload["content"].strip(), now, now, payload["kind"], payload["owner_user_id"], payload["key"], origin))
        else:
            assert row is not None
            memory_id = row["id"]
            connection.execute("UPDATE memories SET title=?, content=?, revision=revision+1, updated_at=?, origin=? WHERE scope=? AND id=?",
                               (payload["title"].strip(), payload["content"].strip(), now, origin, self.scope.key, memory_id))
        connection.execute("DELETE FROM memory_refs WHERE scope_key=? AND memory_id=?", (self.scope.key, memory_id))
        connection.executemany("INSERT INTO memory_refs VALUES (?, ?, ?)", [(memory_id, self.scope.key, identity) for identity in payload["source_message_ids"]])
        self._audit(connection, memory_id, actor, operation)
        return {"ok": True, "status": "saved", "memory_id": memory_id, "memory": self._record(connection, self._lookup(connection, memory_id))}

    def _audit(self, connection: sqlite3.Connection, memory_id: str, actor: str, operation: str) -> None:
        """
        记录不含正文的操作审计, 删除不留下内容副本

        参数:
        - connection: 当前事务
        - memory_id: 记忆 ID
        - actor: 已核验操作者
        - operation: 实际应用的操作
        """
        connection.execute("INSERT INTO memory_audit VALUES (?, ?, ?, ?, ?, ?)",
                           (uuid.uuid4().hex, self.scope.key, memory_id, actor, operation, self.archive._clock()))

    def _expire(self, connection: sqlite3.Connection) -> None:
        """
        标记并清除过期待审批正文

        参数:
        - connection: 当前事务连接
        """
        connection.execute("UPDATE memory_proposals SET state='expired', payload_json='{}', decision_at=? WHERE scope_key=? AND state='pending' AND expires_at<=?",
                           (self.archive._clock(), self.scope.key, self.archive._clock()))

    def proposals(self) -> dict[str, Any]:
        """
        查看当前群提案, 不将待审内容当成有效记忆

        返回:
        - 按时间倒序的有界提案列表
        """
        with closing(self.store._connect()) as connection, connection:
            self._expire(connection)
            items = []
            for row in connection.execute("SELECT * FROM memory_proposals WHERE scope_key=? ORDER BY created_at DESC LIMIT 100", (self.scope.key,)):
                item = dict(row)
                item["proposed"] = json.loads(item.pop("payload_json"))
                item.pop("scope_key")
                items.append(item)
            return {"ok": True, "items": items}

    def decide(self, proposal_id: str, approve: bool, expected_revision: int) -> dict[str, Any]:
        """
        在同一事务中审批并应用变化, 冲突不覆盖当前值

        参数:
        - proposal_id: 当前群提案 ID
        - approve: 是否批准
        - expected_revision: 界面读到的基准记忆修订

        返回:
        - approved, rejected, expired 或 conflicted
        """
        if type(approve) is not bool or type(expected_revision) is not int:
            raise MemoryError("invalid_argument", "审批决定和修订无效")
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection)
            proposal = connection.execute("SELECT * FROM memory_proposals WHERE scope_key=? AND proposal_id=?", (self.scope.key, proposal_id)).fetchone()
            if proposal is None:
                raise MemoryError("not_found", "记忆提案不存在")
            if proposal["state"] != "pending":
                return {"ok": True, "status": proposal["state"], "proposal_id": proposal_id}
            if expected_revision != proposal["base_revision"]:
                raise MemoryError("revision_conflict", "审批基准与提案不一致")
            result: dict[str, Any] = {}
            state = "rejected"
            if approve:
                payload = json.loads(proposal["payload_json"])
                row = connection.execute("SELECT * FROM memories WHERE scope=? AND id=?", (self.scope.key, proposal["memory_id"])).fetchone() if proposal["memory_id"] else None
                collision = connection.execute("SELECT 1 FROM memories WHERE scope=? AND kind='group_rule' AND purpose_key=?", (self.scope.key, proposal["purpose_key"])).fetchone() if proposal["operation"] == "create" else None
                if collision or proposal["operation"] != "create" and (row is None or row["revision"] != proposal["base_revision"]):
                    state = "conflicted"
                else:
                    self._sources(payload["source_message_ids"])
                    result = self._apply(connection, proposal["operation"], row, payload, "model", actor="authenticated_operator")
                    state = "approved"
            connection.execute("UPDATE memory_proposals SET state=?, decision_at=?, payload_json='{}' WHERE scope_key=? AND proposal_id=?",
                               (state, self.archive._clock(), self.scope.key, proposal_id))
            return {**result, "ok": True, "status": state, "proposal_id": proposal_id}
