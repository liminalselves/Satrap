"""
平台待处理申请的宿主私有收件箱

原始 flag 只用于向适配器提交动作, 不进入模型结果和审批审计账本;
收件箱保存有限期限的申请原值, 执行资格始终由独立持久账本复核
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

    def __init__(self, path: Path | None, ttl: float) -> None:
        """
        初始化私有申请存储, 持久数据库按需打开

        参数:
        - path: 持久库位置, None 使用隔离内存库
        - ttl: 与审批账本一致的有效秒数
        """
        self.path, self.ttl = path, ttl
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
                assert self._memory is not None
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
                    yield connection
            finally:
                if self.path is not None:
                    connection.close()

    def register(self, adapter_id: str, self_id: str, kind: str, flag: str, *, group_id: str,
                 sub_type: str, user_id: str, comment: str, received_at: float, now: float) -> None:
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
        """
        if not isinstance(kind, str) or not kind or len(kind) > 64 or not flag or len(flag) > 4096 or not isinstance(comment, str):
            raise ValueError("申请原值或类别无效")
        with self._transaction() as connection:
            connection.execute("DELETE FROM requests WHERE expires_at<=?", (now,))
            identity = (adapter_id, self_id, kind, flag_digest(kind, self_id, flag))
            if connection.execute("SELECT 1 FROM requests WHERE adapter_id=? AND self_id=? AND kind=? AND digest=?", identity).fetchone():
                return
            if connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= 16384:
                raise ValueError("申请收件箱容量已满")
            if received_at + self.ttl <= now:
                return
            connection.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                               ("rq_" + uuid.uuid4().hex, *identity, flag, group_id, sub_type, user_id,
                                comment[:2000], received_at, received_at + self.ttl))

    def rows(self, adapter_id: str, self_id: str, kind: str, now: float) -> list[dict[str, Any]]:
        """
        返回当前账号的有限内部行, 原值仅供宿主复核

        参数:
        - adapter_id: 平台实例
        - self_id: 当前账号
        - kind: 申请类别
        - now: 当前时间

        返回:
        - 尚未到期的申请内部行
        """
        with self._transaction() as connection:
            connection.execute("DELETE FROM requests WHERE expires_at<=?", (now,))
            rows = connection.execute("SELECT * FROM requests WHERE adapter_id=? AND self_id=? AND kind=? "
                                      "ORDER BY received_at DESC,request_id LIMIT 16384", (adapter_id, self_id, kind)).fetchall()
            return [dict(row) for row in rows]

    def resolve(self, adapter_id: str, self_id: str, kind: str, request_id: str, now: float) -> dict[str, Any]:
        """
        用不透明申请 ID 定位当前账号的原值, 不跨账号和类别回退

        参数:
        - adapter_id: 平台实例
        - self_id: 当前账号
        - kind: 申请类别
        - request_id: 查询工具返回的申请 ID
        - now: 当前时间

        返回:
        - 供宿主执行的内部行, 不存在或到期时抛 LookupError
        """
        with self._transaction() as connection:
            connection.execute("DELETE FROM requests WHERE expires_at<=?", (now,))
            row = connection.execute("SELECT * FROM requests WHERE request_id=? AND adapter_id=? AND self_id=? AND kind=?",
                                     (request_id, adapter_id, self_id, kind)).fetchone()
            if row is None:
                raise LookupError("申请不存在, 已过期或不属于当前账号")
            return dict(row)

    def discard(self, request_ids: list[str]) -> None:
        """
        清除已失去执行资格的申请原值, 保留独立账本的不可重放记录

        参数:
        - request_ids: 已确认终态的收件箱 ID
        """
        if request_ids:
            with self._transaction() as connection:
                connection.executemany("DELETE FROM requests WHERE request_id=?", ((identity,) for identity in request_ids))

    def forget(self, adapter_id: str, self_id: str, kind: str, flag: str) -> None:
        """
        动作终态已落盘后清除对应原值

        参数:
        - adapter_id: 平台实例
        - self_id: 机器人账号
        - kind: 申请类别
        - flag: 已消费的平台原值
        """
        with self._transaction() as connection:
            connection.execute("DELETE FROM requests WHERE adapter_id=? AND self_id=? AND kind=? AND digest=?",
                               (adapter_id, self_id, kind, flag_digest(kind, self_id, flag)))

    def purge(self, now: float) -> None:
        """
        空闲维护时删除到期原值, 不修改权威账本

        参数:
        - now: 当前时间
        """
        with self._transaction() as connection:
            connection.execute("DELETE FROM requests WHERE expires_at<=?", (now,))

    def close(self) -> None:
        """释放隔离内存库, 持久库没有长期连接"""
        with self._mutex:
            if self._memory is not None:
                self._memory.close()
                self._memory = None
