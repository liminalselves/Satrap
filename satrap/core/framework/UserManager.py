"""
用户与会话归属管理器

持久化平台用户资料及其会话列表, 根据用户调用选择或创建会话,
并协调 SessionManager 完成用户侧会话生命周期管理
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Type, cast

from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.storage import LOCAL_PLATFORM_ID, StorageLayout, StorageScope
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.providers.base import SESSION_CLASS_PROVIDER
from satrap.core.log import logger
from satrap.core.type import SessionConfig, UserCall, UserInfo
from satrap.core.utils.paths import get_db_path


@dataclass
class ContextSession:
    """上下文会话记录"""
    context_key: str = ""
    user_id: str = ""
    platform: str = ""
    provider_name: str = SESSION_CLASS_PROVIDER
    session_type: str = ""
    session_id: str = ""
    created_at: float = 0.0
    last_used_at: float = 0.0


class UserInfoStore:
    """UserInfo 的 SQLite 持久化存储"""

    def __init__(self, db_path: str | Path | None = None):
        """
        初始化用户信息存储

        参数:
        - db_path: 数据库文件路径, 默认 local 平台的 platform.db
        """
        self._lock = threading.RLock()
        self.db_path = Path(db_path) if db_path else Path(get_db_path())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        """
        连接数据库

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
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS context_sessions (
                        context_key TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        provider_name TEXT NOT NULL DEFAULT 'session_class',
                        session_type TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        last_used_at REAL NOT NULL
                    )
                    """
                )
                columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(context_sessions)").fetchall()
                }
                if "provider_name" not in columns:
                    conn.execute(
                        "ALTER TABLE context_sessions "
                        "ADD COLUMN provider_name TEXT NOT NULL DEFAULT 'session_class'"
                    )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cs_user ON context_sessions "
                    "(user_id, platform, provider_name, session_type)"
                )
                conn.commit()

    @staticmethod
    def _row_to_userinfo(row: sqlite3.Row) -> UserInfo:
        """
        将 SQLite 行转换为 UserInfo 实例

        参数:
        - row: SQLite 行数据

        返回:
        - UserInfo: 将 SQLite 行转换为 UserInfo 实例
        """
        sessions: List[str] = []
        raw_sessions = row["user_session"]
        if raw_sessions:
            try:
                parsed = json.loads(raw_sessions)
                if isinstance(parsed, list):
                    sessions = [str(item) for item in cast(list[Any], parsed) if item is not None]
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
        """
        将 UserInfo 实例转换为 SQLite 参数行

        参数:
        - info: 用户信息实例

        返回:
        - tuple[str, str, str, str]: 将 UserInfo 实例转换为 SQLite 参数行
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
        """
        插入或更新用户信息

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
        """
        根据 user_id 查询用户信息

        参数:
        - user_id: 用户 ID

        返回:
        - Optional[UserInfo]: 根据 user_id 查询用户信息
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
        """
        删除用户信息

        参数:
        - user_id: 用户 ID
        """
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM user_info WHERE user_id=?", (user_id,))
                conn.commit()

    def list(self, limit: int = 200) -> List[UserInfo]:
        """
        列出用户信息

        参数:
        - limit: 最大返回数量, 默认 200

        返回:
        - List[UserInfo]: 列出用户信息
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
        """
        给用户追加一个 session_id, 幂等

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
        """
        移除用户的一个 session_id

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

    def remove_session_references(self, session_ids: Iterable[str]) -> tuple[int, int]:
        """
        批量移除用户绑定和上下文路由中的会话引用

        参数:
        - session_ids: 待清理的会话 ID 集合

        返回:
        - tuple[int, int]: 更新的用户数和删除的上下文路由数
        """
        targets = {str(session_id).strip() for session_id in session_ids if str(session_id).strip()}
        if not targets:
            return 0, 0
        with self._lock:
            with self._connect() as conn:
                updated_users = 0
                rows = conn.execute("SELECT user_id, user_session FROM user_info").fetchall()
                for row in rows:
                    try:
                        parsed: object = json.loads(row["user_session"] or "[]")
                    except Exception:
                        parsed = []
                    sessions = [
                        str(item)
                        for item in cast(list[Any], parsed)
                    ] if isinstance(parsed, list) else []
                    remaining = [session_id for session_id in sessions if session_id not in targets]
                    if remaining != sessions:
                        conn.execute(
                            "UPDATE user_info SET user_session=? WHERE user_id=?",
                            (json.dumps(remaining, ensure_ascii=False), row["user_id"]),
                        )
                        updated_users += 1
                placeholders = ",".join("?" for _ in targets)
                cursor = conn.execute(
                    f"DELETE FROM context_sessions WHERE session_id IN ({placeholders})",
                    tuple(sorted(targets)),
                )
                conn.commit()
                return updated_users, max(cursor.rowcount, 0)

    def list_user_sessions(self, user_id: str) -> List[str]:
        """
        获取用户的 session_id 列表

        参数:
        - user_id: 用户 ID

        返回:
        - List[str]: 用户的 session_id 列表
        """
        with self._lock:
            info = self.get(user_id)
            if info is None:
                return []
            return list(info.user_session or [])

    # ---------- 上下文会话 ----------

    @staticmethod
    def _context_key(
        user_id: str,
        platform: str,
        session_type: str,
        provider_name: str,
    ) -> str:
        """
        构建上下文会话唯一键

        参数:
        - user_id: 用户 ID
        - platform: 平台名称
        - session_type: 命名会话类型
        - provider_name: Provider 名称

        返回:
        - str: 兼容旧 session_class 数据的上下文键
        """
        if provider_name == SESSION_CLASS_PROVIDER:
            return f"{session_type}:{platform}:{user_id}"
        return f"{provider_name}:{session_type}:{platform}:{user_id}"

    def upsert_context_session(
        self,
        user_id: str,
        platform: str,
        session_type: str,
        session_id: str,
        provider_name: str = SESSION_CLASS_PROVIDER,
    ) -> str:
        """
        创建或更新上下文会话记录

        参数:
        - user_id: 用户 ID
        - platform: 平台名称
        - session_type: 会话类型
        - session_id: 会话 ID
        - provider_name: Provider 名称

        返回: context_key (格式: "{session_type}:{platform}:{user_id}")

        返回:
        - str: 创建或更新上下文会话记录
        """
        context_key = self._context_key(user_id, platform, session_type, provider_name)
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO context_sessions
                    (context_key, user_id, platform, provider_name, session_type, session_id, created_at, last_used_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(context_key) DO UPDATE SET
                        session_id=excluded.session_id,
                        provider_name=excluded.provider_name,
                        last_used_at=excluded.last_used_at
                    """,
                    (context_key, user_id, platform, provider_name, session_type, session_id, now, now),
                )
                conn.commit()
        return context_key

    def get_context_session(
        self,
        user_id: str,
        platform: str,
        session_type: str,
        provider_name: str = SESSION_CLASS_PROVIDER,
    ) -> Optional[ContextSession]:
        """
        查询上下文会话记录

        参数:
        - user_id: 用户 ID
        - platform: 平台名称
        - session_type: 会话类型
        - provider_name: Provider 名称

        返回:
        - Optional[ContextSession]: 查询上下文会话记录
        """
        context_key = self._context_key(user_id, platform, session_type, provider_name)
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM context_sessions WHERE context_key=?",
                    (context_key,),
                ).fetchone()
                if row is None:
                    return None
                return ContextSession(
                    context_key=row["context_key"],
                    user_id=row["user_id"],
                    platform=row["platform"],
                    provider_name=row["provider_name"],
                    session_type=row["session_type"],
                    session_id=row["session_id"],
                    created_at=row["created_at"],
                    last_used_at=row["last_used_at"],
                )

    def delete_context_session(
        self,
        user_id: str,
        platform: str,
        session_type: str,
        provider_name: str = SESSION_CLASS_PROVIDER,
    ):
        """
        删除上下文会话记录

        参数:
        - user_id: 用户 ID
        - platform: 平台名称
        - session_type: 会话类型
        - provider_name: Provider 名称
        """
        context_key = self._context_key(user_id, platform, session_type, provider_name)
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM context_sessions WHERE context_key=?", (context_key,))
                conn.commit()


class UserManager:
    """用户管理器, 负责用户信息和用户-会话绑定关系"""

    def __init__(
        self,
        session_manager: SessionManager,
        db_path: str | Path | None = None,
        auto_create: bool = True,
        platform_id: str = LOCAL_PLATFORM_ID,
        storage_layout: StorageLayout | None = None,
    ):
        """
        初始化用户管理器

        参数:
        - session_manager: 共享的 SessionManager 实例
        - db_path: 用户信息数据库路径, 默认 local 平台的 platform.db
        - auto_create: 是否允许自动创建用户, 关闭后未知用户不落库
        - platform_id: 此管理器所属的平台实例 ID
        - storage_layout: 可选 v2 数据布局
        """
        self.sm = session_manager
        self.store = UserInfoStore(db_path=db_path)
        self.auto_create = bool(auto_create)
        self.platform_id = platform_id.strip() or LOCAL_PLATFORM_ID
        self.storage_layout = storage_layout
        self._lock = threading.RLock()

    # ---------- 用户信息 ----------
    def get_or_create_user(
        self,
        user_id: str,
        platform: str = "",
        nickname: str = "",
    ) -> Optional[UserInfo]:
        """
        查询用户; auto_create 开启时不存在则创建并持久化

        参数:
        - user_id: 用户 ID
        - platform: 平台名称
        - nickname: 昵称

        返回:
        - UserInfo: 用户信息; auto_create=False 且用户不存在时返回 None
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
                if self.storage_layout is not None:
                    self.storage_layout.ensure_user(
                        StorageScope(platform_id=self.platform_id, user_id=user_id)
                    )
                return existed

            if not self.auto_create:
                return None

            created = UserInfo(
                user_id=user_id,
                user_platform=platform or "",
                user_nickname=nickname or "",
                user_session=[],
            )
            self.store.upsert(created)
            if self.storage_layout is not None:
                self.storage_layout.ensure_user(
                    StorageScope(platform_id=self.platform_id, user_id=user_id)
                )
            return created

    def get_user(self, user_id: str) -> Optional[UserInfo]:
        """
        查询用户信息

        参数:
        - user_id: 用户 ID

        返回:
        - Optional[UserInfo]: 查询用户信息
        """
        try:
            return self.store.get(user_id)
        except Exception as e:
            logger.error(f"[UserManager] get_user 失败: user_id={user_id}, 错误={e}")
            return None

    def update_user(self, user_id: str, **kwargs: Any) -> bool:
        """
        更新用户基础信息

        参数:
        - user_id: 用户 ID
        - kwargs: 额外关键字参数

        支持字段:
        - user_platform
        - user_nickname

        返回:
        - bool: 更新用户基础信息
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
        """
        删除用户信息, 不删除会话本身

        参数:
        - user_id: 用户 ID

        返回:
        - bool: 删除用户信息, 不删除会话本身
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
        """
        列出所有用户

        参数:
        - limit: 最大返回数量, 默认 200

        返回:
        - List[UserInfo]: 列出所有用户
        """
        try:
            return self.store.list(limit=limit)
        except Exception as e:
            logger.error(f"[UserManager] list_users 失败: 错误={e}")
            return []

    # ---------- 用户-会话关联 ----------
    def bind_session(self, user_id: str, session_id: str) -> bool:
        """
        将 session_id 绑定到用户, 幂等操作

        参数:
        - user_id: 用户 ID
        - session_id: 会话 ID

        返回:
        - bool: 将 session_id 绑定到用户, 幂等操作
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
        """
        从用户解绑一个 session_id

        参数:
        - user_id: 用户 ID
        - session_id: 会话 ID

        返回:
        - bool: 从用户解绑一个 session_id
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
        """
        获取用户绑定的 session_id 列表

        参数:
        - user_id: 用户 ID

        返回:
        - List[str]: 用户绑定的 session_id 列表
        """
        try:
            return self.store.list_user_sessions(user_id)
        except Exception as e:
            logger.error(f"[UserManager] get_user_session_ids 失败: user_id={user_id}, 错误={e}")
            return []

    def get_user_sessions(self, user_id: str) -> List[SessionConfig]:
        """
        获取用户绑定的 SessionConfig 列表

        参数:
        - user_id: 用户 ID

        返回:
        - List[SessionConfig]: 用户绑定的 SessionConfig 列表
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
        """
        清理已不存在的过期 session_id 绑定

        参数:
        - user_id: 用户 ID

        返回:
        - int: 清理已不存在的过期 session_id 绑定
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

    def remove_session_references(self, session_ids: Iterable[str]) -> tuple[int, int]:
        """
        清理已删除会话的用户绑定和上下文路由

        参数:
        - session_ids: 已删除的会话 ID 集合

        返回:
        - tuple[int, int]: 更新的用户数和删除的上下文路由数
        """
        try:
            return self.store.remove_session_references(session_ids)
        except Exception as error:
            logger.error(f"[UserManager] remove_session_references 失败: {error}")
            return 0, 0

    # ---------- 快捷创建 ----------
    def create_user_session(
        self,
        user_id: str,
        session_class: Type[Session] | Type[AsyncSession],
        session_type_name: str | None = None,
        session_config: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        创建会话并绑定到用户

        参数:
        - user_id: 用户 ID
        - session_class: 会话类
        - session_type_name: 会话类型名称
        - session_config: 会话配置

        返回:
        - str: 创建会话并绑定到用户
        """
        with self._lock:
            try:
                if self.get_or_create_user(user_id) is None:
                    logger.warning(
                        f"[UserManager] create_user_session 跳过: 用户不存在且 auto_create=False, user_id={user_id}"
                    )
                    return ""
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

    # ---------- 上下文路由 ----------
    def resolve_session(
        self,
        user_id: str,
        platform: str,
        session_type: str = "",
        class_cfg_mgr: SessionClassConfigManager | None = None,
        extra_params: Optional[Dict[str, Any]] = None,
        session_provider: str = SESSION_CLASS_PROVIDER,
    ) -> str:
        """
        解析用户+平台到 session_id, 不存在则自动创建

        参数:
        - user_id: 用户 ID (如 Misskey 用户 ID)
        - platform: 平台标识 (如 "misskey")
        - session_type: 会话类型名称 (SessionClassConfigManager 中的注册名)
        - class_cfg_mgr: SessionClassConfigManager 实例 (自动创建时需要)
        - extra_params: 补充/覆盖 params
        - session_provider: 会话 Provider 名称

        返回: session_id (格式: "{session_type}:{platform}:{user_id}")

        返回:
        - str: 解析用户+平台到 session_id, 不存在则自动创建
        """
        if self.get_or_create_user(user_id=user_id, platform=platform) is None:
            logger.warning(
                f"[UserManager] resolve_session 跳过: 用户不存在且 auto_create=False, "
                f"user_id={user_id}, platform={platform}"
            )
            return ""

        provider_name = session_provider.strip() or SESSION_CLASS_PROVIDER
        existed = self.store.get_context_session(
            user_id,
            platform,
            session_type,
            provider_name,
        )
        if existed is not None:
            return existed.session_id

        if not session_type:
            return ""

        if provider_name == SESSION_CLASS_PROVIDER and class_cfg_mgr is not None:
            self.sm.class_cfg_mgr = class_cfg_mgr
        session_id = self.sm.register_session_from_provider_context(
            provider_name=provider_name,
            definition_name=session_type,
            context_value=user_id,
            platform=platform,
            extra_params=extra_params,
        ).session_id or ""

        if session_id:
            if self.storage_layout is not None:
                self.storage_layout.ensure_session(
                    StorageScope(
                        platform_id=self.platform_id,
                        user_id=user_id,
                        session_id=session_id,
                    )
                )
            self.store.upsert_context_session(
                user_id,
                platform,
                session_type,
                session_id,
                provider_name,
            )
            self.bind_session(user_id=user_id, session_id=session_id)

        return session_id

    def update_context_session(
        self,
        user_id: str,
        platform: str,
        session_type: str,
        session_id: str,
        session_provider: str = SESSION_CLASS_PROVIDER,
    ):
        """
        更新上下文会话路由, 使该用户的后续消息路由到指定 session_id

        参数:
        - user_id: 用户 ID
        - platform: 平台标识
        - session_type: 会话类型名称
        - session_id: 目标会话 ID
        - session_provider: 会话 Provider 名称
        """
        try:
            self.store.upsert_context_session(
                user_id,
                platform,
                session_type,
                session_id,
                session_provider,
            )
        except Exception as e:
            logger.error(
                f"[UserManager] update_context_session 失败: "
                f"user_id={user_id}, platform={platform}, "
                f"session_type={session_type}, session_id={session_id}, 错误={e}"
            )

    def get_or_create_context(
        self,
        user_id: str,
        platform: str,
        session_type: str,
        class_cfg_mgr: SessionClassConfigManager,
        extra_params: Optional[Dict[str, Any]] = None,
        session_provider: str = SESSION_CLASS_PROVIDER,
    ) -> str:
        """
        获取或创建用户上下文 (resolve_session 的别名)

        参数:
        - user_id: 用户 ID
        - platform: 平台名称
        - session_type: 会话类型
        - class_cfg_mgr: 类cfgmgr
        - extra_params: extra参数集合
        - session_provider: 会话 Provider 名称

        返回:
        - str: 或创建用户上下文 (resolve_session 的别名)
        """
        return self.resolve_session(
            user_id=user_id,
            platform=platform,
            session_type=session_type,
            class_cfg_mgr=class_cfg_mgr,
            extra_params=extra_params,
            session_provider=session_provider,
        )

    # ---------- 消息路由 ----------
    def route_call(self, user_call: UserCall, user_id: str) -> str:
        """
        按用户维度路由消息到 SessionManager

        参数:
        - user_call: 用户调用对象
        - user_id: 用户 ID

        返回:
        - str: 按用户维度路由消息到 SessionManager
        """
        with self._lock:
            try:
                if self.get_or_create_user(user_id=user_id) is None:
                    logger.warning(
                        f"[UserManager] route_call 拒绝: 用户不存在且 auto_create=False, user_id={user_id}"
                    )
                    return ""

                if not user_call.session_id:
                    session_ids = self.get_user_session_ids(user_id=user_id)
                    if session_ids:
                        user_call.session_id = session_ids[0]

                return self.sm.handle_call(user_call)
            except Exception as e:
                logger.error(f"[UserManager] route_call 失败: user_id={user_id}, 错误={e}")
                return ""
