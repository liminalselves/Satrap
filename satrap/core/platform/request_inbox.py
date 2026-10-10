"""
平台待处理申请的宿主私有收件箱

原始 flag 只用于向适配器提交动作, 不进入模型结果和审批审计账本;
本地到期后归档申请, 凭据与历史分别按保留期限清理; 执行资格由独立持久账本复核
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
import sqlite3
import threading
import uuid
import os
import hashlib
from time import time


def flag_digest(kind: str, self_id: str, flag: str) -> str:
    """
    计算申请标识的分域摘要, 不在审计记录中保留原始标识

    参数:
    - kind: 适配器定义的申请类别
    - self_id: 已绑定的机器人账号
    - flag: 平台上报的申请原值

    返回:
    - 32 位十六进制摘要, 实例身份由账本键另行绑定
    """
    return hashlib.sha256(f"{kind}\x00{self_id}\x00{flag}".encode("utf-8")).hexdigest()[:32]


class RequestInbox:
    """按适配器, 账号和申请类别隔离的有限收件箱"""

    def __init__(self, path: Path | None, ttl: float, *, credential_days: int = 30, history_days: int = 90) -> None:
        """
        初始化私有申请存储, 持久数据库按需打开

        参数:
        - path: 持久库位置, None 使用隔离内存库
        - ttl: 与账本一致的本地近期列表秒数, 到期只归档
        - credential_days: 未处理凭据保留天数, 默认 30
        - history_days: 历史保留天数, 默认 90, 不得短于凭据期限
        """
        self.path, self.ttl = path, ttl
        if type(credential_days) is not int or type(history_days) is not int or not 1 <= credential_days <= history_days <= 3650:
            raise ValueError("申请凭据和历史保留天数必须为 1 到 3650, 历史期限不能短于凭据期限")
        self.credential_days, self.history_days = credential_days, history_days
        self._mutex = threading.RLock()
        self._memory = sqlite3.connect(":memory:", check_same_thread=False) if path is None else None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """
        在短事务中读取或修改收件箱, 路径不对外暴露

        返回:
        - 当前 SQLite 连接
        """
        with self._mutex:
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.is_symlink() or self.path.parent.resolve() != self.path.parent.absolute():
                    raise ValueError("申请收件箱不是受管理目录")
                connection = sqlite3.connect(self.path, timeout=5)
            else:
                if self._memory is None:
                    raise RuntimeError("申请收件箱已关闭")
                connection = self._memory
            connection.row_factory = sqlite3.Row
            try:
                if self.path is not None and os.name != "nt":
                    os.chmod(self.path, 0o600)
                with connection:
                    connection.execute("PRAGMA secure_delete=ON")
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("CREATE TABLE IF NOT EXISTS requests("
                                       "request_id TEXT PRIMARY KEY,adapter_id TEXT NOT NULL,self_id TEXT NOT NULL,"
                                       "kind TEXT NOT NULL,digest TEXT NOT NULL,flag TEXT NOT NULL,"
                                       "group_id TEXT NOT NULL,sub_type TEXT NOT NULL,user_id TEXT NOT NULL,"
                                       "comment TEXT NOT NULL,received_at REAL NOT NULL,expires_at REAL NOT NULL,"
                                       "UNIQUE(adapter_id,self_id,kind,digest))")
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(requests)")}
                    for name, declaration in {
                        "revision": "INTEGER NOT NULL DEFAULT 1", "archived_at": "REAL",
                        "platform_state": "TEXT NOT NULL DEFAULT 'unknown'",
                        "execution_state": "TEXT NOT NULL DEFAULT 'not_started'", "last_checked_at": "REAL",
                        "decision": "TEXT", "credential_expires_at": "REAL NOT NULL DEFAULT 0",
                        "request_category": "TEXT NOT NULL DEFAULT 'normal'", "nickname": "TEXT NOT NULL DEFAULT ''",
                        "request_source": "TEXT NOT NULL DEFAULT ''", "suspicious_reason": "TEXT NOT NULL DEFAULT ''",
                        "requested_at": "INTEGER",
                    }.items():
                        if name not in columns:
                            connection.execute(f"ALTER TABLE requests ADD COLUMN {name} {declaration}")
                    connection.execute("UPDATE requests SET credential_expires_at=received_at+? WHERE credential_expires_at=0",
                                       (self.credential_days * 86400,))
                    connection.execute("CREATE TABLE IF NOT EXISTS request_archive_policy("
                                       "adapter_id TEXT NOT NULL,self_id TEXT NOT NULL,credential_days INTEGER NOT NULL,"
                                       "history_days INTEGER NOT NULL,PRIMARY KEY(adapter_id,self_id))")
                    yield connection
            finally:
                if self.path is not None:
                    connection.close()

    def register(self, adapter_id: str, self_id: str, kind: str, flag: str, *, group_id: str,
                 sub_type: str, user_id: str, comment: str, received_at: float, now: float, identity_digest: str | None = None,
                 details: dict[str, Any] | None = None, restore_missing: bool = True) -> bool:
        """
        为已经在账本登记的申请保存原值, 重复事件不延长有效期

        参数:
        - adapter_id: 来源平台实例
        - self_id: 已确认机器人账号
        - kind: 适配器确认的申请类别
        - flag: 平台申请原值, 不得写日志
        - group_id: 群申请目标
        - sub_type: 群申请子类型
        - user_id: 申请人
        - comment: 验证信息, 最多 2000 字符
        - received_at: 账本固定的首次接收时间
        - now: 当前时间, 用于清理过期原值
        - identity_digest: 账本的一次申请身份, None 沿用旧版凭据摘要
        - details: 平台确认的申请类别和展示字段, 不包含模型推断
        - restore_missing: 是否允许补回缺失详情, 已占用或终态只更新已有展示字段

        返回:
        - 是否新增且保留了详情, 已有记录不更新身份或凭据期限
        """
        if not isinstance(kind, str) or not kind or len(kind) > 64 or not flag or len(flag) > 4096 or not isinstance(comment, str):
            raise ValueError("申请原值或类别无效")
        metadata = dict(details or {})
        allowed = {"request_category", "nickname", "request_source", "suspicious_reason", "requested_at"}
        if set(metadata) - allowed or metadata.get("request_category", "normal") not in {"normal", "suspicious"}:
            raise ValueError("申请详情字段或类别无效")
        if any(not isinstance(metadata[key], str) or len(metadata[key]) > 2000 for key in metadata.keys() & (allowed - {"requested_at"})):
            raise ValueError("申请展示字段无效")
        stamp = metadata.get("requested_at")
        if stamp is not None and (type(stamp) is not int or stamp <= 0):
            raise ValueError("申请原始时间无效")
        with self._transaction() as connection:
            self._maintain(connection, now)
            identity = (adapter_id, self_id, kind, identity_digest or flag_digest(kind, self_id, flag))
            existing = connection.execute("SELECT * FROM requests WHERE adapter_id=? AND self_id=? AND kind=? AND digest=?", identity).fetchone()
            if existing:
                changed = {key: value for key, value in metadata.items() if existing[key] != value}
                if changed:
                    assignments = ",".join(f"{key}=?" for key in sorted(changed))
                    connection.execute(f"UPDATE requests SET {assignments},revision=revision+1 WHERE request_id=?",
                                       (*[changed[key] for key in sorted(changed)], existing["request_id"]))
                return False
            if not restore_missing:
                return False
            if connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= 16384:
                raise ValueError("申请收件箱容量已满")
            policy = connection.execute("SELECT credential_days FROM request_archive_policy WHERE adapter_id=? AND self_id=?",
                                        (adapter_id, self_id)).fetchone()
            credential_days = policy[0] if policy else self.credential_days
            connection.execute("INSERT INTO requests(request_id,adapter_id,self_id,kind,digest,flag,group_id,sub_type,user_id,"
                               "comment,received_at,expires_at,credential_expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               ("rq_" + uuid.uuid4().hex, *identity, flag, group_id, sub_type, user_id,
                                comment[:2000], received_at, received_at + self.ttl, received_at + credential_days * 86400))
            if metadata:
                assignments = ",".join(f"{key}=?" for key in sorted(metadata))
                connection.execute(f"UPDATE requests SET {assignments} WHERE adapter_id=? AND self_id=? AND kind=? AND digest=?",
                                   (*[metadata[key] for key in sorted(metadata)], *identity))
            self._maintain(connection, now)
            return connection.execute("SELECT 1 FROM requests WHERE adapter_id=? AND self_id=? AND kind=? AND digest=?", identity).fetchone() is not None

    def _maintain(self, connection: sqlite3.Connection, now: float) -> None:
        """本地到期只归档, 凭据和历史按各自期限清理"""
        connection.execute("UPDATE requests SET archived_at=?,revision=revision+1 WHERE expires_at<=? AND archived_at IS NULL",
                           (now, now))
        connection.execute("UPDATE requests SET flag='',revision=revision+1 WHERE credential_expires_at<=? AND flag!=''", (now,))
        connection.execute("DELETE FROM requests WHERE received_at<=?-86400*COALESCE((SELECT history_days "
                           "FROM request_archive_policy p WHERE p.adapter_id=requests.adapter_id AND p.self_id=requests.self_id),?)",
                           (now, self.history_days))

    def policy(self, adapter_id: str, self_id: str, values: dict[str, Any] | None = None) -> dict[str, int]:
        """按平台和机器人账号保存凭据与历史的独立保留期限"""
        with self._transaction() as connection:
            if values is not None:
                if set(values) != {"credential_days", "history_days"}:
                    raise ValueError("申请保留配置字段无效")
                credential, history = values["credential_days"], values["history_days"]
                if type(credential) is not int or type(history) is not int or not 1 <= credential <= history <= 3650:
                    raise ValueError("保留天数必须为 1 到 3650, 历史期限不能短于凭据期限")
                connection.execute("INSERT INTO request_archive_policy VALUES(?,?,?,?) ON CONFLICT(adapter_id,self_id) "
                                   "DO UPDATE SET credential_days=excluded.credential_days,history_days=excluded.history_days",
                                   (adapter_id, self_id, credential, history))
                connection.execute("UPDATE requests SET credential_expires_at=received_at+?,revision=revision+1 "
                                   "WHERE adapter_id=? AND self_id=? AND credential_expires_at!=received_at+?",
                                   (credential * 86400, adapter_id, self_id, credential * 86400))
            row = connection.execute("SELECT credential_days,history_days FROM request_archive_policy WHERE adapter_id=? AND self_id=?",
                                     (adapter_id, self_id)).fetchone()
            return dict(row) if row else {"credential_days": self.credential_days, "history_days": self.history_days}

    def rows(self, adapter_id: str, self_id: str, kind: str, now: float) -> list[dict[str, Any]]:
        """
        返回当前账号的有限内部行, 原值仅供宿主复核

        参数:
        - adapter_id: 平台实例
        - self_id: 当前账号
        - kind: 申请类别
        - now: 当前时间

        返回:
        - 保留期限内的申请内部行, 包括归档和终态
        """
        with self._transaction() as connection:
            self._maintain(connection, now)
            rows = connection.execute("SELECT * FROM requests WHERE adapter_id=? AND self_id=? AND kind=? "
                                      "ORDER BY received_at DESC,request_id LIMIT 16384", (adapter_id, self_id, kind)).fetchall()
            return [dict(row) for row in rows]

    def resolve(self, adapter_id: str, self_id: str, kind: str, request_id: str, now: float,
                expected_revision: int | None = None) -> dict[str, Any]:
        """
        用不透明申请 ID 定位当前账号的原值, 不跨账号和类别回退

        参数:
        - adapter_id: 平台实例
        - self_id: 当前账号
        - kind: 申请类别
        - request_id: 查询工具返回的申请 ID
        - now: 当前时间

        返回:
        - 宿主内部行, 不存在时抛 LookupError, 修订冲突时抛 ValueError
        """
        with self._transaction() as connection:
            self._maintain(connection, now)
            row = connection.execute("SELECT * FROM requests WHERE request_id=? AND adapter_id=? AND self_id=? AND kind=?",
                                     (request_id, adapter_id, self_id, kind)).fetchone()
            if row is None:
                raise LookupError("申请不存在或不属于当前账号")
            if expected_revision is not None and (type(expected_revision) is not int or row["revision"] != expected_revision):
                raise ValueError("申请记录已变化, 请重新查询后处理")
            return dict(row)

    def discard(self, request_ids: list[str]) -> None:
        """
        清除已失去执行资格的申请原值, 保留独立账本的不可重放记录

        参数:
        - request_ids: 已确认终态的收件箱 ID
        """
        if request_ids:
            with self._transaction() as connection:
                connection.executemany("UPDATE requests SET flag='',archived_at=COALESCE(archived_at,received_at),"
                                       "execution_state='unknown',revision=revision+1 WHERE request_id=? AND flag!=''",
                                       ((identity,) for identity in request_ids))

    def forget(self, adapter_id: str, self_id: str, kind: str, flag: str, *, state: str = "completed",
               decision: str | None = None, now: float | None = None, identity_digest: str | None = None) -> None:
        """
        动作终态已落盘后清除对应原值

        参数:
        - adapter_id: 平台实例
        - self_id: 机器人账号
        - kind: 申请类别
        - flag: 已消费的平台原值
        - state: completed 或 unknown
        - decision: accepted, rejected 或无明确业务结果的 None
        - now: 结算时间, 默认当前时间
        - identity_digest: 实际占用的申请身份, 不清除同凭据的新申请
        """
        with self._transaction() as connection:
            moment = time() if now is None else now
            connection.execute("UPDATE requests SET flag='',archived_at=COALESCE(archived_at,?),execution_state=?,"
                               "platform_state=?,decision=?,last_checked_at=?,revision=revision+1 "
                               "WHERE adapter_id=? AND self_id=? AND kind=? AND digest=?",
                               (moment, "unknown" if state == "unknown" else "succeeded" if decision else "failed",
                                "unknown" if state == "unknown" or not decision else "processed", decision, moment,
                                adapter_id, self_id, kind, identity_digest or flag_digest(kind, self_id, flag)))

    def annotate(self, request_id: str, *, platform_state: str, now: float) -> None:
        """记录实际核验结果, 状态没有变化时不增加修订号"""
        if platform_state not in {"pending", "processed", "invalid", "unknown"}:
            raise ValueError("申请平台状态无效")
        with self._transaction() as connection:
            connection.execute("UPDATE requests SET platform_state=?,last_checked_at=?,"
                               "revision=revision+CASE WHEN platform_state!=? THEN 1 ELSE 0 END WHERE request_id=?",
                               (platform_state, now, platform_state, request_id))

    def archive(self, request_id: str, now: float, *, clear_credential: bool = False) -> None:
        """持久归档账本中的非活动申请, 终态凭据立即清除"""
        with self._transaction() as connection:
            connection.execute("UPDATE requests SET archived_at=COALESCE(archived_at,?),"
                               "flag=CASE WHEN ? THEN '' ELSE flag END,revision=revision+1 "
                               "WHERE request_id=? AND (archived_at IS NULL OR (? AND flag!=''))",
                               (now, clear_credential, request_id, clear_credential))

    def delete(self, adapter_id: str, self_id: str, kind: str, request_id: str, expected_revision: int, now: float) -> None:
        """仅删除已归档且修订号匹配的历史, 不删除防重放账本"""
        with self._transaction() as connection:
            self._maintain(connection, now)
            if connection.execute("DELETE FROM requests WHERE request_id=? AND adapter_id=? AND self_id=? AND kind=? "
                                  "AND revision=? AND archived_at IS NOT NULL",
                                  (request_id, adapter_id, self_id, kind, expected_revision)).rowcount != 1:
                raise ValueError("归档记录不存在或已变化, 请刷新后重试")

    def purge(self, now: float) -> None:
        """
        空闲维护时归档近期到期申请并清理超出保留期限的凭据与历史, 不修改权威账本

        参数:
        - now: 当前时间
        """
        with self._transaction() as connection:
            self._maintain(connection, now)

    def close(self) -> None:
        """释放隔离内存库, 持久库没有长期连接"""
        with self._mutex:
            if self._memory is not None:
                self._memory.close()
                self._memory = None
