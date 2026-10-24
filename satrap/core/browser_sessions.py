"""按部署和服务隔离的持久浏览器登录凭据"""
from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Callable


REMEMBER_SECONDS = 30 * 86400
RENEW_INTERVAL = 86400
DASHBOARD_NAMESPACES = ('api', 'backend', 'control', 'chat')


class PersistentBrowserSessions:
    """仅保存随机凭据摘要, 支持跨进程恢复和撤销"""

    def __init__(self, path: Path, namespace: str, scope: str, now: Callable[[], float] = time.time) -> None:
        """
        绑定持久凭据范围, 构造时不创建文件

        参数:
        - path: credentials 中的专用数据库
        - namespace: 当前服务名称
        - scope: 部署凭据摘要
        - now: UTC 秒数提供函数
        """
        self.path = path
        self.namespace = namespace
        self.scope = scope
        self.now = now

    def issue(self, password_version: str) -> str:
        """
        签发三十天有效的随机凭据并清理过期记录

        参数:
        - password_version: 当前访问密码版本

        返回:
        - 仅返回给浏览器的原始随机凭据
        """
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        session = 'p_' + secrets.token_urlsafe(32)
        now = self.now()
        with closing(sqlite3.connect(self.path, timeout=3)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS browser_sessions (digest TEXT PRIMARY KEY, namespace TEXT NOT NULL, scope TEXT NOT NULL, password_version TEXT NOT NULL, created_at REAL NOT NULL, renewed_at REAL NOT NULL, expires_at REAL NOT NULL)')
            connection.execute('DELETE FROM browser_sessions WHERE expires_at <= ?', (now,))
            connection.execute('DELETE FROM browser_sessions WHERE namespace=? AND scope=? AND digest IN (SELECT digest FROM browser_sessions WHERE namespace=? AND scope=? ORDER BY renewed_at DESC LIMIT -1 OFFSET 1023)', (self.namespace, self.scope, self.namespace, self.scope))
            connection.execute('INSERT INTO browser_sessions VALUES(?, ?, ?, ?, ?, ?, ?)', (hashlib.sha256(session.encode()).hexdigest(), self.namespace, self.scope, password_version, now, now, now + REMEMBER_SECONDS))
        return session

    def validate(self, session: str, password_version: str) -> bool:
        """
        只读核验凭据, 不在普通 API 请求中续期或写库

        参数:
        - session: Cookie 中的随机凭据
        - password_version: 当前访问密码版本

        返回:
        - 范围, 版本与有效期均匹配时为 True
        """
        if not session.startswith('p_') or not self.path.is_file():
            return False
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=3)) as connection:
            row = connection.execute('SELECT expires_at FROM browser_sessions WHERE digest=? AND namespace=? AND scope=? AND password_version=?', (hashlib.sha256(session.encode()).hexdigest(), self.namespace, self.scope, password_version)).fetchone()
        return row is not None and float(row[0]) > self.now()

    def renew(self, session: str, password_version: str) -> bool:
        """
        仅在恢复登录且距上次续期超过一天时延长有效期

        参数:
        - session: 当前有效凭据
        - password_version: 当前访问密码版本

        返回:
        - 本次发生续期时为 True
        """
        if not self.path.is_file():
            return False
        now = self.now()
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=3)) as connection:
            row = connection.execute('SELECT renewed_at FROM browser_sessions WHERE digest=? AND namespace=? AND scope=? AND password_version=? AND expires_at>?', (hashlib.sha256(session.encode()).hexdigest(), self.namespace, self.scope, password_version, now)).fetchone()
        if row is None or float(row[0]) > now - RENEW_INTERVAL:
            return False
        with closing(sqlite3.connect(self.path, timeout=3)) as connection, connection:
            changed = connection.execute('UPDATE browser_sessions SET renewed_at=?, expires_at=? WHERE digest=? AND namespace=? AND scope=? AND password_version=? AND expires_at>? AND renewed_at<=?', (now, now + REMEMBER_SECONDS, hashlib.sha256(session.encode()).hexdigest(), self.namespace, self.scope, password_version, now, now - RENEW_INTERVAL)).rowcount
        return bool(changed)

    def revoke(self, session: str) -> bool:
        """
        撤销当前服务中的单个凭据

        参数:
        - session: 待撤销的原始凭据

        返回:
        - 实际删除记录时为 True
        """
        if not self.path.is_file():
            return False
        with closing(sqlite3.connect(self.path, timeout=3)) as connection, connection:
            return bool(connection.execute('DELETE FROM browser_sessions WHERE digest=? AND namespace=? AND scope=?', (hashlib.sha256(session.encode()).hexdigest(), self.namespace, self.scope)).rowcount)

    def validate_related(self, session: str, namespace: str, password_version: str) -> bool:
        """
        核验同部署其它管理服务的持久凭据, 仅用于显式登录恢复

        参数:
        - session: 浏览器携带的其它服务凭据
        - namespace: 对应 Cookie 的服务名称
        - password_version: 当前访问密码版本

        返回:
        - 同部署, 同密码版本且未过期时为 True
        """
        if self.namespace not in DASHBOARD_NAMESPACES or namespace not in DASHBOARD_NAMESPACES:
            return False
        return PersistentBrowserSessions(self.path, namespace, self.scope, self.now).validate(session, password_version)

    def revoke_related(self, sessions: list[str]) -> bool:
        """
        一次撤销本浏览器携带的同部署管理凭据, 包含暂未运行的服务

        参数:
        - sessions: 当前请求中已知服务 Cookie 的原始值

        返回:
        - 至少撤销一条记录时为 True
        """
        if not sessions or not self.path.is_file() or self.namespace not in DASHBOARD_NAMESPACES:
            return False
        digests = [hashlib.sha256(session.encode()).hexdigest() for session in sessions]
        placeholders = ','.join('?' for _ in digests)
        realms = ','.join('?' for _ in DASHBOARD_NAMESPACES)
        with closing(sqlite3.connect(self.path, timeout=3)) as connection, connection:
            return bool(connection.execute(f'DELETE FROM browser_sessions WHERE scope=? AND namespace IN ({realms}) AND digest IN ({placeholders})', (self.scope, *DASHBOARD_NAMESPACES, *digests)).rowcount)
