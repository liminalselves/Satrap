from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, List, Optional, Type

from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.log import logger
from satrap.core.type import SessionConfig, UserCall, UserInfo


class UserInfoStore:
    """UserInfo 的 SQLite 持久化存储"""

    def __init__(self, db_path: str | Path | None = None):
        """初始化用户信息存储

        参数:
        - db_path: 数据库文件路径, 默认当前目录下的 .satrap/user_info.db
        """
        self._lock = threading.RLock()
        self.db_path = Path(db_path) if db_path else (Path.cwd() / ".satrap" / "user_info.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        """连接数据库

        返回:
        - 数据库连接对象 sqlite3.Connection
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self):
        """初始化用户信息表"""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_info (
                        user_id TEXT PRIMARY KEY,
                        user_platform TEXT NOT NULL,
                        user_nickname TEXT NOT NULL,
                        user_session TEXT NOT NULL
                    )
                    """
                )
                conn.commit()

    @staticmethod
    def _row_to_userinfo(row: sqlite3.Row) -> UserInfo:
        """将 SQLite 行转换为 UserInfo 实例
        
        参数:
        - row: SQLite 行数据
        """
        sessions: List[str] = []
        raw_sessions = row["user_session"]
        if raw_sessions:
            try:
                parsed = json.loads(raw_sessions)
                if isinstance(parsed, list):
                    sessions = [str(item) for item in parsed if item is not None]
            except Exception:
                sessions = []

        return UserInfo(
            user_id=row["user_id"],
            user_platform=row["user_platform"],
            user_nickname=row["user_nickname"],
            user_session=sessions,
        )

    @staticmethod
    def _userinfo_to_row(info: UserInfo) -> tuple[str, str, str, str]:
        """将 UserInfo 实例转换为 SQLite 参数行
        
        参数:
        - info: 用户信息实例
        """
        user_id = str(info.user_id or "").strip()
        user_platform = str(info.user_platform or "").strip()
        user_nickname = str(info.user_nickname or "").strip()
        user_session = list(info.user_session or [])
        return (
            user_id,
            user_platform,
            user_nickname,
            json.dumps(user_session, ensure_ascii=False),
        )

    def upsert(self, info: UserInfo):
        """插入或更新用户信息

        参数:
        - info: 用户信息实例
        """
        if not info.user_id:
            raise ValueError("user_id 不能为空")

        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO user_info
                    (user_id, user_platform, user_nickname, user_session)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        user_platform=excluded.user_platform,
                        user_nickname=excluded.user_nickname,
                        user_session=excluded.user_session
                    """,
                    self._userinfo_to_row(info),
                )
                conn.commit()

    def get(self, user_id: str) -> Optional[UserInfo]:
        """根据 user_id 查询用户信息

        参数:
        - user_id: 用户 ID
        """
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM user_info WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                if row is None:
                    return None
                return self._row_to_userinfo(row)

    def delete(self, user_id: str):
        """删除用户信息

        参数:
        - user_id: 用户 ID
        """
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM user_info WHERE user_id=?", (user_id,))
                conn.commit()

    def list(self, limit: int = 200) -> List[UserInfo]:
        """列出用户信息

        参数:
        - limit: 最大返回数量, 默认 200
        """
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM user_info
                    ORDER BY user_id ASC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
                return [self._row_to_userinfo(row) for row in rows]

    def add_session(self, user_id: str, session_id: str):
        """给用户追加一个 session_id, 幂等

        参数:
        - user_id: 用户 ID
        - session_id: 要追加的 session_id
        """
        with self._lock:
            info = self.get(user_id)
            if info is None:
                raise ValueError(f"user_id 不存在: {user_id}")
            sessions = list(info.user_session or [])
            if session_id not in sessions:
                sessions.append(session_id)
                info.user_session = sessions
                self.upsert(info)

    def remove_session(self, user_id: str, session_id: str):
        """移除用户的一个 session_id

        参数:
        - user_id: 用户 ID
        - session_id: 要移除的 session_id
        """
        with self._lock:
            info = self.get(user_id)
            if info is None:
                raise ValueError(f"user_id 不存在: {user_id}")
            sessions = [sid for sid in list(info.user_session or []) if sid != session_id]
            info.user_session = sessions
            self.upsert(info)

    def list_user_sessions(self, user_id: str) -> List[str]:
        """获取用户的 session_id 列表

        参数:
        - user_id: 用户 ID
        """
        with self._lock:
            info = self.get(user_id)
            if info is None:
                return []
            return list(info.user_session or [])


class UserManager:
    """用户管理器, 负责用户信息和用户-会话绑定关系"""

    def __init__(
        self,
        session_manager: SessionManager,
        db_path: str | Path | None = None,
        auto_create: bool = True,
    ):
        """初始化用户管理器

        参数:
        - session_manager: 共享的 SessionManager 实例
        - db_path: 用户信息数据库路径, 默认 .satrap/user_info.db
        - auto_create: 预留开关, 兼容自动创建用户场景
        """
        self.sm = session_manager
        self.store = UserInfoStore(db_path=db_path)
        self.auto_create = bool(auto_create)
        self._lock = threading.RLock()

    # ---------------- 用户信息 ----------------
    def get_or_create_user(
        self,
        user_id: str,
        platform: str = "",
        nickname: str = "",
    ) -> UserInfo:
        """查询用户, 不存在则创建并持久化

        参数:
        - user_id: 用户 ID
        - platform: 平台名称
        - nickname: 昵称
        """
        with self._lock:
            existed = self.store.get(user_id)
            if existed is not None:
                changed = False
                if platform and existed.user_platform != platform:
                    existed.user_platform = platform
                    changed = True
                if nickname and existed.user_nickname != nickname:
                    existed.user_nickname = nickname
                    changed = True
                if changed:
                    self.store.upsert(existed)
                return existed

            created = UserInfo(
                user_id=user_id,
                user_platform=platform or "",
                user_nickname=nickname or "",
                user_session=[],
            )
            self.store.upsert(created)
            return created

    def get_user(self, user_id: str) -> Optional[UserInfo]:
        """查询用户信息"""
        try:
            return self.store.get(user_id)
        except Exception as e:
            logger.error(f"[UserManager] get_user 失败: user_id={user_id}, 错误={e}")
            return None

    def update_user(self, user_id: str, **kwargs: Any) -> bool:
        """更新用户基础信息

        支持字段:
        - user_platform
        - user_nickname
        """
        with self._lock:
            try:
                info = self.store.get(user_id)
                if info is None:
                    return False

                changed = False
                if "user_platform" in kwargs and kwargs["user_platform"] is not None:
                    info.user_platform = str(kwargs["user_platform"])
                    changed = True
                if "user_nickname" in kwargs and kwargs["user_nickname"] is not None:
                    info.user_nickname = str(kwargs["user_nickname"])
                    changed = True

                if changed:
                    self.store.upsert(info)
                return changed
            except Exception as e:
                logger.error(f"[UserManager] update_user 失败: user_id={user_id}, 错误={e}")
                return False

    def delete_user(self, user_id: str) -> bool:
        """删除用户信息, 不删除会话本身

        参数:
        - user_id: 用户 ID
        """
        with self._lock:
            try:
                info = self.store.get(user_id)
                if info is None:
                    return False
                self.store.delete(user_id)
                return True
            except Exception as e:
                logger.error(f"[UserManager] delete_user 失败: user_id={user_id}, 错误={e}")
                return False

    def list_users(self, limit: int = 200) -> List[UserInfo]:
        """列出所有用户

        参数:
        - limit: 最大返回数量, 默认 200
        """
        try:
            return self.store.list(limit=limit)
        except Exception as e:
            logger.error(f"[UserManager] list_users 失败: 错误={e}")
            return []

    # ---------------- 用户-会话关联 ----------------
    def bind_session(self, user_id: str, session_id: str) -> bool:
        """将 session_id 绑定到用户, 幂等操作

        参数:
        - user_id: 用户 ID
        - session_id: 会话 ID
        """
        with self._lock:
            try:
                if not self.store.get(user_id):
                    return False
                self.store.add_session(user_id=user_id, session_id=session_id)
                return True
            except Exception as e:
                logger.error(
                    f"[UserManager] bind_session 失败: user_id={user_id}, session_id={session_id}, 错误={e}"
                )
                return False

    def unbind_session(self, user_id: str, session_id: str) -> bool:
        """从用户解绑一个 session_id

        参数:
        - user_id: 用户 ID
        - session_id: 会话 ID
        """
        with self._lock:
            try:
                if not self.store.get(user_id):
                    return False
                self.store.remove_session(user_id=user_id, session_id=session_id)
                return True
            except Exception as e:
                logger.error(
                    f"[UserManager] unbind_session 失败: user_id={user_id}, session_id={session_id}, 错误={e}"
                )
                return False

    def get_user_session_ids(self, user_id: str) -> List[str]:
        """获取用户绑定的 session_id 列表

        参数:
        - user_id: 用户 ID
        """
        try:
            return self.store.list_user_sessions(user_id)
        except Exception as e:
            logger.error(f"[UserManager] get_user_session_ids 失败: user_id={user_id}, 错误={e}")
            return []

    def get_user_sessions(self, user_id: str) -> List[SessionConfig]:
        """获取用户绑定的 SessionConfig 列表

        参数:
        - user_id: 用户 ID
        """
        session_ids = self.get_user_session_ids(user_id)
        if not session_ids:
            return []

        result: List[SessionConfig] = []
        for sid in session_ids:
            try:
                cfg = self.sm.get_session_config(sid)
                if cfg is not None:
                    result.append(cfg)
            except Exception as e:
                logger.error(
                    f"[UserManager] get_user_sessions 读取会话失败: user_id={user_id}, session_id={sid}, 错误={e}"
                )
        return result

    def unbind_orphan_sessions(self, user_id: str) -> int:
        """清理已不存在的过期 session_id 绑定

        参数:
        - user_id: 用户 ID
        """
        with self._lock:
            removed = 0
            try:
                session_ids = self.get_user_session_ids(user_id)
                for sid in session_ids:
                    cfg = self.sm.get_session_config(sid)
                    if cfg is None:
                        if self.unbind_session(user_id=user_id, session_id=sid):
                            removed += 1
                return removed
            except Exception as e:
                logger.error(f"[UserManager] unbind_orphan_sessions 失败: user_id={user_id}, 错误={e}")
                return removed

    # ---------------- 快捷创建 ----------------
    def create_user_session(
        self,
        user_id: str,
        session_class: Type[Session] | Type[AsyncSession],
        session_type_name: str | None = None,
        session_config: Optional[dict[str, Any]] = None,
    ) -> str:
        """创建会话并绑定到用户

        参数:
        - user_id: 用户 ID
        - session_class: 会话类
        - session_type_name: 会话类型名称
        - session_config: 会话配置
        """
        with self._lock:
            try:
                self.get_or_create_user(user_id)
                cfg = self.sm.register_session(
                    session_class=session_class,
                    session_type_name=session_type_name,
                    session_config=session_config,
                )
                session_id = str(cfg.session_id or "")
                if not session_id:
                    return ""
                if not self.bind_session(user_id=user_id, session_id=session_id):
                    logger.error(
                        f"[UserManager] create_user_session 绑定失败: user_id={user_id}, session_id={session_id}"
                    )
                    return ""
                return session_id
            except Exception as e:
                logger.error(f"[UserManager] create_user_session 失败: user_id={user_id}, 错误={e}")
                return ""

    # ---------------- 消息路由 ----------------
    def route_call(self, user_call: UserCall, user_id: str) -> str:
        """按用户维度路由消息到 SessionManager

        参数:
        - user_call: 用户调用对象
        - user_id: 用户 ID
        """
        with self._lock:
            try:
                if not isinstance(user_call, UserCall):
                    logger.error("[UserManager] route_call 失败: user_call 必须是 UserCall 实例")
                    return ""

                self.get_or_create_user(user_id=user_id)

                if not user_call.session_id:
                    session_ids = self.get_user_session_ids(user_id=user_id)
                    if session_ids:
                        user_call.session_id = session_ids[0]

                return self.sm.handle_call(user_call)
            except Exception as e:
                logger.error(f"[UserManager] route_call 失败: user_id={user_id}, 错误={e}")
                return ""
