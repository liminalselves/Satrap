"""
会话实例生命周期管理器

按会话类型创建并缓存同步或异步会话, 管理容量与空闲回收,
同时负责会话元数据持久化, 恢复和默认模型配置注入
"""
from __future__ import annotations
from satrap.core.plugin_interop.environment import EnvironmentSnapshot, bind_environment

import dataclasses
from dataclasses import dataclass, field
from contextlib import contextmanager
from functools import partial
import threading
import asyncio
import inspect
from pathlib import Path
import secrets
import sqlite3
import string
from typing import Any, Awaitable, Callable, Dict, List, Optional, Type, cast, TYPE_CHECKING
import json
import time

import traceback

from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.framework.providers import (
    SESSION_CLASS_PROVIDER,
    SessionClassProvider,
    SessionProvider,
    SessionProviderRegistry,
)
from satrap.core.framework.providers.base import SessionProviderDefinition
from satrap.core.config.model_snapshot import llm_config_fingerprint
from satrap.core.utils.async_worker import SESSION_WORKERS, WorkerBusyError
from satrap.core.utils.performance import PhaseTimings
from satrap.core.utils.application_progress import progress_instance, progress_locks, progress_phase, progress_result, progress_source_check
from satrap.core.APICall.LLMCall import AsyncLLM, LLM, build_llm_from_config
from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap.core.utils.paths import get_db_path
from satrap.edictum.registry import EDICTUM_PROVIDER
from satrap.edictum.plugin_apply import EdictumPluginApplySnapshot
from satrap.core.storage import (
    LOCAL_PLATFORM_ID,
    StorageLayout,
    StorageMaintenanceService,
    StorageScope,
    default_storage_layout,
)
from satrap.core.type import SessionConfig, UserCall, LLMConfig, CommandAction, safe_getattr, safe_getattr_callable
from satrap.core.call_context import bind_call_origin
from satrap.core.conversation import bind_conversation_route
from satrap.core.group_chat.reply import buffer_session_reply, abort_reply_turn

from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.core.framework.UserManager import UserManager

_UID_ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase

from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.core.config.group_session import resolve_group_session


def _short_uid(n: int = 6) -> str:
    """
    生成 n 字符 base62 随机 ID, 默认 6 字符 ~= 568 亿组合

    参数:
    - n: 输入值

    返回:
    - str: 生成 n 字符 base62 随机 ID, 默认 6 字符 ~= 568 亿组合
    """
    return ''.join(secrets.choice(_UID_ALPHABET) for _ in range(n))


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
    instance_generation: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    """会话对象的生命周期标识"""
    sync_operation_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    """串行化同步会话运行和运行时配置更新"""
    async_operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    """串行化异步会话运行和运行时配置更新"""
    active_calls: int = 0
    """正在使用此条目的调用数量"""
    retiring: bool = False
    """条目是否已进入淘汰流程"""


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


class SessionPoolCapacityError(RuntimeError):
    """会话池达到硬容量上限且没有可淘汰条目"""


class SessionRegistry:
    """会话类型注册表: 维护 `session_type_name -> 会话类` 的映射"""

    def __init__(self):
        """初始化 SessionRegistry"""
        self._mapping: Dict[str, Type[Session] | Type[AsyncSession]] = {}
        self._lock = threading.RLock()

    def register(self, session_type_name: str, session_class: Type[Session] | Type[AsyncSession]):
        """
        注册会话类型

        这里仅维护映射, 不做实例化; 实例化由 SessionManager 在真正需要时完成

        参数:
        - session_type_name: 会话类型名称
        - session_class: 会话类对象 (Session 或 AsyncSession)
        """
        if not session_type_name:
            logger.error("[SessionRegistry] 注册失败：session_type_name 不能为空")
            return

        with self._lock:
            self._mapping[session_type_name] = session_class

    def get_class(self, session_type_name: str) -> Optional[Type[Session] | Type[AsyncSession]]:
        """
        根据会话类型名称获取会话类

        参数:
        - session_type_name: 会话类型名称

        返回:
        - 会话类对象 (Session 或 AsyncSession)
        """
        with self._lock:
            return self._mapping.get(session_type_name)

    def list_types(self) -> List[str]:
        """
        列出所有已注册的会话类型名称

        返回:
        - 会话类型名称列表
        """
        with self._lock:
            return list(self._mapping.keys())


class SessionConfigStore:
    """
    SessionConfig 的 SQLite 持久化存储

    表设计非常轻量, 仅保存会话配置与基础统计字段, 便于后续按 session_id 恢复实例
    """

    def __init__(self, db_path: str | Path | None = None):
        """
        初始化会话配置存储

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
        - 数据库连接对象 (sqlite3.Connection)
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        """创建事务连接并确保离开作用域时关闭"""
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_table(self):
        """
        初始化会话配置表

        确保数据库表存在, 并创建必要的索引
        """
        with self._lock:
            with self._connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_configs (
                        session_id TEXT PRIMARY KEY,
                        session_type_name TEXT NOT NULL,
                        provider_name TEXT NOT NULL DEFAULT 'session_class',
                        created_at REAL NOT NULL,
                        last_used_at REAL NOT NULL,
                        message_count INTEGER NOT NULL,
                        session_config TEXT NOT NULL
                    )
                    """
                )
                columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(session_configs)").fetchall()
                }
                if "provider_name" not in columns:
                    conn.execute(
                        "ALTER TABLE session_configs ADD COLUMN provider_name TEXT NOT NULL DEFAULT 'session_class'"
                    )
                conn.commit()

    @staticmethod
    def _row_to_config(row: sqlite3.Row) -> SessionConfig:
        """
        将 SQLite 行转换为 SessionConfig 实例

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
                    payload = cast(Dict[str, Any], parsed)
            except Exception:
                payload = {}

        return SessionConfig(
            session_id=row["session_id"],
            session_type_name=row["session_type_name"],
            provider_name=str(row["provider_name"] or SESSION_CLASS_PROVIDER),
            created_at=float(row["created_at"]),
            last_used_at=float(row["last_used_at"]),
            message_count=int(row["message_count"]),
            session_config=payload,
        )

    def upsert(self, config: SessionConfig):
        """
        插入或更新一条 SessionConfig 记录

        参数:
        - config: 会话配置 (SessionConfig)
        """
        if not config.session_id:
            raise ValueError("session_id 不能为空")

        with self._lock:
            with self._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO session_configs
                    (session_id, session_type_name, provider_name, created_at, last_used_at, message_count, session_config)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        session_type_name=excluded.session_type_name,
                        provider_name=excluded.provider_name,
                        created_at=excluded.created_at,
                        last_used_at=excluded.last_used_at,
                        message_count=excluded.message_count,
                        session_config=excluded.session_config
                    """,
                    (
                        config.session_id,
                        config.session_type_name or "",
                        config.provider_name or SESSION_CLASS_PROVIDER,
                        float(config.created_at),
                        float(config.last_used_at),
                        int(config.message_count),
                        json.dumps(config.session_config or {}, ensure_ascii=False),
                    ),
                )
                conn.commit()

    def get(self, session_id: str) -> Optional[SessionConfig]:
        """
        获取

        参数:
        - session_id: 会话 ID

        返回:
        - Optional[SessionConfig]: 获取
        """
        with self._lock:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT * FROM session_configs WHERE session_id=?", (session_id,)
                ).fetchone()
                if row is None:
                    return None
                return self._row_to_config(row)

    def list(self, limit: int = 200) -> List[SessionConfig]:
        """
        列出持久化的会话配置 (按最后使用时间倒序)

        参数:
        - limit: 最大返回数量 (默认 200)

        返回:
        - List[SessionConfig]: 列出持久化的会话配置 (按最后使用时间倒序)
        """
        with self._lock:
            with self._connection() as conn:
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
        """
        删除

        参数:
        - session_id: 会话 ID
        """
        with self._lock:
            with self._connection() as conn:
                conn.execute("DELETE FROM session_configs WHERE session_id=?", (session_id,))
                conn.commit()

    def list_definition_references(
        self,
        provider_name: str,
        definition_name: str,
    ) -> List[str]:
        """
        列出引用指定 Provider 命名定义的会话 ID

        参数:
        - provider_name: Provider 名称
        - definition_name: 命名定义名称

        返回:
        - List[str]: 引用该定义的会话 ID
        """
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT session_id FROM session_configs
                    WHERE provider_name=? AND session_type_name=?
                    ORDER BY session_id
                    """,
                    (provider_name, definition_name),
                ).fetchall()
                return [str(row["session_id"]) for row in rows]

    def rename_definition_references(
        self,
        provider_name: str,
        old_name: str,
        new_name: str,
    ) -> List[str]:
        """
        迁移指定 Provider 命名定义的全部会话引用

        参数:
        - provider_name: Provider 名称
        - old_name: 原定义名称
        - new_name: 新定义名称

        返回:
        - List[str]: 已迁移的会话 ID
        """
        if not new_name.strip():
            raise ValueError("新定义名称不能为空")
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT session_id FROM session_configs
                    WHERE provider_name=? AND session_type_name=?
                    ORDER BY session_id
                    """,
                    (provider_name, old_name),
                ).fetchall()
                session_ids = [str(row["session_id"]) for row in rows]
                conn.execute(
                    """
                    UPDATE session_configs SET session_type_name=?
                    WHERE provider_name=? AND session_type_name=?
                    """,
                    (new_name, provider_name, old_name),
                )
                conn.commit()
                return session_ids

    def list_ids_by_message_count(self, message_count: int) -> List[str]:
        """
        按消息数列出全部会话 ID

        参数:
        - message_count: 精确匹配的消息数

        返回:
        - List[str]: 符合条件的全部会话 ID
        """
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(
                    "SELECT session_id FROM session_configs WHERE message_count=?",
                    (int(message_count),),
                ).fetchall()
                return [str(row["session_id"]) for row in rows]

    def update_runtime_fields(self, session_id: str, last_used_at: float, message_count: int):
        """
        仅更新运行时统计字段, 免覆盖 session_config 本体

        参数:
        - session_id: 会话 ID
        - last_used_at: 最后使用时间 (Unix 时间戳)
        - message_count: 消息计数
        """
        with self._lock:
            with self._connection() as conn:
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
        """
        初始化会话池

        参数:
        - max_size: 最大会话数量 (默认 1000)
        - idle_timeout: 闲置超时时间 (秒, 默认 3600)
        """
        self._sessions: Dict[str, SessionEntry] = {}
        self.max_size = max_size
        self.idle_timeout = idle_timeout
        self._lock = threading.RLock()

    def get(self, session_id: str) -> Optional[SessionEntry]:
        """
        获取会话条目

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

    def acquire(
        self,
        session_id: str,
        expected: SessionEntry | None = None,
    ) -> SessionEntry | None:
        """
        获取会话使用租约

        参数:
        - session_id: 会话 ID
        - expected: 可选的预期条目, 用于阻止替换后的旧条目被误用

        返回:
        - 可用会话条目; 条目不存在, 已替换或正在淘汰时返回 None
        """
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None or entry.retiring:
                return None
            if expected is not None and entry is not expected:
                return None
            entry.active_calls += 1
            entry.last_used = time.time()
            return entry

    def release(self, entry: SessionEntry) -> None:
        """
        释放会话使用租约

        参数:
        - entry: 已获取租约的会话条目
        """
        with self._lock:
            if entry.active_calls <= 0:
                raise RuntimeError("会话使用租约计数失衡")
            entry.active_calls -= 1
            entry.last_used = time.time()

    def put(
        self,
        session_id: str,
        session: Session | AsyncSession,
        session_type: str,
    ) -> Optional[tuple[str, SessionEntry]]:
        """
        添加会话条目到池

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
                if entry.session is not session:
                    entry.instance_generation = secrets.token_urlsafe(16)
                entry.session = session
                entry.session_type = session_type
                entry.last_used = now
                return None

            evicted: Optional[tuple[str, SessionEntry]] = None
            if len(self._sessions) >= self.max_size:
                evicted = self._evict_one_locked()
                if evicted is None:
                    raise SessionPoolCapacityError(
                        f"会话池已满且所有条目均在使用: max_size={self.max_size}"
                    )

            self._sessions[session_id] = SessionEntry(
                session=session,
                session_type=session_type,
                created_at=now,
                last_used=now,
            )
            return evicted

    def replace(
        self,
        session_id: str,
        expected: SessionEntry,
        replacement: SessionEntry,
        *, expected_generation: str | None = None,
    ) -> bool:
        """
        仅在当前条目仍匹配时原子替换会话

        参数:
        - session_id: 会话 ID
        - expected: 预期被替换的旧条目
        - replacement: 新会话条目
        - expected_generation: 可选旧运行代次, 防止同一条目对象被再次替换后误提交

        返回:
        - 成功替换返回 True; 当前条目已变化时返回 False
        """
        with self._lock:
            current = self._sessions.get(session_id)
            if current is None or current is not expected:
                return False
            if expected_generation is not None and current.instance_generation != expected_generation:
                return False
            if current.active_calls > 0 or current.retiring:
                return False
            current.session = replacement.session
            current.instance_generation = replacement.instance_generation
            current.session_type = replacement.session_type
            current.created_at = replacement.created_at
            current.last_used = replacement.last_used
            return True

    def remove(
        self,
        session_id: str,
        expected: SessionEntry | None = None,
        *, expected_generation: str | None = None,
    ) -> Optional[SessionEntry]:
        """
        从池中移除会话条目

        参数:
        - session_id: 会话 ID
        - expected: 可选的预期条目, 用于阻止并发替换后的新条目被误删
        - expected_generation: 可选运行代次, 阻止同一条目对象的新实例被误删

        返回:
        - 匹配并移除的会话条目; 条目不存在或与 expected 不匹配时返回 None
        """
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None or (expected is not None and entry is not expected):
                return None
            if expected_generation is not None and entry.instance_generation != expected_generation:
                return None
            removed = self._sessions.pop(session_id)
            removed.retiring = True
            return removed

    def remove_if_idle(
        self,
        session_id: str,
        expected: SessionEntry | None = None,
    ) -> Optional[SessionEntry]:
        """
        仅在没有活动租约时移除会话条目

        参数:
        - session_id: 会话 ID
        - expected: 可选的预期条目, 用于阻止并发替换后的新条目被误删

        返回:
        - 空闲且匹配时返回被移除条目; 条目忙碌、不存在或已替换时返回 None
        """
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None or (expected is not None and entry is not expected):
                return None
            if entry.active_calls > 0 or entry.retiring:
                return None
            removed = self._sessions.pop(session_id)
            removed.retiring = True
            return removed

    def list_entries(self) -> Dict[str, SessionEntry]:
        """
        列出所有会话条目

        返回:
        - 所有会话条目 (会话 ID -> SessionEntry)
        """
        with self._lock:
            return dict(self._sessions)

    def collect_idle(self, max_idle_seconds: Optional[int] = None) -> List[tuple[str, SessionEntry]]:
        """
        收集闲置会话

        参数:
        - max_idle_seconds: 最大闲置时间 (秒, 默认 3600)

        返回:
        - 所有移除的会话条目 (会话 ID -> SessionEntry)
        """
        with self._lock:
            now = time.time()
            timeout = self.idle_timeout if max_idle_seconds is None else max_idle_seconds
            idle_ids = [
                sid
                for sid, entry in self._sessions.items()
                if entry.active_calls == 0
                and not entry.retiring
                and (now - entry.last_used) > timeout
            ]

            removed: List[tuple[str, SessionEntry]] = []
            for sid in idle_ids:
                entry = self._sessions.pop(sid)
                entry.retiring = True
                removed.append((sid, entry))
            return removed

    def _evict_one_locked(self) -> tuple[str, SessionEntry] | None:
        """
        从池中移除最旧会话

        返回:
        - 移除的会话条目; 所有条目均在使用时返回 None
        """
        candidates = {
            session_id: entry
            for session_id, entry in self._sessions.items()
            if entry.active_calls == 0 and not entry.retiring
        }
        if not candidates:
            return None
        oldest_id = min(candidates, key=lambda sid: candidates[sid].last_used)
        entry = self._sessions.pop(oldest_id)
        entry.retiring = True
        return oldest_id, entry


class SessionManager:
    """
    会话管理器

    目标:
    - 注册会话类型时可创建并持久化 `SessionConfig`, 自动分配 session_id
    - 使用 session_id 对话时, 从 SQL 恢复 SessionConfig 再实例化 session
    - 保持原有活跃淘汰机制(SessionPool)不变
    """

    def __init__(
        self,
        default_session_type: str = "default",
        default_session_class: Type[Session] | Type[AsyncSession] = Session,
        max_size: int = 1000,
        idle_timeout: int = 3600,
        db_path: str | Path | None = None,
        default_checkpoint: bool = False,
        default_checkpoint_db: str | None = None,
        platform_id: str = LOCAL_PLATFORM_ID,
        storage_layout: StorageLayout | None = None,
    ):
        """
        初始化会话管理器

        参数:
        - default_session_type: 默认会话类型 (默认 "default")
        - default_session_class: 默认会话类
        - max_size: 最大会话池大小 (默认 1000)
        - idle_timeout: 最大闲置时间 (秒, 默认 3600)
        - db_path: 数据库文件路径, 默认 local 平台的 platform.db
        - default_checkpoint: 实例化会话时若未显式配置 enable_checkpoint, 是否默认启用状态检查点 (默认 False)
        - default_checkpoint_db: 实例化会话时若未显式配置 db_path, 注入的上下文库路径 (默认 None, 使用会话自身默认库)
        - platform_id: 此管理器所属的平台实例 ID
        - storage_layout: 可选 v2 数据布局, 提供后启用会话目录隔离与回收
        """
        self.registry = SessionRegistry()
        self.pool = SessionPool(max_size=max_size, idle_timeout=idle_timeout)
        self.store = SessionConfigStore(db_path=db_path)
    
        self.default_session_type = default_session_type
        self._default_checkpoint = default_checkpoint
        self._default_checkpoint_db = default_checkpoint_db

        self.plugin_environment = PluginEnvironment()
        self.platform_id = platform_id.strip() or LOCAL_PLATFORM_ID
        self.storage_layout = (
            storage_layout
            if storage_layout is not None
            else StorageLayout(Path(db_path).parent / "data")
            if db_path is not None
            else default_storage_layout
        )
        self._async_lock = asyncio.Lock()
        self._entry_creation_lock = threading.RLock()
        self._class_cfg_mgr: SessionClassConfigManager | None = None
        self._user_mgr: UserManager | None = None
        self._model_cfg_mgr: ModelConfigManager | None = None
        self.group_apply_reporter: Callable[[UserCall, str, str, str | None], None] | None = None
        self.group_apply_observer: Callable[[UserCall, str, str], None] | None = None
        self.provider_registry = SessionProviderRegistry()
        self.session_class_provider = SessionClassProvider(
            self.registry,
            default_checkpoint=default_checkpoint,
            default_checkpoint_db=default_checkpoint_db,
        )
        self.provider_registry.register(self.session_class_provider)

        try:
            self.registry.register(default_session_type, default_session_class)
            # 保持兼容: 默认类型仍映射到基础 Session 类
        except Exception as e:
            logger.error(f"[SessionManager] 注册默认会话类型失败：{e}")

    # ---------- 注册与配置 ----------
    def register_session_type(self, session_type_name: str, session_class: Type[Session] | Type[AsyncSession]):
        """
        仅注册类型映射

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

    def register_provider(self, provider: SessionProvider, *, replace: bool = False) -> None:
        """
        注册运行时会话 Provider

        参数:
        - provider: Provider 实例
        - replace: 是否允许覆盖同名 Provider
        """
        self.provider_registry.register(provider, replace=replace)

    def register_session(
        self,
        session_class: Type[Session] | Type[AsyncSession],
        session_type_name: str | None = None,
        session_config: Optional[Dict[str, Any]] = None,
        session_id: str | None = None,
        provider_name: str = SESSION_CLASS_PROVIDER,
    ) -> SessionConfig:
        """
        注册会话并分配 session_id, 同时持久化 SessionConfig

        参数:
        - session_class: 必须继承 Session / AsyncSession
        - session_type_name: 可选, 不传则使用类名
        - session_config: 会话实例初始化配置(会存库)
        - session_id: 可选, 不传则自动生成
        - provider_name: 创建会话实例的 Provider 名称

        返回:
        - SessionConfig: 注册会话并分配 session_id, 同时持久化 SessionConfig
        """
        type_name = (session_type_name or session_class.__name__).strip() or session_class.__name__
        self.registry.register(type_name, session_class)

        now = time.time()
        sid = (session_id or _short_uid()).strip()
        cfg = SessionConfig(
            session_id=sid,
            session_type_name=type_name,
            provider_name=provider_name,
            created_at=now,
            last_used_at=now,
            message_count=0,
            session_config=dict(session_config or {}),
        )
        self._persist_config(cfg)
        return cfg

    def register_session_from_class_config(
        self,
        class_config_name: str,
        class_cfg_mgr: SessionClassConfigManager,
        session_id: str | None = None,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> SessionConfig:
        """
        从 SessionClassConfigManager 读取类级配置, 创建实例级 SessionConfig

        参数:
        - class_config_name: SessionClassConfigManager 中的注册名称
        - class_cfg_mgr: SessionClassConfigManager 实例
        - session_id: 可选, 不传则自动生成
        - extra_params: 可选, 补充/覆盖 params(如当前用户特有的覆盖项)

        流程:
        1. 从 class_cfg_mgr 获取 class 对象 + params
        2. 将 class 注册到 self.registry
        3. 合并 params + extra_params 作为 SessionConfig.session_config
        4. 生成 session_id 和 timestamps, 持久化到 SessionConfigStore
        5. 返回 SessionConfig

        返回:
        - SessionConfig: 从 SessionClassConfigManager 读取类级配置, 创建实例级 SessionConfig
        """
        session_class = class_cfg_mgr.get_class(class_config_name)
        params = dict(class_cfg_mgr.get_params(class_config_name))
        if extra_params:
            params.update(extra_params)
        return self.register_session(
            session_class=session_class,
            session_type_name=class_config_name,
            session_config=params,
            session_id=session_id,
        )

    def register_session_from_provider_config(
        self,
        provider_name: str,
        definition_name: str,
        session_id: str | None = None,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> SessionConfig:
        """
        从指定 Provider 的命名定义创建实例级配置

        参数:
        - provider_name: Provider 名称
        - definition_name: Provider 内的命名定义
        - session_id: 可选会话 ID, 不传则自动生成
        - extra_params: 实例级补充或覆盖参数

        返回:
        - SessionConfig: 已持久化的会话实例配置
        """
        resolved = self.provider_registry.resolve_definition(definition_name, provider_name)
        if resolved is None:
            raise ValueError(f"未知会话定义: provider={provider_name}, name={definition_name}")
        provider, definition = resolved
        if not definition.enabled:
            raise ValueError(f"会话定义已禁用: provider={provider_name}, name={definition_name}")
        params = (
            dict(extra_params or {})
            if provider.provider_name == EDICTUM_PROVIDER
            else dict(definition.params)
        )
        if extra_params and provider.provider_name != EDICTUM_PROVIDER:
            params.update(extra_params)
        # Edictum 实例只持久化实例覆盖值, 运行时始终合并最新命名配置
        now = time.time()
        config = SessionConfig(
            session_id=(session_id or _short_uid()).strip(),
            session_type_name=definition.name,
            provider_name=provider.provider_name,
            created_at=now,
            last_used_at=now,
            message_count=0,
            session_config=params,
        )
        self._persist_config(config)
        return config

    def register_session_from_provider_context(
        self,
        provider_name: str,
        definition_name: str,
        context_value: str,
        platform: str = "",
        extra_params: Optional[Dict[str, Any]] = None,
        session_identity: str | None = None,
    ) -> SessionConfig:
        """
        从指定 Provider 定义创建平台上下文会话

        参数:
        - provider_name: Provider 名称
        - definition_name: Provider 内的命名定义
        - context_value: 用户或频道等上下文区分值
        - platform: 平台实例标识
        - extra_params: 实例级补充或覆盖参数
        - session_identity: 可选会话标识, 与注入 Provider 的上下文值分离

        返回:
        - SessionConfig: 已持久化的上下文会话配置
        """
        resolved = self.provider_registry.resolve_definition(definition_name, provider_name)
        if resolved is None:
            raise ValueError(f"未知会话定义: provider={provider_name}, name={definition_name}")
        _, definition = resolved
        context_key = str(definition.metadata.get("context_key", "")).strip()
        params = dict(extra_params or {})
        if context_key:
            params[context_key] = context_value
        identity = context_value if session_identity is None else session_identity
        random_id = _short_uid()
        if platform and platform != definition_name:
            session_id = f"{definition_name}:{platform}:{identity}:{random_id}"
        else:
            session_id = f"{definition_name}:{identity}:{random_id}"
        return self.register_session_from_provider_config(
            provider_name,
            definition_name,
            session_id=session_id,
            extra_params=params,
        )

    def register_session_from_context(
        self,
        class_config_name: str,
        class_cfg_mgr: SessionClassConfigManager,
        context_value: str,
        platform: str = "",
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> SessionConfig:
        """
        用 context_value 作为 session_id 创建实例级 SessionConfig

        参数:
        - class_config_name: SessionClassConfigManager 中的注册名称
        - class_cfg_mgr: SessionClassConfigManager 实例
        - context_value: 上下文区分值 (如用户 ID, 频道 ID)
        - platform: 平台标识, 用于构建 session_id
        - extra_params: 补充/覆盖 params

        session_id 格式: "{class_config_name}:{platform}:{context_value}:{short_uid}"
        同类型+同平台+同 context_value = 同一个上下文

        返回:
        - SessionConfig: 用 context_value 作为 session_id 创建实例级 SessionConfig
        """
        entry = class_cfg_mgr.get_config(class_config_name)
        context_key = entry.get("context_key", "") if entry else ""

        rand = _short_uid()
        if platform and platform != class_config_name:
            session_id = f"{class_config_name}:{platform}:{context_value}:{rand}"
        else:
            session_id = f"{class_config_name}:{context_value}:{rand}"

        params = dict(class_cfg_mgr.get_params(class_config_name))
        if context_key:
            params[context_key] = context_value
        if extra_params:
            params.update(extra_params)

        return self.register_session(
            session_class=class_cfg_mgr.get_class(class_config_name),
            session_type_name=class_config_name,
            session_config=params,
            session_id=session_id,
        )

    def get_session_config(self, session_id: str) -> Optional[SessionConfig]:
        """
        根据 session_id 获取会话配置

        参数:
        - session_id: 会话 ID

        返回:
        - 会话配置 (SessionConfig) 或 None
        """
        return self.store.get(session_id)

    def activate_session(self, session_id: str) -> bool:
        """
        将已持久化会话实例加载到活跃会话池

        参数:
        - session_id: 会话 ID

        返回:
        - bool: 会话是否已成功处于活跃状态
        """
        config = self.store.get(session_id)
        return bool(config and self._get_or_create_entry(config) is not None)

    async def activate_session_async(self, session_id: str) -> bool:
        """
        将持久化会话加载到活跃池并完成异步 Provider 准备

        参数:
        - session_id: 会话 ID

        返回:
        - bool: 会话及其 Provider 生命周期是否准备完成
        """
        config = self.store.get(session_id)
        if config is None:
            return False
        entry = await self._get_or_create_entry_async(config)
        if entry is None:
            return False
        try:
            async with entry.async_operation_lock:
                await self._prepare_session_async(entry.session)
            return True
        except Exception as error:
            removed = self.pool.remove(session_id, expected=entry)
            if removed is not None:
                await self._release_session_memory_async(removed.session)
            logger.error(f"[SessionManager] 激活会话失败: {session_id}, {error}")
            return False

    async def restart_session_async(self, session_id: str) -> dict[str, Any]:
        """
        在保留旧实例兜底的前提下热重启活跃会话

        参数:
        - session_id: 会话 ID

        返回:
        - dict[str, Any]: 热重启结果和最新运行时元数据
        """
        entry = self.pool.get(session_id)
        if entry is None:
            activated = await self.activate_session_async(session_id)
            return {
                "ok": activated,
                "action": "activate",
                "session_id": session_id,
                "platform_id": self.platform_id,
                "runtime": self.get_session_runtime_metadata(session_id),
                **({} if activated else {"error": "会话激活失败"}),
            }

        async with entry.async_operation_lock:
            if isinstance(entry.session, AsyncSession):
                return await self._restart_entry_locked(session_id, entry)
            with entry.sync_operation_lock:
                return await self._restart_entry_locked(session_id, entry)

    async def _restart_entry_locked(self, session_id: str, entry: SessionEntry, *,
                                    apply_snapshot: EdictumPluginApplySnapshot | None = None,
                                    runtime_is_current: Callable[[], bool] | None = None) -> dict[str, Any]:
        """
        在已取得会话操作锁后构建并切换候选实例, 保留失败兜底

        参数:
        - session_id: 活跃会话 ID
        - entry: 调用者已锁定的当前条目
        - apply_snapshot: 可选本次固定来源, 默认保持手动重启行为
        - runtime_is_current: 可选平台管理器复核, 候选准备后提交前再次检查

        返回:
        - 热重启结果, 来源变化或候选失败时保留旧实例
        """
        if self.pool.list_entries().get(session_id) is not entry:
            return {"ok": False, "action": "restart", "session_id": session_id, "platform_id": self.platform_id,
                    "error": "会话运行时已变化", "old_runtime_preserved": True}
        config = self.store.get(session_id)
        if config is None:
            return {
                "ok": False,
                "action": "restart",
                "session_id": session_id,
                "platform_id": self.platform_id,
                "error": "会话冷配置不存在",
            }
        old_session = entry.session
        generation = entry.instance_generation

        def mark_failed(error: str) -> None:
            """把热重启失败写入旧 Provider 运行时状态"""
            provider_name = str(safe_getattr(old_session, "_satrap_provider_name") or "")
            provider = self.provider_registry.get(provider_name) if provider_name else None
            marker = safe_getattr_callable(provider, "mark_runtime_restart_failed")
            if marker is not None:
                marker(old_session, error)

        self._sync_runtime_to_store(session_id, old_session)
        refreshed = self.store.get(session_id) or config
        snapshot_args = {"apply_snapshot": apply_snapshot} if apply_snapshot is not None else {}
        candidate = await asyncio.to_thread(self._create_entry, refreshed, add_to_pool=False, **snapshot_args)
        if candidate is None:
            await self._persist_session_contexts(old_session)
            mark_failed("候选会话创建失败")
            return {
                "ok": False,
                "action": "restart",
                "session_id": session_id,
                "platform_id": self.platform_id,
                "old_runtime_preserved": True,
                "error": "候选会话创建失败",
            }
        try:
            if isinstance(candidate.session, AsyncSession):
                with progress_phase("candidate_prepare_ms"):
                    await self._prepare_session_async(candidate.session, **snapshot_args)
            if apply_snapshot is not None and not await progress_source_check(apply_snapshot.sources.is_current):
                raise RuntimeError("配置在候选准备期间已变化, 请重试应用")
            self._check_apply_runtime(runtime_is_current)
        except Exception as error:
            logger.error(f"[SessionManager] 候选会话准备失败: {session_id}, {error}\n{traceback.format_exc()}")
            with progress_phase("uninstall_ms"):
                await self._release_session_memory_async(candidate.session)
            await self._persist_session_contexts(old_session)
            mark_failed(str(error))
            return {
                "ok": False,
                "action": "restart",
                "session_id": session_id,
                "platform_id": self.platform_id,
                "old_runtime_preserved": True,
                "error": str(error),
            }

        if not self.pool.replace(session_id, entry, candidate, expected_generation=generation):
            with progress_phase("uninstall_ms"):
                await self._release_session_memory_async(candidate.session)
            current = self.pool.get(session_id)
            if current is not None:
                await self._persist_session_contexts(current.session)
            return {
                "ok": False,
                "action": "restart",
                "session_id": session_id,
                "platform_id": self.platform_id,
                "runtime_changed": True,
                "error": "会话运行时已变化",
            }
        with progress_phase("uninstall_ms"):
            await self._release_session_memory_async(old_session)
        await self._persist_session_contexts(candidate.session)
        logger.info(f"[SessionManager] 会话已热重启: {session_id}")
        return {
            "ok": True,
            "action": "restart",
            "session_id": session_id,
            "platform_id": self.platform_id,
            "runtime": self.get_session_runtime_metadata(session_id),
        }
    @staticmethod
    async def _persist_session_contexts(session: Session | AsyncSession) -> None:
        """
        在热重启操作锁内恢复最终实例的上下文数据库

        参数:
        - session: 成功时的新实例或失败时保留的旧实例
        """
        if safe_getattr(session, "session_ctx") is None:
            return
        for context in session._all_contexts().values():
            context._mark_dirty()   # 候选初始化与插件清理可能改写同一数据库, 最终实例的上下文为准
            synced = context._sync()
            if inspect.isawaitable(synced):
                await synced

    async def unload_session_async(self, session_id: str) -> dict[str, Any]:
        """
        卸载活跃实例但保留持久化冷配置

        参数:
        - session_id: 会话 ID

        返回:
        - dict[str, Any]: 卸载结果
        """
        entry = self.pool.get(session_id)
        if entry is None:
            return {
                "ok": True,
                "action": "unload",
                "session_id": session_id,
                "platform_id": self.platform_id,
                "active": False,
            }

        async with entry.async_operation_lock:
            if isinstance(entry.session, AsyncSession):
                return await self._unload_entry_locked(session_id, entry)
            with entry.sync_operation_lock:
                return await self._unload_entry_locked(session_id, entry)

    async def _unload_entry_locked(self, session_id: str, entry: SessionEntry) -> dict[str, Any]:
        """
        在已取得会话锁后卸载选定条目

        参数:
        - session_id: 活跃实例 ID
        - entry: 已取得操作锁的实例条目

        返回:
        - 卸载结果; 已替换条目不影响当前运行实例
        """
        if self.pool.list_entries().get(session_id) is not entry:
            return {
                "ok": True,
                "action": "unload",
                "session_id": session_id,
                "platform_id": self.platform_id,
                "active": False,
            }
        generation = entry.instance_generation
        self._sync_runtime_to_store(session_id, entry.session)
        removed = self.pool.remove(session_id, expected=entry, expected_generation=generation)
        if removed is None and session_id in self.pool.list_entries():
            logger.warning(f"[SessionManager] 卸载未提交, 会话运行代次已变化: {session_id}")
            return {"ok": False, "action": "unload", "session_id": session_id, "platform_id": self.platform_id,
                    "error": "会话运行时已变化, 请重试卸载"}
        if removed is not None:
            with progress_phase("uninstall_ms"):
                await self._release_session_memory_async(removed.session)
        logger.info(f"[SessionManager] 会话运行时已卸载: {session_id}")
        return {
            "ok": True,
            "action": "unload",
            "session_id": session_id,
            "platform_id": self.platform_id,
            "active": False,
        }
    def list_session_configs(self, limit: int = 200) -> List[SessionConfig]:
        """
        列出持久化 SessionConfig (来自 SQLite)

        参数:
        - limit: 最大返回数量 (默认 200)

        返回:
        - 会话配置列表 (SessionConfig)
        """
        return self.store.list(limit=limit)

    def list_registered_session_types(self) -> List[str]:
        """
        列出当前已注册的 session_type_name

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
        """
        更新已存在会话的持久化配置并返回更新后的 SessionConfig

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
        self._persist_config(cfg)
        return cfg

    # ---------- 调用入口 ----------
    def handle_call(self, user_call: UserCall) -> str:
        """
        同步处理用户调用

        参数:
        - user_call: 用户call

        实现逻辑:
        1. 根据 user_call.session_id 读取 SessionConfig
        2. 若不存在则按请求类型创建一条默认 SessionConfig
        3. 使用 SessionConfig 实例化会话(若池中无活跃实例)
        4. 保持原有池化/淘汰流程

        返回:
        - str: 同步处理用户调用
        """
        try:
            session_cfg = self._resolve_or_create_session_config(user_call)
            session_id = session_cfg.session_id or ""
            user_call.session_id = session_id

            entry = self._acquire_or_create_entry(session_cfg)
            if entry is None:
                logger.error(f"[SessionManager] handle_call 失败：会话创建失败，session_id={session_id}")
                return ""

            try:
                if isinstance(entry.session, AsyncSession):
                    logger.error(
                        f"[SessionManager] handle_call 失败：session_id={session_id} 对应异步会话类 "
                        f"{type(entry.session).__name__}，请使用 handle_call_async"
                    )
                    return ""

                with entry.sync_operation_lock:
                    if self.pool.list_entries().get(session_id) is not entry:
                        return ""
                    response = self._invoke_sync_session(entry.session, user_call)
                    self._sync_runtime_to_store(session_id, entry.session)
            finally:
                self.pool.release(entry)
            self.cleanup_idle_sessions()
            if isinstance(response, CommandAction):
                return self._handle_transition_sync(user_call, session_cfg, response)
            return "" if response is None else str(response)
        except Exception as e:
            logger.error(f"[SessionManager] handle_call 发生异常：{e}")
            return ""

    async def handle_call_async(self, user_call: UserCall) -> str:
        """
        异步处理用户调用

        参数:
        - user_call: 用户调用对象 (包含 session_id, method, params)

        返回:
        - 会话响应 (str) 或空字符串
        """
        try:
            session_cfg = self._resolve_or_create_session_config(user_call)
            session_id = session_cfg.session_id or ""
            user_call.session_id = session_id

            entry = await self._acquire_or_create_entry_async(session_cfg)
            if entry is None:
                abort_reply_turn()
                logger.error(f"[SessionManager] handle_call_async 失败：会话创建失败，session_id={session_id}")
                return ""

            try:
                async with entry.async_operation_lock:
                    if self.pool.list_entries().get(session_id) is not entry:
                        return ""
                    self._observe_group_apply(user_call, session_id, getattr(entry, "instance_generation", ""))
                    try:
                        group_plugins = self._group_plugin_target(session_cfg, user_call)
                        setattr(entry.session, "_satrap_group_plugins", group_plugins)
                        await self._prepare_session_async(entry.session)
                        await self._apply_group_session_overrides(session_cfg, entry.session, user_call, group_plugins)
                    except Exception as error:
                        self._report_group_apply(user_call, session_id, getattr(entry, "instance_generation", ""), str(error))
                        raise
                    self._report_group_apply(user_call, session_id, getattr(entry, "instance_generation", ""), None)

                    if isinstance(entry.session, AsyncSession):
                        response = await self._invoke_async_session(entry.session, user_call)
                        self._sync_runtime_to_store(session_id, entry.session)
                    else:
                        response = await SESSION_WORKERS.run(self._invoke_sync_entry, entry, user_call, session_id)
            finally:
                self.pool.release(entry)

            if isinstance(response, CommandAction):   # 当需要切换会话时
                response = await self._handle_transition_async(user_call, session_cfg, response)
                await self.cleanup_idle_sessions_async()
                return "" if response is None else str(response)   # 切换时返回

            await self.cleanup_idle_sessions_async()
            return "" if response is None else str(response)   # 正常返回

        except WorkerBusyError:
            abort_reply_turn()
            logger.warning(f"[SessionManager] 同步会话处理繁忙, session_id={user_call.session_id}")
            return "同步会话处理繁忙, 请稍后重试"
        except Exception as e:
            abort_reply_turn()
            logger.error(f"[SessionManager] handle_call_async 发生异常: {e}, 堆栈={traceback.format_exc()}")
            return ""

    def _transition_config(self, user_call: UserCall, source: SessionConfig, action: CommandAction) -> SessionConfig:
        """
        验证会话动作的归属, 创建动作与切换动作不互相冒充

        参数:
        - user_call: 当前宿主请求
        - source: 已执行命令的原会话配置
        - action: 原生命令返回的动作

        返回:
        - 已验证或新建的目标配置, 越界或目标无效时抛出 ValueError
        """
        target = action.target_session_id
        if action.action not in {"new_session", "switch", "switch_session"} or not isinstance(target, str) or not target.strip():
            raise ValueError("会话切换动作或目标无效")
        creating = action.action in {"new_session", "switch"}
        manager = self._user_mgr
        route = user_call.route
        if route is not None:
            if manager is None:
                raise ValueError("当前路由管理器不可用")
            bound = manager.store.get_context_session(route.owner, route.platform, route.session_type, route.provider, context_key=route.key)
            if bound is None or bound.session_id != source.session_id:
                raise ValueError("当前会话不再属于该路由")
            if not creating and target not in manager.store.list_context_history(route):
                raise ValueError("目标会话不属于当前对话范围")
        elif user_call.origin is not None:
            raise ValueError("平台会话切换缺少可信路由")
        existing = self.store.get(target)
        if creating:
            if existing is not None:
                raise ValueError("新会话 ID 已存在")
            return self._resolve_or_create_session_config(dataclasses.replace(
                user_call, session_id=target, session_provider=source.provider_name, session_type=source.session_type_name,
            ))
        if existing is None:
            raise ValueError("目标会话不存在")
        if route is not None and (existing.provider_name, existing.session_type_name) != (source.provider_name, source.session_type_name):
            raise ValueError("目标会话的 Provider 或类型不匹配")
        return existing

    def _publish_transition(self, user_call: UserCall, source: SessionConfig, target: SessionConfig, action: CommandAction) -> str:
        """
        目标初始化成功后比较交换持久路由, 最后发布窗口重置信号

        参数:
        - user_call: 待更新的本轮宿主请求
        - source: 切换前的真实会话配置
        - target: 已初始化的目标会话配置
        - action: 已校验的命令动作

        返回:
        - 路由绑定成功后的命令回执, 绑定失败抛出异常而不发布成功信号
        """
        target_id = target.session_id
        if not target_id:
            raise ValueError("目标会话没有 ID")
        if not source.session_type_name:
            raise ValueError("原会话没有可核验的类型")
        manager = self._user_mgr
        owner = ""
        if manager is not None:
            route = user_call.route
            if route is not None:
                owner = route.owner
                platform, session_type, provider, context_key = route.platform, route.session_type, route.provider, route.key
            else:
                parts = (source.session_id or "").split(":")
                if len(parts) < 3 or (parts[2] if len(parts) >= 4 else parts[1]).startswith("scope_"):
                    raise ValueError("无法确认当前会话归属")
                owner = parts[2] if len(parts) >= 4 else parts[1]
                platform = parts[1] if len(parts) >= 4 else parts[0]
                session_type, provider, context_key = source.session_type_name, source.provider_name, None
            if manager.storage_layout is not None:
                manager.storage_layout.bind_session(StorageScope(platform_id=manager.platform_id, user_id=owner, session_id=target_id))
            manager.store.upsert_context_session(owner, platform, session_type, target_id, provider,
                                                 context_key=context_key, expected_session_id=source.session_id)
        user_call.session_id = target_id
        if action.action in {"new_session", "switch"}:
            user_call.context_reset_at = user_call.received_at
        if manager is not None and owner:
            manager.bind_session(owner, target_id)
        return action.message

    def _handle_transition_sync(self, user_call: UserCall, source: SessionConfig, action: CommandAction) -> str:
        """
        同步初始化目标并在成功后提交会话动作

        参数:
        - user_call: 本轮宿主请求
        - source: 原会话配置
        - action: 命令动作

        返回:
        - 实际生效的回执或明确的切换失败说明
        """
        try:
            target = self._transition_config(user_call, source, action)
            entry = self._acquire_or_create_entry(target)
            if entry is None or isinstance(entry.session, AsyncSession):
                if entry is not None:
                    self.pool.release(entry)
                raise ValueError("同步入口无法初始化目标会话")
            try:
                with entry.sync_operation_lock:
                    self._prepare_session_sync(entry.session)
                    return self._publish_transition(user_call, source, target, action)
            finally:
                self.pool.release(entry)
        except Exception as error:
            logger.error(f"[SessionManager] 会话切换失败: {traceback.format_exc()}")
            return f"切换失败: {error}"

    async def _handle_transition_async(self, user_call: UserCall, source: SessionConfig, action: CommandAction) -> str:
        """
        异步初始化目标并在成功后提交会话动作

        参数:
        - user_call: 本轮宿主请求
        - source: 原会话配置
        - action: 命令动作

        返回:
        - 实际生效的回执或明确的切换失败说明
        """
        try:
            target = self._transition_config(user_call, source, action)
            entry = await self._acquire_or_create_entry_async(target)
            if entry is None:
                raise ValueError("无法初始化目标会话")
            try:
                async with entry.async_operation_lock:
                    group_plugins = self._group_plugin_target(target, user_call)
                    setattr(entry.session, "_satrap_group_plugins", group_plugins)
                    await self._prepare_session_async(entry.session)
                    await self._apply_group_session_overrides(target, entry.session, user_call, group_plugins)
                    return self._publish_transition(user_call, source, target, action)
            finally:
                self.pool.release(entry)
        except Exception as error:
            logger.error(f"[SessionManager] 会话切换失败: {traceback.format_exc()}")
            return f"切换失败: {error}"

    def _observe_group_apply(self, user_call: UserCall, session_id: str, instance_generation: str) -> None:
        """把群调用与当前实例的可信关联交给平台运行时"""
        observer = getattr(self, "group_apply_observer", None)
        if observer is None or user_call.group_config_revision is None:
            return
        try:
            observer(user_call, session_id, instance_generation)
        except Exception as error:
            logger.error(f"[SessionManager] 群会话实例关联上报失败: {error}")

    def _report_group_apply(
        self, user_call: UserCall, session_id: str, instance_generation: str, error: str | None,
    ) -> None:
        """将群会话安全轮次的应用结果交给平台运行时"""
        reporter = getattr(self, "group_apply_reporter", None)
        if reporter is None or user_call.group_config_revision is None:
            return
        try:
            reporter(user_call, session_id, instance_generation, error)
        except Exception as report_error:
            logger.error(f"[SessionManager] 群配置应用状态上报失败: {report_error}")

    async def retry_group_session_apply_async(
        self, session_id: str, user_call: UserCall, instance_generation: str,
        is_current: Callable[[], bool],
    ) -> bool:
        """在现有实例的会话锁内重试覆盖应用, 不发起模型对话"""
        entry = self.pool.list_entries().get(session_id)
        session_cfg = self.store.get(session_id)
        if entry is None or session_cfg is None or entry.instance_generation != instance_generation:
            return False
        async with entry.async_operation_lock:
            if (self.pool.list_entries().get(session_id) is not entry
                    or entry.instance_generation != instance_generation or not is_current()):
                return False
            try:
                plugins = self._group_plugin_target(session_cfg, user_call)
                setattr(entry.session, "_satrap_group_plugins", plugins)
                await self._prepare_session_async(entry.session)
                await self._apply_group_session_overrides(session_cfg, entry.session, user_call, plugins)
            except Exception as error:
                self._report_group_apply(user_call, session_id, instance_generation, str(error))
                return False
            if not is_current():
                return False
            self._report_group_apply(user_call, session_id, instance_generation, None)
            return True

    def _group_plugin_target(self, session_cfg: SessionConfig, user_call: UserCall) -> list[object] | None:
        """合并群插件设置, 保留会话实例插件配置的最高优先级"""
        overrides = user_call.group_session_overrides or {}
        if getattr(session_cfg, "provider_name", SESSION_CLASS_PROVIDER) != EDICTUM_PROVIDER:
            return None
        instance = session_cfg.session_config or {}
        if "plugins" in instance:
            plugins = instance["plugins"]
            return plugins if isinstance(plugins, list) else None
        if "plugins" not in overrides:
            return None

        resolved = self.provider_registry.resolve_definition(
            session_cfg.session_type_name or "", EDICTUM_PROVIDER,
        )
        if resolved is None:
            raise ValueError("群会话命名配置不可用")
        explicit = {"plugins": {"mode": "value", "value": overrides["plugins"]}}
        effective, _ = resolve_group_session(
            {}, explicit, {"plugins": resolved[1].metadata.get("plugins", [])},
        )
        plugins = effective["plugins"]
        return plugins if isinstance(plugins, list) else None

    async def _apply_group_session_overrides(
        self, session_cfg: SessionConfig, session: Session | AsyncSession,
        user_call: UserCall, group_plugins: list[object] | None,
    ) -> None:
        """在本轮会话锁内应用群配置, 不覆盖持久化的实例显式设置"""
        if getattr(session_cfg, "provider_name", SESSION_CLASS_PROVIDER) != EDICTUM_PROVIDER:
            return
        overrides = user_call.group_session_overrides or {}
        resolved = self.provider_registry.resolve_definition(
            session_cfg.session_type_name or "", EDICTUM_PROVIDER,
        )
        if resolved is None:
            raise ValueError("群会话命名配置不可用")
        provider, definition = resolved
        instance = session_cfg.session_config or {}
        base_model = instance.get("model_name") or definition.params.get("model_name") or "default"
        model_name = str(base_model if "model_name" in instance else overrides.get("model", base_model))
        applied_model = getattr(session, "_satrap_group_model_name", str(base_model))
        if model_name != applied_model:
            model_cfg = self._model_cfg_mgr.get_llm_config(name=model_name) if self._model_cfg_mgr else None
            if model_cfg is None or not model_cfg.api_key:
                raise ValueError("群模型配置不存在或缺少必要凭据")
            llm = build_llm_from_config(model_cfg, async_=isinstance(session, AsyncSession))
            self._apply_model_reload(session_cfg.session_id or "", session, llm, model_cfg)
        base_prompt = instance.get("system_prompt", definition.params.get("system_prompt"))
        prompt = base_prompt if "system_prompt" in instance else overrides.get("prompt", base_prompt)
        if prompt is None and hasattr(session, "_satrap_group_prompt"):
            prompt = ""
        if isinstance(prompt, str) and prompt != getattr(session, "_satrap_group_prompt", base_prompt):
            setattr(session, "_init_system_prompt", prompt)
            contexts = session._all_contexts()
            for context in contexts.values():
                compose_prompt = safe_getattr_callable(session, "compose_system_prompt")
                effective_prompt = compose_prompt(prompt, context) if compose_prompt is not None else prompt
                result = context.reset_system_prompt(effective_prompt)
                if inspect.isawaitable(result):
                    await result
            setattr(session, "_satrap_group_prompt", prompt)
        if group_plugins is not None:
            reconcile = getattr(provider, "reconcile_session_plugins_async", None)
            if callable(reconcile):
                outcome = reconcile(session, desired_plugins=group_plugins)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                if not isinstance(outcome, dict) or not outcome.get("ok", False):
                    raise RuntimeError("群插件配置应用失败")

    def reload_model_configs(self) -> None:
        """同步重载活跃同步会话的 LLM 实例"""
        model_cfg_mgr = self._model_cfg_mgr
        if not model_cfg_mgr:
            logger.warning("[SessionManager] reload_model_configs 跳过：无 ModelConfigManager")
            return

        for session_id, entry in self.pool.list_entries().items():
            if isinstance(entry.session, AsyncSession):
                logger.warning(
                    f"[SessionManager] 同步模型重载跳过异步会话: {session_id}"
                )
                continue
            prepared = self._prepare_model_reload(session_id, entry)
            if prepared is None:
                continue
            new_llm, llm_cfg = prepared
            with entry.sync_operation_lock:
                if self.pool.list_entries().get(session_id) is entry:
                    self._apply_model_reload(session_id, entry.session, new_llm, llm_cfg)

    async def reload_model_configs_async(self) -> None:
        """异步重载所有活跃会话的 LLM 实例"""
        if self._model_cfg_mgr is None:
            logger.warning("[SessionManager] reload_model_configs_async 跳过：无 ModelConfigManager")
            return

        for session_id, entry in self.pool.list_entries().items():
            prepared = self._prepare_model_reload(session_id, entry)
            if prepared is None:
                continue
            new_llm, llm_cfg = prepared
            async with entry.async_operation_lock:
                if self.pool.list_entries().get(session_id) is not entry:
                    continue
                if isinstance(entry.session, AsyncSession):
                    self._apply_model_reload(session_id, entry.session, new_llm, llm_cfg)
                else:
                    with entry.sync_operation_lock:
                        self._apply_model_reload(session_id, entry.session, new_llm, llm_cfg)

    def _edictum_plugin_entries(self, config_name: str | None, session_ids: set[str] | None) -> list[tuple[str, SessionEntry, str]]:
        """
        选择应用候选实例, 只读取会话目录与运行时名称

        参数:
        - config_name: 可选命名配置过滤
        - session_ids: 可选实例范围

        返回:
        - 会话 ID, 条目及配置名称, 保持池顺序
        """
        provider = self.provider_registry.get("edictum")
        runtime_name = safe_getattr_callable(provider, "plugin_config_name")
        selected: list[tuple[str, SessionEntry, str]] = []
        for session_id, entry in self.pool.list_entries().items():
            if str(safe_getattr(entry.session, "_satrap_provider_name") or "") != "edictum":
                continue
            if session_ids is not None and session_id not in session_ids:
                continue
            record = self.store.get(session_id)
            name = str(record.session_type_name if record is not None else runtime_name(entry.session) if runtime_name is not None else "")
            if config_name is not None and name != config_name:
                continue
            selected.append((session_id, entry, name))
        return selected

    def edictum_plugin_apply_scope(self, *, config_name: str | None = None, session_ids: set[str] | None = None,
                                 plugin_name: str | None = None,
                                 apply_targets: list[tuple[str, SessionEntry, str, str]] | None = None) -> dict[str, bool]:
        """
        汇总候选命名配置及旧引用, 新引用在捕获新文件后判断

        参数:
        - config_name: 可选命名配置过滤
        - session_ids: 可选实例范围
        - plugin_name: 可选旧插件引用标记
        - apply_targets: 可选来源准备前固定的实例范围

        返回:
        - 配置名称到旧引用是否存在的映射
        """
        references = safe_getattr_callable(self.provider_registry.get("edictum"), "references_plugin")
        scope: dict[str, bool] = {}
        targets = apply_targets if apply_targets is not None else self.edictum_runtime_apply_targets(config_name=config_name, session_ids=session_ids)
        for _session_id, entry, name, _generation in targets:
            old = bool(plugin_name is not None and references is not None and references(entry.session, plugin_name, desired_plugins=[]))
            scope[name] = scope.get(name, False) or old
        return scope

    def edictum_runtime_apply_targets(self, *, config_name: str | None = None,
                                      session_ids: set[str] | None = None) -> list[tuple[str, SessionEntry, str, str]]:
        """
        在来源准备前固定完整配置或插件应用实例范围与运行代次

        参数:
        - config_name: 可选命名配置过滤
        - session_ids: 可选实例范围

        返回:
        - 会话 ID, 条目, 原绑定及运行代次, 后续绑定变化明确失败
        """
        return [(sid, entry, name, entry.instance_generation) for sid, entry, name in self._edictum_plugin_entries(config_name, session_ids)]

    def _check_edictum_apply_entry(self, session_id: str, entry: SessionEntry, generation: str,
                                   runtime_is_current: Callable[[], bool] | None) -> None:
        """
        同步核验所属平台与当前驻留实例, 每次调用均读取实时状态

        参数:
        - session_id: 原计划会话
        - entry: 原计划驻留对象
        - generation: 原计划实例代次
        - runtime_is_current: 所属平台管理器实时核验器
        """
        self._check_apply_runtime(runtime_is_current)
        if self.pool.list_entries().get(session_id) is not entry or entry.instance_generation != generation:
            raise RuntimeError("会话运行时已变化")

    async def _verify_edictum_apply_entry(
        self, session_id: str, entry: SessionEntry, planned_name: str, generation: str,
        snapshot: EdictumPluginApplySnapshot | None, runtime_is_current: Callable[[], bool] | None,
        source_error: str,
    ) -> SessionConfig | None:
        """
        持有原操作锁时复核来源, await 前后都确认实例身份并检查持久化绑定

        参数:
        - session_id: 原计划会话
        - entry: 原计划驻留对象
        - planned_name: 捕获来源前固定的 Agent 名称
        - generation: 原实例代次
        - snapshot: 本次共享配置来源, None 保留实时读取入口
        - runtime_is_current: 所属平台管理器实时核验器
        - source_error: 原入口的来源变化说明

        返回:
        - 本轮重新读取的会话配置, 不沿用来源捕获前的绑定
        """
        self._check_edictum_apply_entry(session_id, entry, generation, runtime_is_current)
        if snapshot is not None and not await progress_source_check(snapshot.sources.is_current):
            raise RuntimeError(source_error)
        self._check_edictum_apply_entry(session_id, entry, generation, runtime_is_current)
        stored = self.store.get(session_id)
        if stored is not None and stored.session_type_name != planned_name:
            raise RuntimeError("Agent 绑定在等待期间已变化, 请重试应用")
        return stored

    async def _reconcile_edictum_entry(
        self, session_id: str, entry: SessionEntry, semaphore: asyncio.Semaphore,
        apply_locked: Callable[[], Awaitable[object]], snapshot: EdictumPluginApplySnapshot | None,
        *, plugin_only: bool,
    ) -> dict[str, Any]:
        """
        共用逐实例协调的锁顺序, 观测范围和失败封装, 保留两种应用的计时契约

        参数:
        - session_id: 原计划会话
        - entry: 原计划驻留对象
        - semaphore: 本批次的并发限制
        - apply_locked: 取得会话锁后执行的领域动作, 内部仍需实时核验
        - snapshot: 可选共享来源, 用于报告捕获耗时
        - plugin_only: 插件应用保留同步锁计时和原调试日志

        返回:
        - 含平台, 会话及原计时字段的逐实例结果, 取消仍向上传播
        """
        with progress_instance(self.platform_id, session_id):
            timings = PhaseTimings()
            started = time.perf_counter()
            lock_wait_ms = 0.0
            sync_lock_wait_ms = 0.0
            label = "Edictum 插件热更新" if plugin_only else "Edictum 完整应用"

            def timing_payload(*, failed: bool = False) -> dict[str, float]:
                """保留成功和失败路径原有的共享读取与同步锁耗时字段"""
                values = timings.to_payload() if plugin_only else {'total_ms': round((time.perf_counter() - started) * 1000, 3)}
                values['queue_and_lock_wait_ms'] = round(lock_wait_ms, 3)
                if plugin_only:
                    values['sync_lock_wait_ms'] = round(sync_lock_wait_ms, 3)
                if snapshot is not None and (plugin_only or not failed):
                    values['shared_config_read_ms'] = round(snapshot.read_ms, 3)
                return values

            try:
                async with progress_locks(semaphore, entry.async_operation_lock):
                    lock_wait_ms = (time.perf_counter() - started) * 1000
                    if isinstance(entry.session, AsyncSession):
                        result = await apply_locked()
                    else:
                        sync_started = time.perf_counter()
                        with entry.sync_operation_lock:
                            sync_lock_wait_ms = (time.perf_counter() - sync_started) * 1000
                            result = await apply_locked()
                payload: dict[str, Any] = cast(dict[str, Any], result).copy() if isinstance(result, dict) else {"ok": True}
                payload.update(session_id=session_id, platform_id=self.platform_id)
                payload['timings_ms'] = {**payload.get('timings_ms', {}), **timing_payload()}
                if plugin_only:
                    logger.debug(f"[SessionManager] 插件协调完成, 会话={session_id}, timings_ms={payload['timings_ms']}")
            except Exception as error:
                logger.error(f"[SessionManager] {label}失败: {session_id}, {error}\n{traceback.format_exc()}")
                payload = {"ok": False, "session_id": session_id, "platform_id": self.platform_id,
                           "error": str(error), "timings_ms": timing_payload(failed=True)}
            progress_result(payload)
            return payload

    async def reconcile_edictum_plugins_async(
        self,
        *,
        config_name: str | None = None,
        session_ids: set[str] | None = None,
        desired_plugins: object | None = None,
        plugin_name: str | None = None,
        concurrency: int = 4,
        apply_snapshot: EdictumPluginApplySnapshot | None = None,
        apply_targets: list[tuple[str, SessionEntry, str, str]] | None = None,
        runtime_is_current: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """
        对当前平台选定的活跃 Edictum 会话应用完整插件差量

        参数:
        - config_name: 可选 Edictum 配置名称过滤
        - session_ids: 可选会话 ID 过滤
        - desired_plugins: 可选目标插件配置覆盖
        - plugin_name: 可选插件引用过滤, 旧引用移除也需要协调
        - concurrency: 最大并发协调数
        - apply_snapshot: 可选后端跨平台共享快照, 默认由 Provider 准备
        - apply_targets: 可选来源准备前固定的实例范围与运行代次
        - runtime_is_current: 可选后端平台管理器身份复核, 不缓存结果

        返回:
        - list[dict[str, Any]]: 逐会话应用结果
        """
        provider = self.provider_registry.get("edictum")
        reconcile = safe_getattr_callable(provider, "reconcile_session_plugins_async")
        references = safe_getattr_callable(provider, "references_plugin")
        if reconcile is None:
            return []
        targets = apply_targets if apply_targets is not None else self.edictum_runtime_apply_targets(config_name=config_name, session_ids=session_ids)
        capture = safe_getattr_callable(provider, "capture_plugin_application")
        if apply_snapshot is None and capture is not None:
            scope = self.edictum_plugin_apply_scope(plugin_name=plugin_name, apply_targets=targets)
            apply_snapshot = await asyncio.to_thread(capture, scope, desired_plugins=desired_plugins, plugin_name=plugin_name,
                                                     model_managers=(self._model_cfg_mgr,) if targets and self._model_cfg_mgr is not None else (), include_main_models=False)
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def reconcile_one(session_id: str, entry: SessionEntry, planned_name: str, generation: str) -> dict[str, Any]:
            """协调一个活跃 Edictum 会话"""
            async def apply_locked() -> object:
                """执行插件差量前核验同一实例, 绑定及来源"""
                await self._verify_edictum_apply_entry(
                    session_id, entry, planned_name, generation, apply_snapshot, runtime_is_current,
                    "插件配置在等待期间已变化, 请重试应用",
                )
                runtime_name = safe_getattr_callable(provider, "plugin_config_name")
                if runtime_name is not None and runtime_name(entry.session) != planned_name:
                    raise RuntimeError("运行时 Agent 绑定与目标不一致, 请应用完整 Agent 配置")
                snapshot_args = {"apply_snapshot": apply_snapshot} if apply_snapshot is not None else {}
                result = reconcile(entry.session, desired_plugins=desired_plugins, **snapshot_args)
                result = await result if inspect.isawaitable(result) else result
                self._check_apply_runtime(runtime_is_current)
                return result

            return await self._reconcile_edictum_entry(
                session_id, entry, semaphore, apply_locked, apply_snapshot, plugin_only=True,
            )

        pending: list[Awaitable[dict[str, Any]]] = []
        for session_id, entry, name, generation in targets:
            if apply_snapshot is not None and not apply_snapshot.includes_config(name):
                continue
            selected_plugins = [spec.name for spec in apply_snapshot.specs_for(name)] if apply_snapshot is not None else desired_plugins
            if plugin_name is not None and references is not None and not references(entry.session, plugin_name, desired_plugins=selected_plugins):
                continue
            pending.append(reconcile_one(session_id, entry, name, generation))
        return list(await asyncio.gather(*pending)) if pending else []

    def preview_edictum_plugins(
        self,
        *,
        config_name: str | None = None,
        session_ids: set[str] | None = None,
        desired_plugins: object | None = None,
    ) -> list[dict[str, Any]]:
        """
        预览当前平台活跃 Edictum 会话的插件变更影响

        参数:
        - config_name: 可选 Edictum 配置名称过滤
        - session_ids: 可选会话 ID 过滤
        - desired_plugins: 可选目标插件配置覆盖

        返回:
        - list[dict[str, Any]]: 逐会话影响预览
        """
        provider = self.provider_registry.get("edictum")
        preview = safe_getattr_callable(provider, "preview_session_plugins")
        if preview is None:
            return []
        results: list[dict[str, Any]] = []
        for session_id, entry in self.pool.list_entries().items():
            provider_name = str(safe_getattr(entry.session, "_satrap_provider_name") or "")
            if provider_name != "edictum":
                continue
            if session_ids is not None and session_id not in session_ids:
                continue
            session_config = self.store.get(session_id)
            if config_name is not None and (
                session_config is None
                or session_config.session_type_name != config_name
            ):
                continue
            result = preview(entry.session, desired_plugins=desired_plugins)
            payload: dict[str, Any] = (
                cast(dict[str, Any], result).copy()
                if isinstance(result, dict)
                else {"ok": True}
            )
            payload["session_id"] = session_id
            payload["platform_id"] = self.platform_id
            results.append(payload)
        return results

    def preview_edictum_runtime(
        self,
        *,
        config_name: str | None = None,
        session_ids: set[str] | None = None,
        desired_config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        预览完整 Edictum 配置对活跃会话的影响

        参数:
        - config_name: 可选 Edictum 配置名称过滤
        - session_ids: 可选会话 ID 过滤
        - desired_config: 可选的尚未保存目标配置

        返回:
        - list[dict[str, Any]]: 逐会话影响预览
        """
        provider = self.provider_registry.get(EDICTUM_PROVIDER)
        preview = safe_getattr_callable(provider, "preview_session_runtime")
        if preview is None:
            return []
        results: list[dict[str, Any]] = []
        for session_id, entry in self.pool.list_entries().items():
            provider_name = str(safe_getattr(entry.session, "_satrap_provider_name") or "")
            if provider_name != EDICTUM_PROVIDER:
                continue
            if session_ids is not None and session_id not in session_ids:
                continue
            stored = self.store.get(session_id)
            if config_name is not None and (
                stored is None or stored.session_type_name != config_name
            ):
                continue
            result = preview(entry.session, desired_config=desired_config)
            payload: dict[str, Any] = (
                cast(dict[str, Any], result).copy()
                if isinstance(result, dict)
                else {"ok": True}
            )
            payload["session_id"] = session_id
            payload["platform_id"] = self.platform_id
            results.append(payload)
        return results

    async def reconcile_edictum_runtime_async(
        self,
        *,
        config_name: str | None = None,
        session_ids: set[str] | None = None,
        desired_config: dict[str, Any] | None = None,
        concurrency: int = 4,
        apply_snapshot: EdictumPluginApplySnapshot | None = None,
        apply_targets: list[tuple[str, SessionEntry, str, str]] | None = None,
        runtime_is_current: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """
        对活跃 Edictum 会话应用插件差量、热重启或卸载

        参数:
        - config_name: 可选 Edictum 配置名称过滤
        - session_ids: 可选会话 ID 过滤
        - desired_config: 可选目标配置覆盖
        - concurrency: 最大并发会话数
        - apply_snapshot: 可选后端跨平台共享目标, 默认由 Provider 捕获
        - apply_targets: 可选准备前固定的实例范围, 默认在本入口捕获
        - runtime_is_current: 可选后端平台管理器身份复核, 不缓存结果

        返回:
        - list[dict[str, Any]]: 逐会话协调结果
        """
        provider = self.provider_registry.get(EDICTUM_PROVIDER)
        preview = safe_getattr_callable(provider, "preview_session_runtime")
        reconcile_plugins = safe_getattr_callable(provider, "reconcile_session_plugins_async")
        if preview is None:
            return []
        targets = apply_targets if apply_targets is not None else self.edictum_runtime_apply_targets(config_name=config_name, session_ids=session_ids)
        capture = safe_getattr_callable(provider, "capture_plugin_application")
        if apply_snapshot is None and capture is not None:
            scope = {name: False for _sid, _entry, name, _generation in targets}
            apply_snapshot = await asyncio.to_thread(capture, scope, desired_config=desired_config,
                                                     model_managers=(self._model_cfg_mgr,) if targets and self._model_cfg_mgr is not None else ())
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def reconcile_one(session_id: str, entry: SessionEntry, planned_name: str, generation: str) -> dict[str, Any]:
            """协调一个活跃 Edictum 会话"""
            async def apply_locked() -> dict[str, Any]:
                """在共同生命周期核验后执行完整目标预览与协调"""
                stored = await self._verify_edictum_apply_entry(
                    session_id, entry, planned_name, generation, apply_snapshot, runtime_is_current,
                    "配置在等待期间已变化, 请重试应用",
                )
                snapshot_args: dict[str, Any] = {}
                if apply_snapshot is not None:
                    runtime_name = safe_getattr_callable(provider, "plugin_config_name")
                    name = stored.session_type_name if stored is not None else runtime_name(entry.session) if runtime_name is not None else None
                    snapshot_args = {"apply_snapshot": apply_snapshot, "target_config_name": name,
                                     "instance_overrides": dict(stored.session_config or {}) if stored is not None else None}
                raw_plan = preview(entry.session, desired_config=desired_config, **snapshot_args)
                plan = (
                    cast(dict[str, Any], raw_plan)
                    if isinstance(raw_plan, dict)
                    else {}
                )
                if not plan.get("ok", True):
                    return {**plan, "session_id": session_id, "platform_id": self.platform_id}
                if apply_snapshot is not None and plan.get("action") != "unload" and self._model_cfg_mgr is not None and stored is not None:
                    getter = safe_getattr_callable(provider, "get_definition")
                    definition = getter(planned_name, apply_snapshot=apply_snapshot) if getter is not None else None
                    if definition is None:
                        raise RuntimeError("Agent 定义在模型核验时不存在")
                    llm_config = self._model_config_for(stored, definition, apply_snapshot=apply_snapshot)
                    if llm_config is not None and safe_getattr(entry.session, "_satrap_model_config_fingerprint") != llm_config_fingerprint(llm_config):
                        plan["action"] = "restart"
                        plan["changed_fields"] = [*plan.get("changed_fields", []), "model_config"]
                if apply_snapshot is not None and not await progress_source_check(apply_snapshot.sources.is_current):
                    raise RuntimeError("配置在目标核验期间已变化, 请重试应用")
                self._check_edictum_apply_entry(session_id, entry, generation, runtime_is_current)
                action = str(plan.get("action", "noop"))
                if action == "restart":
                    return await self._restart_entry_locked(session_id, entry, apply_snapshot=apply_snapshot, runtime_is_current=runtime_is_current)
                if action == "unload":
                    return await self._unload_entry_locked(session_id, entry)
                if action == "reconcile_plugins" and reconcile_plugins is not None:
                    target_name = stored.session_type_name if stored is not None else config_name
                    definition = provider.get_definition(target_name or "") if apply_snapshot is None and desired_config is None and provider is not None else None
                    desired_plugins = desired_config.get("plugins", []) if desired_config is not None else definition.metadata.get("plugins", []) if definition is not None else None
                    result = reconcile_plugins(entry.session, desired_plugins=desired_plugins,
                                               **({"apply_snapshot": apply_snapshot} if apply_snapshot is not None else {}))
                    if inspect.isawaitable(result):
                        result = await result
                    self._check_apply_runtime(runtime_is_current)
                    payload: dict[str, Any] = (
                        cast(dict[str, Any], result).copy()
                        if isinstance(result, dict)
                        else {"ok": True}
                    )
                    payload["action"] = action
                    payload["session_id"] = session_id
                    payload["platform_id"] = self.platform_id
                    return payload
                return {
                    "ok": True,
                    "action": "noop",
                    "session_id": session_id,
                    "platform_id": self.platform_id,
                }

            return await self._reconcile_edictum_entry(
                session_id, entry, semaphore, apply_locked, apply_snapshot, plugin_only=False,
            )

        pending: list[Awaitable[dict[str, Any]]] = []
        for session_id, entry, name, generation in targets:
            pending.append(reconcile_one(session_id, entry, name, generation))
        return list(await asyncio.gather(*pending)) if pending else []

    @staticmethod
    def _check_apply_runtime(runtime_is_current: Callable[[], bool] | None) -> None:
        """每次应用边界实时检查所属平台, 已失效批次交给原错误封装记录并返回"""
        if runtime_is_current is not None and not runtime_is_current():
            raise RuntimeError("平台运行管理器已变化, 请重试应用")

    # ---------- 查询/清理 ----------
    def list_sessions(self) -> List[SessionMetadata]:
        """
        列出当前活跃会话 (内存池快照)

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
        """
        同步清理闲置会话 (仅影响活跃池, 不删除持久化配置)

        参数:
        - max_idle_seconds: 最大闲置时间 (默认 3600 秒)
        """
        try:
            removed = self.pool.collect_idle(max_idle_seconds=max_idle_seconds)
            for session_id, entry in removed:
                self._sync_runtime_to_store(session_id, entry.session)
                self._release_session_memory(entry.session)
                logger.info(f"[SessionManager] 已清理闲置会话：{session_id}")
        except Exception as e:
            logger.error(f"[SessionManager] cleanup_idle_sessions 失败：{e}")

    async def cleanup_idle_sessions_async(self, max_idle_seconds: int = 3600):
        """
        异步清理闲置会话 (仅影响活跃池, 不删除持久化配置)

        参数:
        - max_idle_seconds: 最大闲置时间 (默认 3600 秒)
        """
        try:
            removed = self.pool.collect_idle(max_idle_seconds=max_idle_seconds)
            for session_id, entry in removed:
                self._sync_runtime_to_store(session_id, entry.session)
                await self._release_session_memory_async(entry.session)
                logger.info(f"[SessionManager] 已清理闲置会话：{session_id}")
        except Exception as e:
            logger.error(f"[SessionManager] cleanup_idle_sessions_async 失败：{e}")

    def remove_session(self, session_id: str, remove_config: bool = False) -> bool:
        """
        移除活跃会话 (同步)

        参数:
        - session_id: 会话 ID
        - remove_config: 为 False 时仅移除内存活跃实例; 为 True 时同时删除 SQL 中的配置

        返回:
        - 会话空闲并成功移除时返回 True; 会话正在执行或处理失败时返回 False
        """
        try:
            entry = self.pool.get(session_id)
            if entry is not None:
                removed = self.pool.remove_if_idle(session_id, expected=entry)
                if removed is None:
                    logger.warning(f"[SessionManager] 会话正在执行, 同步删除已跳过: {session_id}")
                    return False
                self._sync_runtime_to_store(session_id, removed.session)
                self._release_session_memory(removed.session)
            if remove_config:
                StorageMaintenanceService(self.storage_layout).archive_session(
                    self.platform_id,
                    session_id,
                    database_path=self.store.db_path,
                )
            return True
        except Exception as e:
            logger.error(f"[SessionManager] remove_session 失败：session_id={session_id}, 错误={e}")
            return False

    async def remove_session_async(self, session_id: str, remove_config: bool = False) -> bool:
        """
        移除活跃会话 (异步)

        参数:
        - session_id: 会话 ID
        - remove_config: 为 False 时仅移除内存活跃实例; 为 True 时同时删除 SQL 中的配置

        返回:
        - 会话成功移除时返回 True; 并发替换导致目标不再匹配或处理失败时返回 False
        """
        try:
            entry = self.pool.get(session_id)
            if entry is not None:
                async with entry.async_operation_lock:
                    if isinstance(entry.session, AsyncSession):
                        removed = self.pool.remove(session_id, expected=entry)
                        if removed is not None:
                            self._sync_runtime_to_store(session_id, removed.session)
                    else:
                        with entry.sync_operation_lock:
                            removed = self.pool.remove(session_id, expected=entry)
                            if removed is not None:
                                self._sync_runtime_to_store(session_id, removed.session)
                    if removed is None:
                        current = self.pool.list_entries().get(session_id)
                        if current is not None:
                            logger.warning(
                                f"[SessionManager] 会话已被并发替换, 异步删除已跳过: {session_id}"
                            )
                            return False
                    else:
                        await self._release_session_memory_async(removed.session)
            if remove_config:
                StorageMaintenanceService(self.storage_layout).archive_session(
                    self.platform_id,
                    session_id,
                    database_path=self.store.db_path,
                )
            return True
        except Exception as e:
            logger.error(f"[SessionManager] remove_session_async 失败：session_id={session_id}, 错误={e}")
            return False

    async def delete_sessions_async(self, session_ids: List[str]) -> List[str]:
        """
        批量删除活跃实例, 持久化配置和用户侧引用

        参数:
        - session_ids: 待删除的会话 ID 列表

        返回:
        - List[str]: 实际删除的会话 ID 列表
        """
        requested = list(dict.fromkeys(session_id.strip() for session_id in session_ids if session_id.strip()))
        existing = [session_id for session_id in requested if self.store.get(session_id) is not None]
        for session_id in existing:
            await self.remove_session_async(session_id, remove_config=True)
        deleted = [session_id for session_id in existing if self.store.get(session_id) is None]
        if deleted and self._user_mgr is not None:
            self._user_mgr.remove_session_references(deleted)
        return deleted

    @property
    def class_cfg_mgr(self) -> SessionClassConfigManager | None:
        """
        获取关联的 SessionClassConfigManager

        返回:
        - SessionClassConfigManager | None: 关联的 SessionClassConfigManager
        """
        return self._class_cfg_mgr

    @class_cfg_mgr.setter
    def class_cfg_mgr(self, mgr: SessionClassConfigManager | None):
        """
        设置关联的 SessionClassConfigManager

        参数:
        - mgr: 管理器实例
        """
        self._class_cfg_mgr = mgr
        self.session_class_provider.set_config_manager(mgr)

    @property
    def user_manager(self):
        """
        获取关联的 UserManager

        返回:
        - 关联的 UserManager
        """
        return self._user_mgr

    @user_manager.setter
    def user_manager(self, mgr: UserManager | None):
        """
        设置关联的 UserManager

        参数:
        - mgr: 管理器实例
        """
        self._user_mgr = mgr

    @property
    def model_config_manager(self):
        """
        获取关联的 ModelConfigManager

        返回:
        - 关联的 ModelConfigManager
        """
        return self._model_cfg_mgr

    @model_config_manager.setter
    def model_config_manager(self, mgr: ModelConfigManager | None):
        """
        设置关联的 ModelConfigManager

        参数:
        - mgr: 管理器实例
        """
        self._model_cfg_mgr = mgr

    # ---------- 内部逻辑 ----------
    def _resolve_or_create_session_config(self, user_call: UserCall) -> SessionConfig:
        """
        按 user_call 解析 SessionConfig, 不存在则创建并持久化

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

        requested_provider = (user_call.session_provider or SESSION_CLASS_PROVIDER).strip()
        requested_type = (user_call.session_type or self.default_session_type).strip()
        try:
            resolved = self.provider_registry.resolve_definition(requested_type, requested_provider)
        except ValueError:
            resolved = None
        if resolved is None:
            logger.warning(
                f"[SessionManager] 未知会话定义 provider={requested_provider}, name={requested_type}, "
                f"回退到 {SESSION_CLASS_PROVIDER}:{self.default_session_type}"
            )
            requested_type = self.default_session_type
            resolved = self.provider_registry.resolve_definition(
                requested_type,
                SESSION_CLASS_PROVIDER,
            )

        if resolved is None:
            self.registry.register(self.default_session_type, Session)
            resolved = self.provider_registry.resolve_definition(
                self.default_session_type,
                SESSION_CLASS_PROVIDER,
            )
        if resolved is None:
            raise ValueError(f"无法解析默认会话定义: {self.default_session_type}")
        provider, definition = resolved

        if not definition.enabled:
            logger.warning(
                f"[SessionManager] 会话类型 {requested_type} 已被禁用"
            )
            requested_type = self.default_session_type
            fallback = self.provider_registry.resolve_definition(
                requested_type,
                SESSION_CLASS_PROVIDER,
            )
            if fallback is None:
                raise ValueError(f"无法解析默认会话定义: {self.default_session_type}")
            provider, definition = fallback

        sid = user_call.session_id or _short_uid()
        now = time.time()
        cfg = SessionConfig(
            session_id=sid,
            session_type_name=requested_type,
            provider_name=provider.provider_name,
            created_at=now,
            last_used_at=now,
            message_count=0,
            session_config=(
                {}
                if provider.provider_name == EDICTUM_PROVIDER
                else dict(definition.params)
            ),
        )
        self._persist_config(cfg)
        return cfg

    def _create_entry(
        self,
        session_cfg: SessionConfig,
        *,
        add_to_pool: bool = True,
        apply_snapshot: EdictumPluginApplySnapshot | None = None,
    ) -> Optional[SessionEntry]:
        """
        根据 SessionConfig 创建活跃会话并放入会话池

        参数:
        - session_cfg: 会话配置 (SessionConfig)
        - add_to_pool: 是否立即放入活跃池, 热重启候选实例传 False
        - apply_snapshot: 可选命名定义和插件来源, 默认沿用正常创建流程

        返回:
        - 会话条目 (SessionEntry)
        """
        session_id = session_cfg.session_id or ""
        session_type = session_cfg.session_type_name or self.default_session_type

        provider_name = session_cfg.provider_name or SESSION_CLASS_PROVIDER
        if apply_snapshot is not None and provider_name == EDICTUM_PROVIDER:
            provider = self.provider_registry.get(provider_name)
            definition_getter = safe_getattr_callable(provider, "get_definition")
            definition = definition_getter(session_type, apply_snapshot=apply_snapshot) if definition_getter is not None else None
            resolved = (provider, definition) if provider is not None and definition is not None else None
        else:
            resolved = self.provider_registry.resolve_definition(session_type, provider_name)
        if resolved is None:
            logger.error(
                f"[SessionManager] 创建会话失败：未知会话定义 provider={provider_name}, "
                f"session_type_name={session_type}"
            )
            return None
        provider, definition = resolved

        session: Session | AsyncSession | None = None
        try:
            current_params = dict(session_cfg.session_config or {})
            merged = dict(definition.params)
            merged.update(current_params)
            session_cfg = dataclasses.replace(
                session_cfg,
                provider_name=provider.provider_name,
                session_config=merged,
            )
            setattr(session_cfg, "_satrap_instance_overrides", current_params)
            # 合并 Provider 定义参数到实例级配置 (实例级优先)

            model_cfg_mgr = self._model_cfg_mgr
            # 读取 model_name -> 通过 ModelConfigManager 构建 LLM/AsyncLLM 实例

            llm_instance = None
            llm_cfg = None
            if model_cfg_mgr:
                llm_cfg = self._model_config_for(session_cfg, definition, apply_snapshot=apply_snapshot)
                if llm_cfg and llm_cfg.api_key:
                    with progress_phase("model_build_ms"):
                        llm_instance = build_llm_from_config(
                            llm_cfg, async_=definition.is_async
                        )
                else:
                    logger.warning(
                        f"[SessionManager] LLM 配置 '{llm_cfg.name if llm_cfg is not None else 'default'}' 不存在或缺少 api_key, "
                        f"请先通过 'satrap model set' 配置"
                    )
            snapshot_args = {"apply_snapshot": apply_snapshot} if apply_snapshot is not None else {}
            session = provider.create_session(session_cfg, llm=llm_instance, **snapshot_args)
            setattr(session, "plugin_environment", self.plugin_environment)
            if llm_cfg is not None:
                self._apply_session_context_config(session, llm_cfg)
                setattr(session, "_satrap_model_config_fingerprint", llm_config_fingerprint(llm_cfg))
            self._apply_storage_scope(session, session_id)
            try:
                setattr(session, "_satrap_provider_name", provider.provider_name)
            except Exception:
                pass
            # 注入 UserManager, 使 Session 能访问当前用户的所有上下文

            user_mgr = self._user_mgr
            if user_mgr is not None:
                session._user_manager = user_mgr
            if not isinstance(session, AsyncSession):
                self._prepare_session_sync(session, **snapshot_args)
        except Exception as e:
            logger.error(
                f"[SessionManager] 创建会话实例失败：session_type_name={session_type}, "
                f"session_id={session_id}, 错误={e}\n{traceback.format_exc()}"
            )
            if session is not None and not isinstance(session, AsyncSession):
                try:
                    self._release_session_memory(session)
                except Exception as cleanup_error:
                    logger.error(f"[SessionManager] 失败候选清理失败: {session_id}, {cleanup_error}\n{traceback.format_exc()}")
            return None

        if not add_to_pool:
            now = time.time()
            return SessionEntry(
                session=session,
                session_type=session_type,
                created_at=now,
                last_used=now,
            )

        try:
            evicted = self.pool.put(session_id, session, session_type)
        except SessionPoolCapacityError as e:
            self._release_session_memory(session)
            logger.warning(
                f"[SessionManager] 会话池容量已满: session_id={session_id}, 错误={e}"
            )
            return None
        except Exception as e:
            self._release_session_memory(session)
            logger.error(f"[SessionManager] 放入会话池失败：session_id={session_id}, 错误={e}")
            return None

        if evicted:
            evicted_id, evicted_entry = evicted
            self._sync_runtime_to_store(evicted_id, evicted_entry.session)
            self._release_session_memory(evicted_entry.session)
            logger.info(f"[SessionManager] 已按 LRU 淘汰会话：{evicted_id}")

        entry = self.pool.get(session_id)
        if entry is None:
            logger.error(f"[SessionManager] 创建会话条目失败：session_id={session_id}")
            return None
        return entry

    def _model_config_for(self, session_cfg: SessionConfig, definition: SessionProviderDefinition, *,
                          apply_snapshot: EdictumPluginApplySnapshot | None = None) -> LLMConfig | None:
        """
        以同一合成规则选择主模型, 完整应用时只读捕获来源

        参数:
        - session_cfg: 最新实例覆盖
        - definition: 当前或已捕获命名定义
        - apply_snapshot: 可选完整运行应用来源

        返回:
        - 独立模型配置, 未接入模型服务时为 None
        """
        if self._model_cfg_mgr is None:
            return None
        key = definition.model_key or "model_name"
        name = (session_cfg.session_config or {}).get(key) or definition.params.get(key) or "default"
        models = apply_snapshot.models_for(self._model_cfg_mgr) if apply_snapshot is not None else self._model_cfg_mgr
        return models.get_llm_config(name=name) if models is not None else None

    def _get_or_create_entry(self, session_cfg: SessionConfig) -> SessionEntry | None:
        """
        原子获取或创建会话条目

        参数:
        - session_cfg: 会话配置

        返回:
        - 活跃会话条目; 创建失败时返回 None
        """
        session_id = session_cfg.session_id or ""
        with self._entry_creation_lock:
            entry = self.pool.get(session_id)
            if entry is None:
                entry = self._create_entry(session_cfg)
            return entry

    def _acquire_or_create_entry(self, session_cfg: SessionConfig) -> SessionEntry | None:
        """
        原子获取或创建会话条目并取得使用租约

        参数:
        - session_cfg: 会话配置

        返回:
        - 已取得租约的会话条目; 创建失败时返回 None
        """
        session_id = session_cfg.session_id or ""
        with self._entry_creation_lock:
            entry = self.pool.acquire(session_id)
            if entry is not None:
                return entry
            created = self._create_entry(session_cfg)
            if created is None:
                return None
            return self.pool.acquire(session_id, expected=created)

    async def _get_or_create_entry_async(
        self,
        session_cfg: SessionConfig,
    ) -> SessionEntry | None:
        """
        在线程工作器中获取或创建会话条目

        参数:
        - session_cfg: 会话配置

        返回:
        - 活跃会话条目; 创建失败时返回 None
        """
        return await asyncio.to_thread(self._get_or_create_entry, session_cfg)

    async def _acquire_or_create_entry_async(
        self,
        session_cfg: SessionConfig,
    ) -> SessionEntry | None:
        """
        在线程工作器中获取或创建会话条目并取得使用租约

        参数:
        - session_cfg: 会话配置

        返回:
        - 已取得租约的会话条目; 创建失败时返回 None
        """
        return await asyncio.to_thread(self._acquire_or_create_entry, session_cfg)

    def _prepare_model_reload(
        self,
        session_id: str,
        entry: SessionEntry,
    ) -> tuple[LLM | AsyncLLM, LLMConfig] | None:
        """
        构建会话热重载所需的新模型实例

        参数:
        - session_id: 会话 ID
        - entry: 当前会话条目

        返回:
        - 新模型实例与配置; 配置不可用时返回 None
        """
        model_cfg_mgr = self._model_cfg_mgr
        session_cfg = self.store.get(session_id)
        if model_cfg_mgr is None or session_cfg is None:
            return None
        cfg_params = session_cfg.session_config or {}
        session_type = session_cfg.session_type_name or entry.session_type
        provider_name = session_cfg.provider_name or SESSION_CLASS_PROVIDER
        resolved = self.provider_registry.resolve_definition(session_type, provider_name)
        definition = resolved[1] if resolved is not None else None
        model_name_key = definition.model_key if definition is not None else "model_name"
        model_name = cfg_params.get(model_name_key)
        if not model_name and definition is not None:
            model_name = definition.params.get(model_name_key)
        llm_cfg = model_cfg_mgr.get_llm_config(name=model_name or "default")
        if not llm_cfg or not llm_cfg.api_key:
            return None
        new_llm = build_llm_from_config(
            llm_cfg,
            async_=isinstance(entry.session, AsyncSession),
        )
        return new_llm, llm_cfg

    def _apply_model_reload(
        self,
        session_id: str,
        session: Session | AsyncSession,
        new_llm: LLM | AsyncLLM,
        llm_cfg: LLMConfig,
    ) -> None:
        """
        把已构建的模型实例应用到当前会话

        参数:
        - session_id: 会话 ID
        - session: 当前会话实例
        - new_llm: 新模型实例
        - llm_cfg: 新模型配置
        """
        setattr(session, "_satrap_group_model_name", None)   # 外部模型替换前失效群覆盖缓存, 失败后下一轮仍会重试
        # 同步与异步模型类型由会话类型决定, reload_llm 恒被调用
        if isinstance(session, AsyncSession):
            session.reload_llm(cast(AsyncLLM, new_llm))
        else:
            session.reload_llm(cast(LLM, new_llm))
        self._apply_session_context_config(session, llm_cfg)
        for attr in ("_wf", "wf", "workflow", "_workflow", "main_wf"):
            workflow = safe_getattr(session, attr)
            reset_llm = safe_getattr_callable(workflow, "reset_llm")
            if reset_llm is not None:
                reset_llm(new_llm)
            elif workflow is not None and hasattr(workflow, "llm"):
                workflow.llm = new_llm
        applied_name = getattr(llm_cfg, "name", None)
        setattr(session, "_satrap_model_config_fingerprint", llm_config_fingerprint(llm_cfg))
        setattr(session, "_satrap_group_model_name", applied_name if isinstance(applied_name, str) else None)
        # 所有模型引用更新成功后记录实际应用名称, 未命名配置保留待重试状态
        logger.info(f"[SessionManager] 已刷新会话 LLM 配置: {session_id}")

    def _persist_config(self, config: SessionConfig) -> None:
        """
        持久化会话配置并初始化其私有目录

        参数:
        - config: 会话配置
        """
        self.store.upsert(config)
        session_id = str(config.session_id or "").strip()
        if session_id:
            self.storage_layout.bind_session(
                StorageScope(platform_id=self.platform_id, session_id=session_id)
            )

    @staticmethod
    def _apply_session_context_config(
        session: Session | AsyncSession,
        config: LLMConfig,
    ) -> None:
        """
        在会话具备上下文配置能力时应用模型策略

        参数:
        - session: 运行时会话
        - config: LLM 配置
        """
        method = safe_getattr(session, "apply_context_config")
        if not callable(method):
            return
        class_method = getattr(type(session), "apply_context_config", None)
        uses_base_method = class_method in {
            Session.apply_context_config,
            AsyncSession.apply_context_config,
        }
        if uses_base_method and safe_getattr(session, "session_ctx") is None:
            return
        method(config)

    def _apply_storage_scope(self, session: Session | AsyncSession, session_id: str) -> None:
        """
        向运行时会话注入平台与私有目录

        参数:
        - session: 运行时会话
        - session_id: 会话 ID
        """

        session.plugin_override_store = SessionOverrideStore(self.storage_layout.platform_db(self.platform_id))
        session.plugin_model_manager = self._model_cfg_mgr
        session.storage_layout = self.storage_layout
        root = self.storage_layout.bind_session(
            StorageScope(platform_id=self.platform_id, session_id=session_id)
        )
        setattr(session, "storage_platform_id", self.platform_id)
        bind_environment(session, EnvironmentSnapshot(
            workspace_root=root / "sandbox",
            sandbox_root=root / "sandbox",
            upload_root=root / "uploads",
            session_root=root,
            artifacts_root=root / "artifacts",
            scope=f"{self.platform_id}:{session_id}",
        ))
        setattr(session, "memory_db", str(self.storage_layout.platform_db(self.platform_id)))
        setattr(session, "memory_scope", f"session:{session_id}")

    def _sync_runtime_to_store(self, session_id: str, session: Session | AsyncSession):
        """
        将内存中的 last_used/message_count 回写到 SQLite 数据库

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
        """
        同步执行会话

        参数:
        - session: 同步会话实例 (Session)
        - user_call: 用户调用对象 (UserCall)

        返回:
        - 会话执行结果
        """
        try:
            run_method = session.run
            args = SessionManager._build_run_args(run_method, user_call)
            with bind_call_origin(user_call.origin), bind_conversation_route(user_call.route), buffer_session_reply(session):
                return run_method(*args, **SessionManager._build_media_kwargs(run_method, user_call))
        except Exception as e:
            abort_reply_turn()
            logger.error(f"[SessionManager] 同步会话执行失败: {e}, 堆栈={traceback.format_exc()}")
            return ""

    @staticmethod
    async def _invoke_async_session(session: AsyncSession, user_call: UserCall) -> Any:
        """
        异步执行会话

        参数:
        - session: 异步会话实例 (AsyncSession)
        - user_call: 用户调用对象 (UserCall)

        返回:
        - 会话执行结果
        """
        try:
            run_method = session.run
            args = SessionManager._build_run_args(run_method, user_call)
            with bind_call_origin(user_call.origin), bind_conversation_route(user_call.route), buffer_session_reply(session):
                return await run_method(*args, **SessionManager._build_media_kwargs(run_method, user_call))
        except Exception as e:
            abort_reply_turn()
            logger.error(f"[SessionManager] 异步会话执行失败: {e}, 堆栈={traceback.format_exc()}")
            return ""

    @staticmethod
    def _build_media_kwargs(run_method: Any, user_call: UserCall) -> dict[str, Any]:
        """
        按会话签名适配视频与本轮资料参数

        参数:
        - run_method: 当前会话的 run 方法
        - user_call: 平台用户调用

        返回:
        - 有视频时传递关键字参数, 不支持该输入的旧会话明确报错
        """
        if not user_call.video_urls and not user_call.request_blocks:
            return {}
        params = inspect.signature(run_method).parameters
        positional = [p for p in params.values() if p.kind in {
            inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }]
        if positional and positional[0].name.lower() in {"user_call", "call", "request"}:
            return {}
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        kwargs: dict[str, Any] = {}
        if user_call.video_urls:
            if "video_urls" not in params and not accepts_kwargs:
                raise ValueError("当前会话未提供视频输入接口")
            kwargs["video_urls"] = user_call.video_urls
        if user_call.request_blocks and ("request_blocks" in params or accepts_kwargs):
            kwargs["request_blocks"] = user_call.request_blocks
        return kwargs

    @staticmethod
    def _build_run_args(run_method: Any, user_call: UserCall) -> tuple[Any, ...]:
        """
        保持原有参数适配策略

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

        params = [param for param in params if param.kind in {
            inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }]
        param_size = len(params)
        if param_size == 0:
            return ()

        if params[0].name.lower() in {"user_call", "call", "request"}:
            return (user_call,)
        if param_size == 1 or params[1].name == "video_urls":
            return (user_call.message or "",)

        return (user_call.message or "", user_call.img_urls)

    @staticmethod
    def _session_message_count(session: Session | AsyncSession) -> int:
        """
        获取会话及其工作流已处理消息总数

        参数:
        - session: 会话实例 (Session 或 AsyncSession)

        返回:
        - 会话已处理消息数量
        """
        contexts: list[ContextManager | AsyncContextManager] = []
        session_ctx: ContextManager | AsyncContextManager | None = safe_getattr(session, "session_ctx")
        if session_ctx is not None:
            contexts.append(session_ctx)

        empty_contexts: dict[str, ContextManager | AsyncContextManager] = {}
        workflow_contexts = cast(
            dict[str, ContextManager | AsyncContextManager],
            safe_getattr(session, "_workflow_contexts", empty_contexts),
        )
        contexts.extend(workflow_contexts.values())

        total = 0
        counted_contexts: set[str] = set()
        for context in contexts:
            conversation_id = safe_getattr(context, "conversation_id")
            context_key = str(conversation_id) if conversation_id is not None else f"object:{id(context)}"
            if context_key in counted_contexts:
                continue
            counted_contexts.add(context_key)
            try:
                total += int(context.static_message())
            except Exception as e:
                logger.error(f"[SessionManager] 读取会话消息数失败：{e}")
        return total

    def _invoke_sync_entry(self, entry: SessionEntry, user_call: UserCall, session_id: str):
        """在线程内取得并释放同步锁, 运行完成后再保存运行时状态"""
        with entry.sync_operation_lock:
            response = self._invoke_sync_session(cast(Session, entry.session), user_call)
            self._sync_runtime_to_store(session_id, entry.session)
            return response

    def _prepare_session_sync(self, session: Session | AsyncSession, *, apply_snapshot: EdictumPluginApplySnapshot | None = None) -> None:
        """
        执行 Provider 的同步会话准备生命周期

        参数:
        - session: 已创建的会话实例
        - apply_snapshot: 可选候选应用来源, 不进入后续请求上下文
        """
        provider_name = str(safe_getattr(session, "_satrap_provider_name") or "")
        provider = self.provider_registry.get(provider_name) if provider_name else None
        prepare = safe_getattr_callable(provider, "prepare_session")
        if prepare is not None:
            prepare(session, **({"apply_snapshot": apply_snapshot} if apply_snapshot is not None else {}))

    async def _prepare_session_async(self, session: Session | AsyncSession, *, apply_snapshot: EdictumPluginApplySnapshot | None = None) -> None:
        """
        执行 Provider 的异步会话准备生命周期

        参数:
        - session: 已创建的会话实例
        - apply_snapshot: 可选候选应用来源, 不进入后续请求上下文
        """
        provider_name = str(safe_getattr(session, "_satrap_provider_name") or "")
        provider = self.provider_registry.get(provider_name) if provider_name else None
        prepare_async = safe_getattr_callable(provider, "prepare_session_async")
        if prepare_async is not None:
            result = prepare_async(session, **({"apply_snapshot": apply_snapshot} if apply_snapshot is not None else {}))
            if inspect.isawaitable(result):
                await result
            return
        await SESSION_WORKERS.run(partial(self._prepare_session_sync, session, apply_snapshot=apply_snapshot))

    def get_session_runtime_metadata(self, session_id: str) -> dict[str, Any]:
        """
        获取活跃会话的 Provider 运行时元数据

        参数:
        - session_id: 会话 ID

        返回:
        - dict[str, Any]: 非活跃或无元数据时返回空对象
        """
        entry = self.pool.list_entries().get(session_id)
        if entry is None:
            return {}
        provider_name = str(safe_getattr(entry.session, "_satrap_provider_name") or "")
        provider = self.provider_registry.get(provider_name) if provider_name else None
        get_metadata = safe_getattr_callable(provider, "get_runtime_metadata")
        if get_metadata is None:
            return {}
        metadata = get_metadata(entry.session)
        return cast(dict[str, Any], metadata).copy() if isinstance(metadata, dict) else {}

    def _release_session_memory(self, session: Session | AsyncSession):
        """
        释放会话内存

        参数:
        - session: 会话实例 (Session 或 AsyncSession)
        """
        provider_name = str(safe_getattr(session, "_satrap_provider_name") or "")
        provider = self.provider_registry.get(provider_name) if provider_name else None
        release = safe_getattr_callable(provider, "release_session")
        if release is not None:
            try:
                release(session)
            except Exception as e:
                logger.warning(f"[SessionManager] Provider 同步释放失败: {e}")

        session_context = safe_getattr(session, "session_ctx")
        close_method = safe_getattr_callable(session_context, "close")
        if close_method is not None:
            try:
                close_method()
            except Exception as e:
                logger.warning(f"[SessionManager] 关闭会话上下文连接失败: {e}")

    async def _release_session_memory_async(self, session: Session | AsyncSession):
        """
        释放会话内存 (异步)

        参数:
        - session: 会话实例 (Session 或 AsyncSession)
        """
        provider_name = str(safe_getattr(session, "_satrap_provider_name") or "")
        provider = self.provider_registry.get(provider_name) if provider_name else None
        release_async = safe_getattr_callable(provider, "release_session_async")
        release = safe_getattr_callable(provider, "release_session")
        try:
            if release_async is not None:
                result = release_async(session)
                if inspect.isawaitable(result):
                    await result
            elif release is not None:
                result = release(session)
                if inspect.isawaitable(result):
                    await result
        except Exception as e:
            logger.warning(f"[SessionManager] Provider 异步释放失败: {e}")

        session_context = safe_getattr(session, "session_ctx")
        close_method = safe_getattr_callable(session_context, "close")
        if close_method is not None:
            try:
                result = close_method()
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                logger.warning(f"[SessionManager] 关闭会话上下文连接失败: {e}")
