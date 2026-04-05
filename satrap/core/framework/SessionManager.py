from __future__ import annotations

import asyncio
import inspect
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Type

from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap.core.framework import AsyncSession, Session
from satrap.core.log import logger
from satrap.core.type import UserCall


@dataclass
class SessionEntry:
    """会话池中的运行时条目"""
    session: Session | AsyncSession
    session_type: str
    created_at: float
    last_used: float


@dataclass
class SessionMetadata:
    """会话状态元数据, 用于查询和展示"""
    session_id: str
    session_type: str
    created_at: float
    last_used_at: float
    message_count: int


class SessionRegistry:
    """会话类型注册表: 维护 session_type 到会话类的映射"""
    def __init__(self):
        self._mapping: Dict[str, Type[Session] | Type[AsyncSession]] = {}
        self._lock = threading.RLock()

    def register(self, session_type: str, session_class: Type[Session] | Type[AsyncSession]):
        """注册会话类型; 校验失败仅记录日志, 不抛异常"""
        if not session_type:
            logger.error("[SessionRegistry] 注册失败：session_type 不能为空")
            return
        if not inspect.isclass(session_class):
            logger.error("[SessionRegistry] 注册失败：session_class 必须是类对象")
            return
        if not issubclass(session_class, (Session, AsyncSession)):
            logger.error("[SessionRegistry] 注册失败：session_class 必须继承 Session 或 AsyncSession")
            return

        with self._lock:
            self._mapping[session_type] = session_class

    def get_class(self, session_type: str) -> Optional[Type[Session] | Type[AsyncSession]]:
        """根据会话类型获取会话类, 不存在时返回 None"""
        with self._lock:
            return self._mapping.get(session_type)

    def list_types(self) -> List[str]:
        """列出所有已注册会话类型"""
        with self._lock:
            return list(self._mapping.keys())


class SessionPool:
    """活跃会话池: 支持 LRU 淘汰和闲置清理"""
    def __init__(self, max_size: int = 1000, idle_timeout: int = 3600):
        """初始化会话池
        
        - max_size: 会话池最大容量, 默认 1000
        - idle_timeout: 会话闲置超时时间, 默认 3600 秒
        """
        self._sessions: Dict[str, SessionEntry] = {}
        self.max_size = max_size
        self.idle_timeout = idle_timeout
        self._lock = threading.RLock()

    def get(self, session_id: str) -> Optional[SessionEntry]:
        """获取会话并刷新最近使用时间"""
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
        """放入会话; 若超出容量时按 LRU 淘汰一个会话"""
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
        """显式移除会话"""
        with self._lock:
            return self._sessions.pop(session_id, None)

    def list_entries(self) -> Dict[str, SessionEntry]:
        """返回会话池快照"""
        with self._lock:
            return dict(self._sessions)

    def collect_idle(self, max_idle_seconds: Optional[int] = None) -> List[tuple[str, SessionEntry]]:
        """收集并移除闲置超时会话"""
        with self._lock:
            now = time.time()
            timeout = self.idle_timeout if max_idle_seconds is None else max_idle_seconds
            idle_ids = [
                sid for sid, entry in self._sessions.items()
                if (now - entry.last_used) > timeout
            ]

            removed: List[tuple[str, SessionEntry]] = []
            for sid in idle_ids:
                entry = self._sessions.pop(sid, None)
                if entry:
                    removed.append((sid, entry))
            return removed

    def _evict_one_locked(self) -> tuple[str, SessionEntry]:
        """在已加锁条件下按 LRU 淘汰一个会话"""
        oldest_id = min(self._sessions.keys(), key=lambda sid: self._sessions[sid].last_used)
        return oldest_id, self._sessions.pop(oldest_id)


class SessionManager:
    """多会话管理器; 支持同步/异步会话管理"""

    def __init__(
        self,
        default_session_type: str = "default",
        max_size: int = 1000,
        idle_timeout: int = 3600,
    ):
        """初始化会话管理器
        
        - default_session_type: 默认会话类型, 默认 "default"
        - max_size: 会话池最大容量, 默认 1000
        - idle_timeout: 会话闲置超时时间, 默认 3600 秒
        """
        self.registry = SessionRegistry()
        self.pool = SessionPool(max_size=max_size, idle_timeout=idle_timeout)
        self.default_session_type = default_session_type
        self._async_lock = asyncio.Lock()

        # 注册默认会话类型, 若异常仅记录日志
        try:
            self.registry.register(default_session_type, Session)
        except Exception as e:
            logger.error(f"[SessionManager] 注册默认会话类型失败：{e}")

    def register_session_type(self, session_type: str, session_class: Type[Session] | Type[AsyncSession]):
        """注册自定义会话类型"""
        try:
            self.registry.register(session_type, session_class)
        except Exception as e:
            logger.error(f"[SessionManager] 注册会话类型失败：session_type={session_type}, 错误={e}")

    def handle_call(self, user_call: UserCall) -> str:
        """同步处理用户调用, 失败时返回空字符串"""
        try:
            if not isinstance(user_call, UserCall):
                logger.error("[SessionManager] handle_call 失败：user_call 必须是 UserCall 实例")
                return ""

            session_id = user_call.session_id or uuid.uuid4().hex
            user_call.session_id = session_id

            session_type = user_call.session_type or self.default_session_type
            entry = self.pool.get(session_id)
            if entry is None:
                entry = self._create_entry(session_id, session_type)
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
            self.cleanup_idle_sessions()
            return "" if response is None else str(response)
        except Exception as e:
            logger.error(f"[SessionManager] handle_call 发生异常：{e}")
            return ""

    async def handle_call_async(self, user_call: UserCall) -> str:
        """异步处理用户调用, 失败时返回空字符串"""
        try:
            if not isinstance(user_call, UserCall):
                logger.error("[SessionManager] handle_call_async 失败：user_call 必须是 UserCall 实例")
                return ""

            session_id = user_call.session_id or uuid.uuid4().hex
            user_call.session_id = session_id
            requested_type = user_call.session_type or self.default_session_type

            async with self._async_lock:
                entry = self.pool.get(session_id)
                if entry is None:
                    entry = self._create_entry(session_id, requested_type)

            if entry is None:
                logger.error(f"[SessionManager] handle_call_async 失败：会话创建失败，session_id={session_id}")
                return ""

            if isinstance(entry.session, AsyncSession):
                response = await self._invoke_async_session(entry.session, user_call)
            else:
                response = self._invoke_sync_session(entry.session, user_call)

            await self.cleanup_idle_sessions_async()
            return "" if response is None else str(response)
        except Exception as e:
            logger.error(f"[SessionManager] handle_call_async 发生异常：{e}")
            return ""

    def list_sessions(self) -> List[SessionMetadata]:
        """列出所有活跃会话元数据"""
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
        """同步清理闲置会话"""
        try:
            removed = self.pool.collect_idle(max_idle_seconds=max_idle_seconds)
            for session_id, entry in removed:
                self._release_session_memory(entry.session)
                logger.info(f"[SessionManager] 已清理闲置会话：{session_id}")
        except Exception as e:
            logger.error(f"[SessionManager] cleanup_idle_sessions 失败：{e}")

    async def cleanup_idle_sessions_async(self, max_idle_seconds: int = 3600):
        """异步清理闲置会话"""
        try:
            removed = self.pool.collect_idle(max_idle_seconds=max_idle_seconds)
            for session_id, entry in removed:
                await self._release_session_memory_async(entry.session)
                logger.info(f"[SessionManager] 已清理闲置会话：{session_id}")
        except Exception as e:
            logger.error(f"[SessionManager] cleanup_idle_sessions_async 失败：{e}")

    def remove_session(self, session_id: str):
        """同步移除指定会话"""
        try:
            entry = self.pool.remove(session_id)
            if entry:
                self._release_session_memory(entry.session)
        except Exception as e:
            logger.error(f"[SessionManager] remove_session 失败：session_id={session_id}, 错误={e}")

    async def remove_session_async(self, session_id: str):
        """异步移除指定会话"""
        try:
            entry = self.pool.remove(session_id)
            if entry:
                await self._release_session_memory_async(entry.session)
        except Exception as e:
            logger.error(f"[SessionManager] remove_session_async 失败：session_id={session_id}, 错误={e}")

    def _create_entry(self, session_id: str, session_type: str) -> Optional[SessionEntry]:
        """创建会话实例并放入会话池"""
        session_class = self.registry.get_class(session_type)
        if session_class is None:
            logger.error(f"[SessionManager] 创建会话失败：未知 session_type={session_type}")
            return None

        try:
            session = session_class(session_id)  # type: ignore[misc]
        except Exception as e:
            logger.error(
                f"[SessionManager] 创建会话实例失败：session_type={session_type}, "
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
            logger.info(f"[SessionManager] 已按 LRU 淘汰会话：{evicted_id}")

        entry = self.pool.get(session_id)
        if entry is None:
            logger.error(f"[SessionManager] 创建会话条目失败：session_id={session_id}")
            return None
        return entry

    @staticmethod
    def _invoke_sync_session(session: Session, user_call: UserCall) -> Any:
        """统一同步 run 调用入口"""
        try:
            run_method = session.run
            args = SessionManager._build_run_args(run_method, user_call)
            return run_method(*args)
        except Exception as e:
            logger.error(f"[SessionManager] 同步会话执行失败：{e}")
            return ""

    @staticmethod
    async def _invoke_async_session(session: AsyncSession, user_call: UserCall) -> Any:
        """统一异步 run 调用入口"""
        try:
            run_method = session.run
            args = SessionManager._build_run_args(run_method, user_call)
            return await run_method(*args)
        except Exception as e:
            logger.error(f"[SessionManager] 异步会话执行失败：{e}")
            return ""

    @staticmethod
    def _build_run_args(run_method: Any, user_call: UserCall) -> tuple[Any, ...]:
        """根据 run 签名自动适配参数"""
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
        """读取会话上下文消息数, 异常时返回 0"""
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
        """同步释放会话内存"""
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
        """异步释放会话内存"""
        clear_method = getattr(session, "clear_memory", None)
        if not callable(clear_method):
            return

        try:
            result = clear_method()
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.warning(f"[SessionManager] clear_memory 执行失败：{e}")
