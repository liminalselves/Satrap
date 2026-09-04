"""
平台事件处理管线调度器

将平台消息转换为用户调用并交给目标会话执行,
统一应用限流, 超时, 错误反馈和执行前后处理钩子
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Awaitable, Callable, List, TypeVar, cast

from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.UserManager import UserManager
from satrap.core.log import logger
from satrap.core.pipeline.rate_limiter import RateLimiter
from satrap.core.platform.event import MessageChain, MessageEvent
from satrap.core.type import UserCall, safe_getattr, safe_getattr_str


_T = TypeVar("_T")


class PipelineScheduler:
    """
    消息管线调度器

    接收 MessageEvent, 依次执行:
    Stage 0: preprocessor 链
    Stage 1a: 限流
    Stage 1b: 唤醒词/@ 检查
    Stage 1c: 权限检查
    Stage 2: LLM 请求 (超时保护)
    后处理: 兜底发送回复
    """
    def __init__(
        self,
        session_manager: SessionManager,
        rate_limiter: RateLimiter | None = None,
        llm_timeout: float = 120.0,
        error_feedback: bool = True,
        user_manager: UserManager | None = None,
    ):
        """
        参数:
        - session_manager: 会话管理器实例
        - rate_limiter: 可选的限流器, 传 None 禁用限流
        - llm_timeout: LLM 调用超时(秒), 默认 120
        - error_feedback: 出错/限流时是否发送反馈消息给用户
        - user_manager: 用户管理器, 传 None 禁用上下文路由
        """
        self.session_manager = session_manager
        self.rate_limiter = rate_limiter
        self.llm_timeout = llm_timeout
        self.error_feedback = error_feedback
        self.user_manager = user_manager
        self.preprocessors: List[Callable[[MessageEvent], Awaitable[bool] | bool]] = []
        self.adapter_ids: set[str] = set()
        self.platform_runtimes: dict[str, tuple[SessionManager, UserManager]] = {}

    def add_preprocessor(self, fn: Callable[[MessageEvent], Awaitable[bool] | bool]):
        """
        添加预处理器, 在 stage 1a 前依次调用; 返回 False 则丢弃事件

        参数:
        - fn: 待调用函数, 支持同步或异步 (返回值经 _await_if_needed 统一处理)
        """
        self.preprocessors.append(fn)

    def set_adapter_ids(self, adapter_ids: set[str]):
        """
        设置当前后端已注册的平台适配器实例 ID

        参数:
        - adapter_ids: 适配器ids
        """
        self.adapter_ids = set(adapter_ids)

    def set_platform_runtimes(
        self,
        runtimes: dict[str, tuple[SessionManager, UserManager]],
    ) -> None:
        """
        设置按平台实例隔离的运行时管理器

        参数:
        - runtimes: 平台实例 ID 到会话和用户管理器的映射
        """
        self.platform_runtimes = dict(runtimes)

    async def execute(self, event: MessageEvent) -> None:
        """
        执行完整管线

        参数:
        - event: 消息事件
        """
        try:
            source_platform_id = event.get_platform_id()
            platform_runtime = self.platform_runtimes.get(source_platform_id)
            session_manager = platform_runtime[0] if platform_runtime else self.session_manager
            user_manager = platform_runtime[1] if platform_runtime else self.user_manager

            for processor in self.preprocessors:
                if not await self._await_if_needed(processor(event)):
                    logger.debug(f"[PipelineScheduler] preprocessor 丢弃事件: {event.session_id}")
                    return
            # ---------- Stage 0: preprocessor 链 ----------

            if self.rate_limiter:
                rate_key = f"{source_platform_id}:{event.session_id}"
                allowed, wait = await self.rate_limiter.check(rate_key)
                if not allowed:
                    logger.debug(
                        f"[PipelineScheduler] 限流丢弃事件: session={event.session_id}, "
                        f"需等待 {wait:.1f}s"
                    )
                    if self.error_feedback:
                        await self._send_feedback(event, "请求频率过高, 请稍后再试")
                    return
            # ---------- Stage 1a: 限流 ----------

            if not event.is_private_chat() and not event.is_wake_up() and not event.is_at_or_wake_command:
                return
            # ---------- Stage 1b: 唤醒词/ @检查 ----------

            if not await self._check_permission(event):
                return
            # ---------- Stage 1c: 权限检查 ----------

            message = event.get_message_str()
            if not message:
                return

            session_id = event.session_id
            # ---------- Stage 1d: 通过 UserManager 解析 session_id ----------
            if user_manager and event.session_type:
                platform_id, extra_params = self._resolve_route_adapter(event)
                resolved = user_manager.resolve_session(
                    user_id=event.get_sender_id(),
                    platform=platform_id,
                    session_provider=event.session_provider,
                    session_type=event.session_type,
                    class_cfg_mgr=safe_getattr(session_manager, 'class_cfg_mgr'),
                    extra_params=extra_params,
                )
                if resolved:
                    session_id = resolved

            user_call = UserCall(
                session_id=session_id,
                session_provider=event.session_provider,
                session_type=event.session_type,
                message=message,
                img_urls=self._extract_img_urls(event),
            )
            # ---------- Stage 2: LLM 请求 via Session (带超时保护) ----------
            try:
                response = await asyncio.wait_for(
                    session_manager.handle_call_async(user_call),
                    timeout=self.llm_timeout,
                )
            except asyncio.TimeoutError:
                logger.error(f"[PipelineScheduler] LLM 调用超时: {event.session_id}")
                if self.error_feedback:
                    await self._send_feedback(event, "请求超时, 请稍后重试")
                return

            if response and not event.has_send_operation():
                await event.send(MessageChain.from_text(response))
            # ---------- 后处理: 兜底发送回复 ----------
            # 如果 Session 内部已通过 content_callback 发送过消息
            # event.has_send_operation() 返回 True, 避免重复发送

        except Exception as e:
            logger.error(f"[PipelineScheduler] 管线执行错误: {e}")
            if self.error_feedback:
                await self._send_feedback(event, "处理失败, 请稍后重试")
        finally:
            event.cleanup_temporary_local_files()

    # ---------- 可覆写钩子 ----------

    async def _check_permission(self, event: MessageEvent) -> bool:
        """
        权限检查, 默认通过; 子类可覆写

        参数:
        - event: 事件

        返回:
        - bool: 权限检查, 默认通过; 子类可覆写
        """
        return True

    async def _send_feedback(self, event: MessageEvent, text: str):
        """
        发送反馈消息给用户

        参数:
        - event: 事件
        - text: 待处理文本
        """
        try:
            await event.send(MessageChain.from_text(text))
        except Exception as e:
            logger.warning(f"[PipelineScheduler] 发送反馈消息失败: {e}")

    def _resolve_route_adapter(self, event: MessageEvent) -> tuple[str, dict[str, str] | None]:
        """
        解析事件应绑定到哪个适配器实例

        参数:
        - event: 事件

        返回:
        - tuple[str, dict[str, str] | None]: 解析事件应绑定到哪个适配器实例
        """
        source_adapter_id = event.get_platform_id()
        if not source_adapter_id:
            return "", None
        return source_adapter_id, {"adapter_id": source_adapter_id}

    @staticmethod
    def _extract_img_urls(event: MessageEvent) -> list[str]:
        """
        从 event 中提取图片 URL 列表

        参数:
        - event: 事件

        返回:
        - list[str]: 从 event 中提取图片 URL 列表
        """
        urls: list[str] = []
        try:
            for comp in event.get_messages():
                ctype = safe_getattr(comp, 'type')
                if ctype is not None:
                    ctype_str = ctype.value if hasattr(ctype, 'value') else str(ctype)
                    if ctype_str.lower() == 'image':
                        url = safe_getattr_str(comp, 'url') or safe_getattr_str(comp, 'file')
                        if url:
                            urls.append(str(url))
        except Exception:
            pass
        return urls

    @staticmethod
    async def _await_if_needed(value: Awaitable[_T] | _T) -> _T:
        if inspect.isawaitable(value):
            return await cast(Awaitable[_T], value)
        return value
