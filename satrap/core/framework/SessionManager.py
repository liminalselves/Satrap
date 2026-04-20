from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.log import logger
from satrap.core.type import SessionConfig, UserCall
from satrap.core.utils.context import AsyncContextManager, ContextManager


@dataclass
class SessionEntry:
    """会话池中的运行时条目(仅驻留内存中的活跃实例)"""

    session: Session | AsyncSession
    """会话实例"""
    session_type: str
    """会话类型名称"""
    created_at: float
    """会话创建时间戳"""
    last_used: float
    """会话最近使用时间戳"""


@dataclass
class SessionMetadata:
    """对外展示的活跃会话元信息"""

    session_id: str
    """会话ID"""
    session_type: str
    """会话类型名称"""
    created_at: float
    """会话创建时间戳"""
    last_used_at: float
    """会话最近使用时间戳"""
    message_count: int
    """会话已处理消息数量"""


class SessionRegistry:
    """会话类型注册表: 维护 `session_type_name -> 会话类` 的映射"""

    def __init__(self):
        self._mapping: Dict[str, Type[Session] | Type[AsyncSession]] = {}
        self._lock = threading.RLock()

    def register(self, session_type_name: str, session_class: Type[Session] | Type[AsyncSession]):
        """注册会话类型

        这里仅维护映射, 不做实例化; 实例化由 SessionManager 在真正需要时完成

        参数:
        - session_type_name: 会话类型名称
        - session_class: 会话类对象 (Session 或 AsyncSession)
        """
        if not session_type_name:
            logger.error("[SessionRegistry] 注册失败：session_type_name 不能为空")
            return
        if not inspect.isclass(session_class):
            logger.error("[SessionRegistry] 注册失败：session_class 必须是类对象")
            return
        if not issubclass(session_class, (Session, AsyncSession)):
            logger.error("[SessionRegistry] 注册失败：session_class 必须继承 Session 或 AsyncSession")
            return

        with self._lock:
            self._mapping[session_type_name] = session_class

    def get_class(self, session_type_name: str) -> Optional[Type[Session] | Type[AsyncSession]]:
        """根据会话类型名称获取会话类
        
        参数:
        - session_type_name: 会话类型名称
        
        返回:
        - 会话类对象 (Session 或 AsyncSession)
        """
        with self._lock:
            return self._mapping.get(session_type_name)

    def list_types(self) -> List[str]:
        """列出所有已注册的会话类型名称
        
        返回:
        - 会话类型名称列表
        """
        with self._lock:
            return list(self._mapping.keys())


class SessionConfigStore:
    """SessionConfig 的 SQLite 持久化存储

    表设计非常轻量, 仅保存会话配置与基础统计字段, 便于后续按 session_id 恢复实例
    """

    def __init__(self, db_path: str | Path | None = None):
        """初始化会话配置存储
        
        参数:
        - db_path: 数据库文件路径 (默认当前目录下的 .satrap/session_config.db)
        """
        self._lock = threading.RLock()
        self.db_path = Path(db_path) if db_path else (Path.cwd() / ".satrap" / "session_config.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        """连接数据库
        
        返回:
        - 数据库连接对象 (sqlite3.Connection)
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self):
        """初始化会话配置表
        
        确保数据库表存在, 并创建必要的索引
        """
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_configs (
                        session_id TEXT PRIMARY KEY,
                        session_type_name TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        last_used_at REAL NOT NULL,
                        message_count INTEGER NOT NULL,
                        session_config TEXT NOT NULL
                    )
                    """
                )
                conn.commit()

    @staticmethod
    def _row_to_config(row: sqlite3.Row) -> SessionConfig:
        """将 SQLite 行转换为 SessionConfig 实例
        
        参数:
        - row: SQLite 行数据 (sqlite3.Row)
        
        返回:
        - 会话配置 (SessionConfig)
        """
        payload: Dict[str, Any] = {}
        raw_payload = row["session_config"]
        if raw_payload:
            try:
                parsed = json.loads(raw_payload)
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = {}

        return SessionConfig(
            session_id=row["session_id"],
            session_type_name=row["session_type_name"],
            created_at=float(row["created_at"]),
            last_used_at=float(row["last_used_at"]),
            message_count=int(row["message_count"]),
            session_config=payload,
        )

    def upsert(self, config: SessionConfig):
        """插入或更新一条 SessionConfig 记录
        
        参数:
        - config: 会话配置 (SessionConfig)
        """
        if not config.session_id:
            raise ValueError("session_id 不能为空")

        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO session_configs
                    (session_id, session_type_name, created_at, last_used_at, message_count, session_config)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        session_type_name=excluded.session_type_name,
                        created_at=excluded.created_at,
                        last_used_at=excluded.last_used_at,
                        message_count=excluded.message_count,
                        session_config=excluded.session_config
                    """,
                    (
                        config.session_id,
                        config.session_type_name or "",
                        float(config.created_at),
                        float(config.last_used_at),
                        int(config.message_count),
                        json.dumps(config.session_config or {}, ensure_ascii=False),
                    ),
                )
                conn.commit()

    def get(self, session_id: str) -> Optional[SessionConfig]:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM session_configs WHERE session_id=?", (session_id,)
                ).fetchone()
                if row is None:
                    return None
                return self._row_to_config(row)

    def list(self, limit: int = 200) -> List[SessionConfig]:
        """列出持久化的会话配置 (按最后使用时间倒序)
        
        参数:
        - limit: 最大返回数量 (默认 200)
        """
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM session_configs
                    ORDER BY last_used_at DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
                return [self._row_to_config(row) for row in rows]

    def delete(self, session_id: str):
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM session_configs WHERE session_id=?", (session_id,))
                conn.commit()

    def update_runtime_fields(self, session_id: str, last_used_at: float, message_count: int):
        """仅更新运行时统计字段, 免覆盖 session_config 本体
        
        参数:
        - session_id: 会话 ID
        - last_used_at: 最后使用时间 (Unix 时间戳)
        - message_count: 消息计数
        """
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE session_configs
                    SET last_used_at=?, message_count=?
                    WHERE session_id=?
                    """,
                    (float(last_used_at), int(message_count), session_id),
                )
                conn.commit()


class SessionPool:
    """活跃会话池: 保持原有 LRU + 闲置清理机制"""

    def __init__(self, max_size: int = 1000, idle_timeout: int = 3600):
        """初始化会话池
        
        参数:
        - max_size: 最大会话数量 (默认 1000)
        - idle_timeout: 闲置超时时间 (秒, 默认 3600)
        """
        self._sessions: Dict[str, SessionEntry] = {}
        self.max_size = max_size
        self.idle_timeout = idle_timeout
        self._lock = threading.RLock()

    def get(self, session_id: str) -> Optional[SessionEntry]:
        """获取会话条目
        
        参数:
        - session_id: 会话 ID
        
        返回:
        - SessionEntry 或 None
        """
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry:
                entry.last_used = time.time()
            return entry

    def put(
        self,
        session_id: str,
        session: Session | AsyncSession,
        session_type: str,
    ) -> Optional[tuple[str, SessionEntry]]:
        """添加会话条目到池
        
        参数:
        - session_id: 会话 ID
        - session: 会话实例
        - session_type: 会话类型
        
        返回:
        - 旧会话条目 (如果存在) 或 None
        """
        with self._lock:
            now = time.time()

            if session_id in self._sessions:
                entry = self._sessions[session_id]
                entry.session = session
                entry.session_type = session_type
                entry.last_used = now
                return None

            evicted: Optional[tuple[str, SessionEntry]] = None
            if len(self._sessions) >= self.max_size:
                evicted = self._evict_one_locked()

            self._sessions[session_id] = SessionEntry(
                session=session,
                session_type=session_type,
                created_at=now,
                last_used=now,
            )
            return evicted

    def remove(self, session_id: str) -> Optional[SessionEntry]:
        """从池中移除会话条目
        
        参数:
        - session_id: 会话 ID
        
        返回:
        - 移除的会话条目 (如果存在) 或 None
        """
        with self._lock:
            return self._sessions.pop(session_id, None)

    def list_entries(self) -> Dict[str, SessionEntry]:
        """列出所有会话条目
        
        返回:
        - 所有会话条目 (会话 ID -> SessionEntry)
        """
        with self._lock:
            return dict(self._sessions)

    def collect_idle(self, max_idle_seconds: Optional[int] = None) -> List[tuple[str, SessionEntry]]:
        """收集闲置会话
        
        参数:
        - max_idle_seconds: 最大闲置时间 (秒, 默认 3600)
        
        返回:
        - 所有移除的会话条目 (会话 ID -> SessionEntry)
        """
        with self._lock:
            now = time.time()
            timeout = self.idle_timeout if max_idle_seconds is None else max_idle_seconds
            idle_ids = [
                sid for sid, entry in self._sessions.items() if (now - entry.last_used) > timeout
            ]

            removed: List[tuple[str, SessionEntry]] = []
            for sid in idle_ids:
                entry = self._sessions.pop(sid, None)
                if entry:
                    removed.append((sid, entry))
            return removed

    def _evict_one_locked(self) -> tuple[str, SessionEntry]:
        """从池中移除最旧会话
        
        返回:
        - 移除的会话条目 (会话 ID -> SessionEntry)
        """
        oldest_id = min(self._sessions.keys(), key=lambda sid: self._sessions[sid].last_used)
        return oldest_id, self._sessions.pop(oldest_id)


class SessionManager:
    """会话管理器

    目标:
    - 注册会话类型时可创建并持久化 `SessionConfig`, 自动分配 session_id
    - 使用 session_id 对话时, 从 SQL 恢复 SessionConfig 再实例化 session
    - 保持原有活跃淘汰机制(SessionPool)不变
    """

    def __init__(
        self,
        default_session_type: str = "default",
        max_size: int = 1000,
        idle_timeout: int = 3600,
        db_path: str | Path | None = None,
    ):
        """初始化会话管理器
        
        参数:
        - default_session_type: 默认会话类型 (默认 "default")
        - max_size: 最大会话池大小 (默认 1000)
        - idle_timeout: 最大闲置时间 (秒, 默认 3600)
        - db_path: 数据库文件路径 (默认当前目录下的 .satrap/session_config.db)
        """
        self.registry = SessionRegistry()
        self.pool = SessionPool(max_size=max_size, idle_timeout=idle_timeout)
        self.store = SessionConfigStore(db_path=db_path)

        self.default_session_type = default_session_type
        self._async_lock = asyncio.Lock()

        try:
            # 保持兼容: 默认类型仍映射到基础 Session 类
            self.registry.register(default_session_type, Session)
        except Exception as e:
            logger.error(f"[SessionManager] 注册默认会话类型失败：{e}")

    # ---------------- 注册与配置 ----------------
    def register_session_type(self, session_type_name: str, session_class: Type[Session] | Type[AsyncSession]):
        """仅注册类型映射
        
        参数:
        - session_type_name: 会话类型名称
        - session_class: 会话类 (必须继承 Session / AsyncSession)
        """
        try:
            self.registry.register(session_type_name, session_class)
        except Exception as e:
            logger.error(
                f"[SessionManager] 注册会话类型失败：session_type_name={session_type_name}, 错误={e}"
            )

    def register_session(
        self,
        session_class: Type[Session] | Type[AsyncSession],
        session_type_name: str | None = None,
        session_config: Optional[Dict[str, Any]] = None,
        session_id: str | None = None,
    ) -> SessionConfig:
        """注册会话并分配 session_id, 同时持久化 SessionConfig

        参数:
        - session_class: 必须继承 Session / AsyncSession
        - session_type_name: 可选, 不传则使用类名
        - session_config: 会话实例初始化配置(会存库)
        - session_id: 可选, 不传则自动生成
        """
        if not inspect.isclass(session_class) or not issubclass(session_class, (Session, AsyncSession)):
            raise TypeError("session_class 必须继承 Session 或 AsyncSession")

        type_name = (session_type_name or session_class.__name__).strip() or session_class.__name__
        self.registry.register(type_name, session_class)

        now = time.time()
        sid = (session_id or uuid.uuid4().hex).strip()
        cfg = SessionConfig(
            session_id=sid,
            session_type_name=type_name,
            created_at=now,
            last_used_at=now,
            message_count=0,
            session_config=dict(session_config or {}),
        )
        self.store.upsert(cfg)
        return cfg

    def get_session_config(self, session_id: str) -> Optional[SessionConfig]:
        """根据 session_id 获取会话配置
        
        参数:
        - session_id: 会话 ID
        
        返回:
        - 会话配置 (SessionConfig) 或 None
        """
        return self.store.get(session_id)

    def list_session_configs(self, limit: int = 200) -> List[SessionConfig]:
        """列出持久化 SessionConfig (来自 SQLite)
        
        参数:
        - limit: 最大返回数量 (默认 200)
        
        返回:
        - 会话配置列表 (SessionConfig)
        """
        return self.store.list(limit=limit)

    def list_registered_session_types(self) -> List[str]:
        """列出当前已注册的 session_type_name
        
        返回:
        - 会话类型名称列表 (str)
        """
        return self.registry.list_types()

    def update_session_config(
        self,
        session_id: str,
        session_config: Optional[Dict[str, Any]] = None,
        session_type_name: Optional[str] = None,
    ) -> Optional[SessionConfig]:
        """更新已存在会话的持久化配置并返回更新后的 SessionConfig

        参数:
        - session_id: 会话 ID
        - session_config: 可选, 不传则不更新 session_config
        - session_type_name: 可选, 不传则不更新 session_type_name
        
        返回:
        - 更新后的会话配置 (SessionConfig) 或 None
        """
        cfg = self.store.get(session_id)
        if cfg is None:
            return None

        if session_config is not None:
            cfg.session_config = dict(session_config)
        if session_type_name is not None:
            cfg.session_type_name = session_type_name
        cfg.last_used_at = time.time()
        self.store.upsert(cfg)
        return cfg

    # ---------------- 调用入口 ----------------
    def handle_call(self, user_call: UserCall) -> str:
        """同步处理用户调用

        实现逻辑:
        1. 根据 user_call.session_id 读取 SessionConfig
        2. 若不存在则按请求类型创建一条默认 SessionConfig
        3. 使用 SessionConfig 实例化会话(若池中无活跃实例)
        4. 保持原有池化/淘汰流程
        """
        try:
            if not isinstance(user_call, UserCall):
                logger.error("[SessionManager] handle_call 失败：user_call 必须是 UserCall 实例")
                return ""

            session_cfg = self._resolve_or_create_session_config(user_call)
            session_id = session_cfg.session_id or ""
            user_call.session_id = session_id

            entry = self.pool.get(session_id)
            if entry is None:
                entry = self._create_entry(session_cfg)
                if entry is None:
                    logger.error(f"[SessionManager] handle_call 失败：会话创建失败，session_id={session_id}")
                    return ""

            if isinstance(entry.session, AsyncSession):
                logger.error(
                    f"[SessionManager] handle_call 失败：session_id={session_id} 对应异步会话类 "
                    f"{type(entry.session).__name__}，请使用 handle_call_async"
                )
                return ""

            response = self._invoke_sync_session(entry.session, user_call)
            self._sync_runtime_to_store(session_id, entry.session)
            self.cleanup_idle_sessions()
            return "" if response is None else str(response)
        except Exception as e:
            logger.error(f"[SessionManager] handle_call 发生异常：{e}")
            return ""

    async def handle_call_async(self, user_call: UserCall) -> str:
        """异步处理用户调用
        
        参数:
        - user_call: 用户调用对象 (包含 session_id, method, params)
        
        返回:
        - 会话响应 (str) 或空字符串
        """
        try:
            if not isinstance(user_call, UserCall):
                logger.error("[SessionManager] handle_call_async 失败：user_call 必须是 UserCall 实例")
                return ""

            session_cfg = self._resolve_or_create_session_config(user_call)
            session_id = session_cfg.session_id or ""
            user_call.session_id = session_id

            async with self._async_lock:
                entry = self.pool.get(session_id)
                if entry is None:
                    entry = self._create_entry(session_cfg)

            if entry is None:
                logger.error(f"[SessionManager] handle_call_async 失败：会话创建失败，session_id={session_id}")
                return ""

            if isinstance(entry.session, AsyncSession):
                response = await self._invoke_async_session(entry.session, user_call)
            else:
                response = self._invoke_sync_session(entry.session, user_call)

            self._sync_runtime_to_store(session_id, entry.session)
            await self.cleanup_idle_sessions_async()
            return "" if response is None else str(response)
        except Exception as e:
            logger.error(f"[SessionManager] handle_call_async 发生异常：{e}")
            return ""

    # ---------------- 查询/清理 ----------------
    def list_sessions(self) -> List[SessionMetadata]:
        """列出当前活跃会话 (内存池快照)
        
        返回:
        - 活跃会话元数据列表 (SessionMetadata)
        """
        try:
            result: List[SessionMetadata] = []
            for session_id, entry in self.pool.list_entries().items():
                result.append(
                    SessionMetadata(
                        session_id=session_id,
                        session_type=entry.session_type,
                        created_at=entry.created_at,
                        last_used_at=entry.last_used,
                        message_count=self._session_message_count(entry.session),
                    )
                )
            result.sort(key=lambda x: x.last_used_at, reverse=True)
            return result
        except Exception as e:
            logger.error(f"[SessionManager] list_sessions 失败：{e}")
            return []

    def cleanup_idle_sessions(self, max_idle_seconds: int = 3600):
        """同步清理闲置会话 (仅影响活跃池, 不删除持久化配置)
        
        参数:
        - max_idle_seconds: 最大闲置时间 (默认 3600 秒)
        """
        try:
            removed = self.pool.collect_idle(max_idle_seconds=max_idle_seconds)
            for session_id, entry in removed:
                self._release_session_memory(entry.session)
                self._sync_runtime_to_store(session_id, entry.session)
                logger.info(f"[SessionManager] 已清理闲置会话：{session_id}")
        except Exception as e:
            logger.error(f"[SessionManager] cleanup_idle_sessions 失败：{e}")

    async def cleanup_idle_sessions_async(self, max_idle_seconds: int = 3600):
        """异步清理闲置会话 (仅影响活跃池, 不删除持久化配置)
        
        参数:
        - max_idle_seconds: 最大闲置时间 (默认 3600 秒)
        """
        try:
            removed = self.pool.collect_idle(max_idle_seconds=max_idle_seconds)
            for session_id, entry in removed:
                await self._release_session_memory_async(entry.session)
                self._sync_runtime_to_store(session_id, entry.session)
                logger.info(f"[SessionManager] 已清理闲置会话：{session_id}")
        except Exception as e:
            logger.error(f"[SessionManager] cleanup_idle_sessions_async 失败：{e}")

    def remove_session(self, session_id: str, remove_config: bool = False):
        """移除活跃会话 (同步)

        参数:
        - session_id: 会话 ID
        - remove_config=False: 仅移除内存活跃实例, 保留 SQL 中的 SessionConfig
        - remove_config=True: 同时删除 SQL 中配置
        """
        try:
            entry = self.pool.remove(session_id)
            if entry:
                self._release_session_memory(entry.session)
            if remove_config:
                self.store.delete(session_id)
        except Exception as e:
            logger.error(f"[SessionManager] remove_session 失败：session_id={session_id}, 错误={e}")

    async def remove_session_async(self, session_id: str, remove_config: bool = False):
        """移除活跃会话 (异步)

        参数:
        - session_id: 会话 ID
        - remove_config=False: 仅移除内存活跃实例, 保留 SQL 中的 SessionConfig
        - remove_config=True: 同时删除 SQL 中配置
        """
        try:
            entry = self.pool.remove(session_id)
            if entry:
                await self._release_session_memory_async(entry.session)
            if remove_config:
                self.store.delete(session_id)
        except Exception as e:
            logger.error(f"[SessionManager] remove_session_async 失败：session_id={session_id}, 错误={e}")

    # ---------------- 内部逻辑 ----------------
    def _resolve_or_create_session_config(self, user_call: UserCall) -> SessionConfig:
        """按 user_call 解析 SessionConfig, 不存在则创建并持久化

        参数:
        - user_call: 用户调用请求 (UserCall)

        返回:
        - 会话配置 (SessionConfig)

        规则:
        - 若传入 session_id 且数据库存在该记录: 直接使用
        - 否则按 user_call.session_type(或 default_session_type) 创建新记录
        """
        if user_call.session_id:
            existed = self.store.get(user_call.session_id)
            if existed is not None:
                return existed

        requested_type = (user_call.session_type or self.default_session_type).strip()
        session_class = self.registry.get_class(requested_type)
        if session_class is None:
            logger.warning(
                f"[SessionManager] 未知会话类型 {requested_type}，回退到默认类型 {self.default_session_type}"
            )
            requested_type = self.default_session_type

        default_cls = self.registry.get_class(requested_type)
        if default_cls is None:
            # 理论上不会发生；兜底保证注册
            self.registry.register(self.default_session_type, Session)

        sid = user_call.session_id or uuid.uuid4().hex
        now = time.time()
        cfg = SessionConfig(
            session_id=sid,
            session_type_name=requested_type,
            created_at=now,
            last_used_at=now,
            message_count=0,
            session_config={},
        )
        self.store.upsert(cfg)
        return cfg

    def _instantiate_session(
        self, session_class: Type[Session] | Type[AsyncSession], session_cfg: SessionConfig
    ) -> Session | AsyncSession:
        """根据 SessionConfig 实例化 session 类

        参数:
        - session_class: 会话类 (Session 或 AsyncSession)
        - session_cfg: 会话配置 (SessionConfig)

        返回:
        - 会话实例 (Session 或 AsyncSession)

        兼容三类构造习惯:
        1. `__init__(self, session_config: SessionConfig, ...)`
        2. `__init__(self, session_id: str, ..., **session_config)`
        3. `__init__(self, session_id: str, ...)` (忽略 session_config)
        """
        sig = inspect.signature(session_class)
        params = list(sig.parameters.values())
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)

        # case 1: 优先支持显式 session_config 入参
        if any(p.name == "session_config" for p in params):
            payload = dict(session_cfg.session_config or {})
            payload.pop("session_id", None)

            kwargs = {"session_config": session_cfg}
            # 若构造器同时也要求 session_id, 这里补齐
            if any(p.name == "session_id" for p in params):
                kwargs["session_id"] = session_cfg.session_id
            if has_var_kw:
                kwargs.update(payload)
            else:
                accepted = {p.name for p in params}
                for key, value in payload.items():
                    if key in accepted and key not in kwargs:
                        kwargs[key] = value
            return session_class(**kwargs)   # type: ignore[misc]

        # case 2/3: 走 session_id + 配置透传
        payload = dict(session_cfg.session_config or {})
        payload.pop("session_id", None)

        accepted = {p.name for p in params}
        kwargs: Dict[str, Any] = {}

        if "session_id" in accepted:
            kwargs["session_id"] = session_cfg.session_id

        if has_var_kw:
            kwargs.update(payload)
        else:
            for key, value in payload.items():
                if key in accepted:
                    kwargs[key] = value

        # 最后兜底: 仍无法提供 session_id 关键字时, 尝试位置参数
        if not kwargs and session_cfg.session_id:
            try:
                return session_class(session_cfg.session_id)   # type: ignore[misc]
            except Exception:
                pass

        return session_class(**kwargs)   # type: ignore[misc]

    def _create_entry(self, session_cfg: SessionConfig) -> Optional[SessionEntry]:
        """根据 SessionConfig 创建活跃会话并放入会话池

        参数:
        - session_cfg: 会话配置 (SessionConfig)

        返回:
        - 会话条目 (SessionEntry)
        """
        session_id = session_cfg.session_id or ""
        session_type = session_cfg.session_type_name or self.default_session_type

        session_class = self.registry.get_class(session_type)
        if session_class is None:
            logger.error(f"[SessionManager] 创建会话失败：未知 session_type_name={session_type}")
            return None

        try:
            session = self._instantiate_session(session_class, session_cfg)
        except Exception as e:
            logger.error(
                f"[SessionManager] 创建会话实例失败：session_type_name={session_type}, "
                f"session_id={session_id}, 错误={e}"
            )
            return None

        try:
            evicted = self.pool.put(session_id, session, session_type)
        except Exception as e:
            logger.error(f"[SessionManager] 放入会话池失败：session_id={session_id}, 错误={e}")
            return None

        if evicted:
            evicted_id, evicted_entry = evicted
            self._release_session_memory(evicted_entry.session)
            self._sync_runtime_to_store(evicted_id, evicted_entry.session)
            logger.info(f"[SessionManager] 已按 LRU 淘汰会话：{evicted_id}")

        entry = self.pool.get(session_id)
        if entry is None:
            logger.error(f"[SessionManager] 创建会话条目失败：session_id={session_id}")
            return None
        return entry

    def _sync_runtime_to_store(self, session_id: str, session: Session | AsyncSession):
        """将内存中的 last_used/message_count 回写到 SQLite 数据库

        参数:
        - session_id: 会话 ID
        - session: 会话实例 (Session 或 AsyncSession)
        """
        try:
            message_count = self._session_message_count(session)
            self.store.update_runtime_fields(
                session_id=session_id,
                last_used_at=time.time(),
                message_count=message_count,
            )
        except Exception as e:
            logger.warning(f"[SessionManager] 同步会话运行状态失败：session_id={session_id}, 错误={e}")

    @staticmethod
    def _invoke_sync_session(session: Session, user_call: UserCall) -> Any:
        """同步执行会话

        参数:
        - session: 同步会话实例 (Session)
        - user_call: 用户调用对象 (UserCall)

        返回:
        - 会话执行结果
        """
        try:
            run_method = session.run
            args = SessionManager._build_run_args(run_method, user_call)
            return run_method(*args)
        except Exception as e:
            logger.error(f"[SessionManager] 同步会话执行失败：{e}")
            return ""

    @staticmethod
    async def _invoke_async_session(session: AsyncSession, user_call: UserCall) -> Any:
        """异步执行会话

        参数:
        - session: 异步会话实例 (AsyncSession)
        - user_call: 用户调用对象 (UserCall)

        返回:
        - 会话执行结果
        """
        try:
            run_method = session.run
            args = SessionManager._build_run_args(run_method, user_call)
            return await run_method(*args)
        except Exception as e:
            logger.error(f"[SessionManager] 异步会话执行失败：{e}")
            return ""

    @staticmethod
    def _build_run_args(run_method: Any, user_call: UserCall) -> tuple[Any, ...]:
        """保持原有参数适配策略

        参数:
        - run_method: 会话实例的 run 方法
        - user_call: 用户调用对象 (UserCall)

        返回:
        - 适配后的参数元组
        """
        try:
            params = list(inspect.signature(run_method).parameters.values())
        except Exception as e:
            logger.error(f"[SessionManager] 解析 run 方法签名失败，回退为 message 单参数：{e}")
            return (user_call.message or "",)

        if any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in params):
            return (user_call.message or "",)

        param_size = len(params)
        if param_size == 0:
            return ()

        if param_size == 1:
            name = params[0].name.lower()
            if name in {"user_call", "call", "request"}:
                return (user_call,)
            return (user_call.message or "",)

        return (user_call.message or "", user_call.img_urls)

    @staticmethod
    def _session_message_count(session: Session | AsyncSession) -> int:
        """获取会话已处理消息数量

        参数:
        - session: 会话实例 (Session 或 AsyncSession)

        返回:
        - 会话已处理消息数量
        """
        ctx: ContextManager | AsyncContextManager | None = getattr(session, "session_ctx", None)
        if ctx is None:
            return 0
        try:
            return int(ctx.static_message())
        except Exception as e:
            logger.error(f"[SessionManager] 读取会话消息数失败：{e}")
            return 0

    @staticmethod
    def _release_session_memory(session: Session | AsyncSession):
        """释放会话内存

        参数:
        - session: 会话实例 (Session 或 AsyncSession)
        """
        clear_method = getattr(session, "clear_memory", None)
        if not callable(clear_method):
            return

        try:
            result = clear_method()
            if inspect.isawaitable(result):
                logger.warning("[SessionManager] 同步路径调用到异步 clear_memory，已跳过 await")
        except Exception as e:
            logger.warning(f"[SessionManager] clear_memory 执行失败：{e}")

    @staticmethod
    async def _release_session_memory_async(session: Session | AsyncSession):
        """释放会话内存 (异步)

        参数:
        - session: 会话实例 (Session 或 AsyncSession)
        """
        clear_method = getattr(session, "clear_memory", None)
        if not callable(clear_method):
            return

        try:
            result = clear_method()
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.warning(f"[SessionManager] clear_memory 执行失败：{e}")
