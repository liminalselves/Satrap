"""好友动作与账号保护策略的持久存储, 不使用群身份"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any
import json
import sqlite3
import time

from satrap.core.config.platform_schema import ensure_platform_tables
from satrap.core.friends import FriendError


class FriendStore:
    """用短事务登记和占用动作, 进程恢复时拒绝重放未确认写操作"""

    def __init__(self, path: Path) -> None:
        """
        创建平台共用数据库中的好友表并恢复中断动作

        参数:
        - path: 宿主管理的平台数据库路径
        """
        self.path = path
        with self.connection() as conn:
            conn.execute("UPDATE friend_actions SET state='unknown', result_json=? WHERE state='executing'",
                         (json.dumps({"reason": "process_interrupted", "message": "执行被中断, 结果未确认"}),))
            conn.execute("UPDATE friend_actions SET state='expired' WHERE state='pending' AND actor_kind='model'")
            conn.execute("UPDATE friend_actions SET state='failed', result_json=? WHERE state='ready'",
                         (json.dumps({"message": "进程重启前尚未执行, 请重新发起操作"}),))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """
        打开带结构检查的短事务

        返回:
        - 当前 SQLite 连接
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                ensure_platform_tables(conn)
                yield conn
        finally:
            conn.close()

    @staticmethod
    def record(row: sqlite3.Row) -> dict[str, Any]:
        """
        还原不含协议凭据的动作

        参数:
        - row: 持久行

        返回:
        - 公开动作字段
        """
        result = dict(row)
        result["target"] = json.loads(result.pop("target_json")) if row["target_json"] else None
        result["params"] = json.loads(result.pop("params_json"))
        result["result"] = json.loads(result.pop("result_json")) if row["result_json"] else None
        return result

    def register(self, account: str, action_id: str, action: str, params: dict[str, Any], actor: str,
                 target: dict[str, Any] | None = None, actor_id: str = "panel", *,
                 requires_approval: bool = False) -> tuple[dict[str, Any], bool]:
        """
        幂等登记人工或模型动作

        参数:
        - account: 固定机器人账号
        - action_id: 调用方保留的幂等 ID
        - action: 好友动作类型
        - params: 已校验的公开参数
        - actor: panel 或 model
        - target: 来自平台核验的目标资料
        - actor_id: 宿主固定的实际发起者

        返回:
        - 动作与是否首次登记, 相同 ID 不同内容时拒绝
        """
        encoded = json.dumps(params, sort_keys=True, ensure_ascii=False)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM friend_actions WHERE self_id=? AND action_id=?", (account, action_id)).fetchone()
            if row:
                if row["action_type"] != action or row["params_json"] != encoded or row["actor_kind"] != actor or row["actor_id"] != actor_id:
                    raise FriendError("action_conflict", "相同动作 ID 已用于不同操作")
                return self.record(row), False
            now = time.time()
            conn.execute("UPDATE friend_actions SET state='expired' WHERE self_id=? AND state='pending' AND expires_at<=?", (account, now))
            if action == "delete_friend":
                previous = conn.execute("SELECT params_json FROM friend_actions WHERE self_id=? AND action_type=? "
                                        "AND state IN ('pending','ready','executing','unknown')", (account, action)).fetchall()
                if any(json.loads(row[0]).get("user_id") == params["user_id"] for row in previous):
                    raise FriendError("unresolved_action", "此好友已有待处理或结果未知的删除动作, 请先核查原动作")
            if action == "send_request":
                previous = conn.execute("SELECT params_json FROM friend_actions WHERE self_id=? AND action_type=? "
                                        "AND (state IN ('pending','ready','executing','unknown') OR (state='succeeded' AND created_at>?))",
                                        (account, action, now - 600)).fetchall()
                if any(json.loads(row[0]).get("user_id") == params["user_id"] for row in previous):
                    raise FriendError("unresolved_action", "此账号已有刚提交或结果未知的好友申请, 请先核查原动作")
            state = "pending" if actor == "model" and (action == "delete_friend" or requires_approval) else "ready"
            conn.execute("INSERT INTO friend_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (account, action_id, action, encoded, actor, state, now, now + 600, None, None, None,
                          json.dumps(target, ensure_ascii=False) if target else None, actor_id))
            row = conn.execute("SELECT * FROM friend_actions WHERE self_id=? AND action_id=?", (account, action_id)).fetchone()
            assert row is not None
            return self.record(row), True

    def get(self, account: str, action_id: str) -> dict[str, Any]:
        """
        查询当前账号的动作并结算到期申请

        参数:
        - account: 固定账号
        - action_id: 动作 ID

        返回:
        - 动作记录, 不存在时拒绝
        """
        with self.connection() as conn:
            conn.execute("UPDATE friend_actions SET state='expired' WHERE self_id=? AND state='pending' AND expires_at<=?", (account, time.time()))
            row = conn.execute("SELECT * FROM friend_actions WHERE self_id=? AND action_id=?", (account, action_id)).fetchone()
            if row is None:
                raise FriendError("action_not_found", "动作不存在或不属于当前账号")
            return self.record(row)

    def transition(self, account: str, action_id: str, expected: str, state: str, result: dict[str, Any] | None = None) -> bool:
        """
        原子占用或结算动作, 并发点击只有一次生效

        参数:
        - account: 固定账号
        - action_id: 动作 ID
        - expected: 允许的旧状态
        - state: 新状态
        - result: 公开执行结果

        返回:
        - 是否实际更新
        """
        now = time.time()
        with self.connection() as conn:
            return conn.execute(
                "UPDATE friend_actions SET state=?, result_json=?, decision_at=CASE WHEN ?='pending' THEN ? ELSE decision_at END, "
                "executed_at=CASE WHEN ?='executing' THEN ? ELSE executed_at END WHERE self_id=? AND action_id=? AND state=? "
                "AND (?!='pending' OR expires_at>?)",
                (state, json.dumps(result, ensure_ascii=False) if result else None, expected, now, expected, now,
                 account, action_id, expected, expected, now),
            ).rowcount == 1

    def list(self, account: str, page: int, limit: int) -> dict[str, Any]:
        """
        分页读取动作审计

        参数:
        - account: 固定账号
        - page: 从 1 开始的页码
        - limit: 每页条数

        返回:
        - 当前页及总数
        """
        with self.connection() as conn:
            conn.execute("UPDATE friend_actions SET state='expired' WHERE self_id=? AND state='pending' AND expires_at<=?", (account, time.time()))
            rows = conn.execute("SELECT * FROM friend_actions WHERE self_id=? ORDER BY created_at DESC, action_id LIMIT ? OFFSET ?",
                                (account, limit, (page - 1) * limit)).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM friend_actions WHERE self_id=?", (account,)).fetchone()[0]
            return {"items": [self.record(row) for row in rows], "total": total, "page": page, "has_more": page * limit < total}

    def policy(self, account: str, protected: list[str] | None = None) -> list[str]:
        """
        读取或写入账号级受保护好友, 与模型插件开关无关

        参数:
        - account: 固定账号
        - protected: None 读取, 列表写入

        返回:
        - 当前保护名单
        """
        with self.connection() as conn:
            if protected is not None:
                conn.execute("INSERT INTO friend_policies VALUES(?,?) ON CONFLICT(self_id) DO UPDATE SET protected_json=excluded.protected_json",
                             (account, json.dumps(sorted(set(protected)), ensure_ascii=False)))
            row = conn.execute("SELECT protected_json FROM friend_policies WHERE self_id=?", (account,)).fetchone()
            return json.loads(row[0]) if row else []
