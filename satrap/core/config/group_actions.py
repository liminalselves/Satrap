"""逐群管理动作的幂等登记、审批占用与保守结果状态"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any
import hashlib
import json
import sqlite3
import time

from satrap.core.config.group_store import GroupConfigConflict, GroupConfigStore, _identity


ACTION_CAPACITY = 10000
ACTION_TTL = 600.0
TERMINAL_RETENTION = 30 * 86400.0


def action_fingerprint(self_id: str, group_id: str, action_type: str, params: Mapping[str, object],
                       actor_kind: str, model_origin: Mapping[str, str] | None = None) -> str:
    """计算同 ID 操作的稳定参数指纹, 不记录原始请求 flag"""
    parts: list[object] = [self_id, group_id, action_type, params, actor_kind]
    if model_origin is not None:
        parts.append(dict(model_origin))
    payload = json.dumps(
        parts,
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class GroupActionStore(GroupConfigStore):
    """把网络动作的执行权先在 SQLite 中原子占用"""

    def __init__(self, database: str | Path, *, recover: bool = False) -> None:
        super().__init__(database)
        if recover:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE group_actions SET state='unknown', result_json=? WHERE state='executing'",
                    (json.dumps({"reason": "interrupted_restart"}),),
                )
                connection.execute(
                    "UPDATE group_actions SET state='expired', decision_at=?, result_json=? "
                    "WHERE state='pending' AND action_type='handle_group_request'",
                    (time.time(), json.dumps({"reason": "request_flag_lost_on_restart"})),
                )
                connection.execute(
                    "UPDATE group_actions SET state='expired', decision_at=?, result_json=? "
                    "WHERE state='pending' AND actor_kind='model'",
                    (time.time(), json.dumps({"reason": "model_source_lost_on_restart"})),
                )

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        """把数据库记录转换为不包含原始请求 flag 的 API 形状"""
        return {
            "action_id": row["action_id"], "self_id": row["self_id"], "group_id": row["group_id"],
            "action_type": row["action_type"], "params": json.loads(row["params_json"]),
            "fingerprint": row["fingerprint"], "actor_kind": row["actor_kind"],
            "policy_revision": int(row["policy_revision"]), "state": row["state"],
            "created_at": float(row["created_at"]), "expires_at": row["expires_at"],
            "decision_at": row["decision_at"], "executed_at": row["executed_at"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }

    def submit(
        self, action_id: str, self_id: str, group_id: str, action_type: str,
        params: Mapping[str, object], actor_kind: str, policy_revision: int,
        *, approval_required: bool, model_origin: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """同 ID 同指纹返回既有记录, 新动作原子登记为 pending 或 executing"""
        _identity(self_id, group_id)
        if not isinstance(action_id, str) or not 8 <= len(action_id) <= 128 or not action_id.isascii() or not all(
            char.isalnum() or char in "-_" for char in action_id
        ):
            raise ValueError("action_id 必须是 8 到 128 字符的安全标识")
        if actor_kind not in {"panel", "model"}:
            raise ValueError("动作来源无效")
        encoded = json.dumps(dict(params), ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(encoded.encode("utf-8")) > 8192:
            raise ValueError("动作参数超过上限")
        encoded_origin = None
        if model_origin is not None:
            required = {"adapter_id", "self_id", "chat_type", "chat_id", "actor_id", "session_id", "tool_name"}
            valid_keys = {frozenset(required), frozenset(required | {"auth_fingerprint"})}
            if frozenset(model_origin) not in valid_keys or any(
                not isinstance(value, str) or len(value) > 256 for value in model_origin.values()
            ):
                raise ValueError("模型动作来源无效")
            encoded_origin = json.dumps(dict(model_origin), ensure_ascii=False, sort_keys=True)
        fingerprint = action_fingerprint(self_id, group_id, action_type, params, actor_kind, model_origin)
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM group_actions WHERE state IN ('succeeded','failed','rejected','expired') "
                "AND created_at<?",
                (now - TERMINAL_RETENTION,),
            )
            row = connection.execute("SELECT * FROM group_actions WHERE action_id=?", (action_id,)).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    raise GroupConfigConflict("相同 action_id 已用于不同动作")
                return self._record(row), False
            count = int(connection.execute("SELECT COUNT(*) FROM group_actions").fetchone()[0])
            if count >= ACTION_CAPACITY:
                raise RuntimeError("管理动作记录已达容量上限")
            state = "pending" if approval_required else "executing"
            connection.execute(
                "INSERT INTO group_actions (action_id, self_id, group_id, action_type, params_json, "
                "fingerprint, actor_kind, policy_revision, state, created_at, expires_at, model_origin_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (action_id, self_id, group_id, action_type, encoded, fingerprint, actor_kind,
                 policy_revision, state, now, now + ACTION_TTL if approval_required else None, encoded_origin),
            )
            row = connection.execute("SELECT * FROM group_actions WHERE action_id=?", (action_id,)).fetchone()
            if row is None:
                raise RuntimeError("动作登记后读取失败")
            return self._record(row), True

    def model_origin(self, self_id: str, group_id: str, action_id: str) -> dict[str, str] | None:
        """仅供执行授权复核读取不出现在动作 API 中的模型来源"""
        _identity(self_id, group_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT model_origin_json FROM group_actions WHERE self_id=? AND group_id=? AND action_id=?",
                (self_id, group_id, action_id),
            ).fetchone()
        if row is None or row["model_origin_json"] is None:
            return None
        decoded = json.loads(row["model_origin_json"])
        if not isinstance(decoded, dict) or any(not isinstance(value, str) for value in decoded.values()):
            raise RuntimeError("模型动作来源数据损坏")
        return decoded

    def get(self, self_id: str, group_id: str, action_id: str) -> dict[str, Any] | None:
        """按固定账号和群身份读取动作并处理自然到期"""
        _identity(self_id, group_id)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE group_actions SET state='expired', decision_at=? "
                "WHERE action_id=? AND self_id=? AND group_id=? AND state='pending' AND expires_at<=?",
                (time.time(), action_id, self_id, group_id, time.time()),
            )
            row = connection.execute(
                "SELECT * FROM group_actions WHERE action_id=? AND self_id=? AND group_id=?",
                (action_id, self_id, group_id),
            ).fetchone()
            return self._record(row) if row is not None else None

    def expire_account(self, self_id: str, reason: str) -> int:
        """账号解绑或平台删除后原子失效全部待审批动作"""
        _identity(self_id)
        if reason not in {"account_changed", "platform_deleted"}:
            raise ValueError("待审批失效原因无效")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE group_actions SET state='expired', decision_at=?, result_json=? "
                "WHERE self_id=? AND state='pending'",
                (time.time(), json.dumps({"reason": reason}), self_id),
            )
            return cursor.rowcount

    def list(
        self, self_id: str, group_id: str, *, state: str = "pending", page: int = 1, page_size: int = 25,
    ) -> dict[str, Any]:
        """在数据库内按固定群和状态筛选分页"""
        _identity(self_id, group_id)
        if state not in {"all", "pending", "executing", "succeeded", "failed", "unknown", "rejected", "expired"}:
            raise ValueError("动作状态筛选无效")
        if page < 1 or page_size not in {25, 50}:
            raise ValueError("动作分页参数无效")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE group_actions SET state='expired', decision_at=? "
                "WHERE self_id=? AND group_id=? AND state='pending' AND expires_at<=?",
                (time.time(), self_id, group_id, time.time()),
            )
            where = "self_id=? AND group_id=?"
            args: list[object] = [self_id, group_id]
            if state != "all":
                where += " AND state=?"
                args.append(state)
            total = int(connection.execute(f"SELECT COUNT(*) FROM group_actions WHERE {where}", args).fetchone()[0])
            rows = connection.execute(
                f"SELECT * FROM group_actions WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*args, page_size, (page - 1) * page_size),
            ).fetchall()
        return {"items": [self._record(row) for row in rows], "total": total,
                "page": page, "page_size": page_size}

    def decide(self, self_id: str, group_id: str, action_id: str, *, approve: bool, policy_revision: int) -> dict[str, Any]:
        """在一个事务内比较待审批状态和策略版本并占用执行权"""
        _identity(self_id, group_id)
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM group_actions WHERE action_id=? AND self_id=? AND group_id=?",
                (action_id, self_id, group_id),
            ).fetchone()
            if row is None:
                raise LookupError("管理动作不存在")
            if row["state"] != "pending":
                raise GroupConfigConflict("管理动作状态已变化")
            if row["expires_at"] <= now:
                connection.execute(
                    "UPDATE group_actions SET state='expired', decision_at=? WHERE action_id=?",
                    (now, action_id),
                )
                connection.commit()
                raise GroupConfigConflict("管理动作已过期")
            if int(row["policy_revision"]) != policy_revision:
                connection.execute(
                    "UPDATE group_actions SET state='expired', decision_at=?, result_json=? WHERE action_id=?",
                    (now, json.dumps({"reason": "policy_changed"}), action_id),
                )
                connection.commit()
                raise GroupConfigConflict("审批策略或账号状态已变化")
            connection.execute(
                "UPDATE group_actions SET state=?, decision_at=? WHERE action_id=?",
                ("executing" if approve else "rejected", now, action_id),
            )
            row = connection.execute("SELECT * FROM group_actions WHERE action_id=?", (action_id,)).fetchone()
            if row is None:
                raise RuntimeError("动作决策后读取失败")
            return self._record(row)

    def settle(self, self_id: str, group_id: str, action_id: str, state: str, reason: str,
               details: Mapping[str, object] | None = None) -> dict[str, Any]:
        """
        只允许执行中的动作进入终态, 持久保存有限的结果诊断字段

        参数:
        - self_id: 动作所属机器人账号
        - group_id: 动作所属群
        - action_id: 已占用的动作 ID
        - state: succeeded, failed 或 unknown 终态
        - reason: 稳定原因码
        - details: 可选的已确认消息 ID, 整数错误码和宿主生成的说明, 不接收平台原始响应

        返回:
        - 结算后的动作记录, 非执行状态或无效字段抛出明确错误
        """
        _identity(self_id, group_id)
        if state not in {"succeeded", "failed", "unknown"}:
            raise ValueError("动作终态无效")
        result: dict[str, object] = {"reason": reason}
        if details is not None:
            if not details or set(details) - {"message_ids", "retcode", "message"}:
                raise ValueError("动作结果字段无效")
            if "message_ids" in details:
                ids = details["message_ids"]
                if (not isinstance(ids, list) or len(ids) > 64
                        or any(not isinstance(item, str) or len(item) > 128 for item in ids)):
                    raise ValueError("动作结果字段无效")
            if "retcode" in details:
                code = details["retcode"]
                if type(code) is not int or not -(2 ** 31) <= code < 2 ** 31:
                    raise ValueError("动作结果错误码无效")
            if "message" in details:
                message = details["message"]
                if (not isinstance(message, str) or not 1 <= len(message) <= 512
                        or any(ord(char) < 32 for char in message)):
                    raise ValueError("动作结果说明无效")
            result.update(details)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE group_actions SET state=?, executed_at=?, result_json=? "
                "WHERE action_id=? AND self_id=? AND group_id=? AND state='executing'",
                (state, time.time(), json.dumps(result), action_id, self_id, group_id),
            )
            if cursor.rowcount != 1:
                raise GroupConfigConflict("动作执行状态已变化")
            row = connection.execute("SELECT * FROM group_actions WHERE action_id=?", (action_id,)).fetchone()
            if row is None:
                raise RuntimeError("动作结算后读取失败")
            return self._record(row)
