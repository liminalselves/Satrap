"""
平台事件处理管线调度器

将平台消息转换为用户调用并交给目标会话执行,
统一应用限流, 超时, 错误反馈和执行前后处理钩子
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import asyncio
import inspect
from typing import (
    Awaitable,
    Callable,
    List,
    TypeVar,
    cast,
)

from satrap.core.framework.SessionManager import SessionManager
from satrap.core.config.platform_policy import normalize_group_whitelist
from satrap.core.config.wake_overrides import resolve_wake_settings
from satrap.core.framework.UserManager import UserManager
from satrap.core.pipeline.rate_limiter import RateLimiter
from satrap.core.platform.event import MessageChain, MessageEvent
from satrap.core.components import PlatformComponentType
from satrap.core.conversation import ConversationRoute
from satrap.core.pipeline.wake_policy import WakeDecision, evaluate_wake
from satrap.core.pipeline.wake_window import WakeWindow
from satrap.core.pipeline.wake_timers import WakeTimers
from satrap.core.pipeline.input_projection import project_input, resolve_forwards, resolve_quotes
from satrap.core.pipeline.manual_wake import ManualWakeRequests
from satrap.core.platform import PlatformAdapter
from satrap.core.type import UserCall, safe_getattr, safe_getattr_str

from satrap.core.log import logger


_T = TypeVar("_T")


class PipelineScheduler:
    """
    消息管线调度器

    接收 MessageEvent, 依次执行:
    来源范围与 preprocessor 链
    停止状态与权限检查
    唤醒词/@ 检查与模型调用限流
    会话路由与 LLM 请求 (超时保护)
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
        self._session_turns: dict[tuple[int, str], tuple[asyncio.Lock, int]] = {}
        self.platform_runtimes: dict[str, tuple[SessionManager, UserManager]] = {}
        self.wake_window = WakeWindow()
        self.wake_timers = WakeTimers(self.wake_window)
        self.manual_wakes = ManualWakeRequests()

    def add_preprocessor(self, fn: Callable[[MessageEvent], Awaitable[bool] | bool]):
        """
        添加预处理器, 在权限与唤醒检查前依次调用; 返回 False 则丢弃事件

        参数:
        - fn: 待调用函数, 支持同步或异步 (返回值经 _await_if_needed 统一处理)
        """
        self.preprocessors.append(fn)

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
            manual_ticket = self.manual_wakes.tickets.get(event)
            if manual_ticket is not None and manual_ticket.cancelled:
                return
            deadline_ticket = self.wake_timers.tickets.get(event)
            if deadline_ticket is not None and deadline_ticket.cancelled:
                return
            source_platform_id = event.get_platform_id()
            platform_runtime = self.platform_runtimes.get(source_platform_id)
            session_manager = platform_runtime[0] if platform_runtime else self.session_manager
            user_manager = platform_runtime[1] if platform_runtime else self.user_manager

            if event.is_stopped() or not event.call_llm or not self._allows_source(event):
                return
            for processor in self.preprocessors:
                if event.is_stopped() or not event.call_llm:
                    return
                if not await self._await_if_needed(processor(event)):
                    logger.debug(f"[PipelineScheduler] preprocessor 丢弃事件: {event.session_id}")
                    return

            # Step.1 停止状态和权限先于唤醒及模型额度检查
            if event.is_stopped() or not event.call_llm:
                return
            if not self._allows_source(event) or not await self._check_permission(event):
                return

            # Step.2 评估独立唤醒规则并记录命中原因
            self._apply_wake_policy(event)
            if not event.is_private_chat() and not event.is_wake_up() and event.policy_settings.get("wake_on_quote_self") is True:
                await self._apply_quote_wake(event)
            pending = manual_ticket.snapshot if manual_ticket is not None else ()
            automatic = False
            if manual_ticket is not None:
                event.is_wake = True
            elif not event.is_private_chat() and self._automatic_policy_current(event) and event.policy_settings.get("wake_mode", "explicit") in {"frequency", "necessity"}:
                pending = deadline_ticket.snapshot if deadline_ticket is not None else self.wake_window.observe(event)
                if not event.is_wake_up() and not event.is_at_or_wake_command:
                    decision = self.wake_window.decide(event, pending, deadline=deadline_ticket is not None)
                    event.set_extra("wake_decision", decision)
                    if decision.triggered:
                        automatic = True
                        event.is_wake = True
            if not event.is_private_chat() and not event.is_wake_up() and not event.is_at_or_wake_command:
                if deadline_ticket is None:
                    self.wake_timers.schedule(event)
                return

            message = event.get_message_str()
            images = self._extract_img_urls(event)
            videos = self._extract_img_urls(event, "video")
            has_quote = any(c.type == PlatformComponentType.Reply for c in event.get_messages())
            if not message and not images and not videos and not has_quote:
                return

            # Step.3 只有已唤醒且允许处理的请求消耗模型额度
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

            # Step.4 限流通过后按预算补全引用与转发原文并投影模型输入
            quote_status = await resolve_quotes(event)
            forward_status = await resolve_forwards(event)
            projected = project_input(event, quote_status, forward_status)
            event.set_extra("input_projection", projected)
            message, images, videos = projected.message, list(projected.images), list(projected.videos)
            if not message and not images and not videos:
                return

            # Step.5 通过 UserManager 解析目标会话
            session_id = event.session_id
            route: ConversationRoute | None = None
            settings = event.policy_settings
            scope = str(settings.get("context_scope", "legacy_user")) if not event.is_private_chat() else "legacy_user"
            if scope != "legacy_user" and (not user_manager or not event.session_type):
                raise ValueError("隔离上下文需要 UserManager 和命名会话配置")
            if user_manager and event.session_type:
                platform_id, extra_params = self._resolve_route_adapter(event)
                route = ConversationRoute(
                    user_id=event.get_sender_id(), platform=platform_id,
                    session_type=event.session_type, provider=event.session_provider,
                    scope=scope, self_id=event.get_self_id(), group_id=event.get_group_id(),
                )
                route_args = {"route": route} if scope != "legacy_user" else {}
                resolved = user_manager.resolve_session(
                    user_id=event.get_sender_id(),
                    platform=platform_id,
                    session_provider=event.session_provider,
                    session_type=event.session_type,
                    class_cfg_mgr=safe_getattr(session_manager, 'class_cfg_mgr'),
                    extra_params=extra_params,
                    **route_args,
                )
                if not resolved:
                    return
                session_id = resolved

            user_call = UserCall(
                session_id=session_id,
                session_provider=event.session_provider,
                session_type=event.session_type,
                message=message,
                img_urls=images,
                video_urls=videos,
                route=route,
                origin=event.call_origin,
            )
            async with self._session_turn(session_manager, session_id):
                if event.is_stopped() or not event.call_llm or not self._allows_source(event) or not await self._check_permission(event):
                    return
                if automatic and not self._automatic_policy_current(event):
                    return
                if deadline_ticket is not None and deadline_ticket.cancelled:
                    return
                if manual_ticket is not None and manual_ticket.cancelled:
                    return
                if pending:
                    batch = self.wake_window.claim(event, pending, automatic, deadline=deadline_ticket is not None)
                    if automatic and not batch:
                        return
                    if manual_ticket is not None and manual_ticket.snapshot and not batch:
                        return
                    if batch:
                        self.wake_timers.cancel_route(event)
                        user_call.message = "\n".join(f"[用户 {item.actor_id}, 消息 {item.message_id}] {item.text}" for item in batch)
                # Step.6 执行会话并限制等待时间
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
            ticket = self.manual_wakes.tickets.get(event)
            if ticket is not None and ticket.status == "pending":
                ticket.status = "processed"
            event.cleanup_temporary_local_files()

    # ---------- 可覆写钩子 ----------

    @asynccontextmanager
    async def _session_turn(self, manager: SessionManager, session_id: str) -> AsyncIterator[None]:
        """
        按最终会话串行执行模型和回复, 不保留空闲锁

        参数:
        - manager: 隔离的会话管理器
        - session_id: 路由解析后的最终会话 ID

        返回:
        - 持有本轮执行权的异步上下文
        """
        key = (id(manager), session_id)
        lock, users = self._session_turns.get(key, (asyncio.Lock(), 0))
        self._session_turns[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self._session_turns[key]
            if users == 1:
                del self._session_turns[key]
            else:
                self._session_turns[key] = (lock, users - 1)

    @staticmethod
    def _allows_source(event: MessageEvent) -> bool:
        """
        检查平台启停与群范围, 包括绕过适配器入口的事件

        参数:
        - event: 待处理事件

        返回:
        - 是否允许进入模型管线
        """
        adapter = event.adapter
        if not isinstance(adapter, PlatformAdapter):
            return True
        if not adapter.config.enable:
            return False
        settings = adapter.config.settings
        if adapter.config.type not in {"onebot", "aiocqhttp"}:
            return True
        if event.is_private_chat():
            return bool(settings.get("enable_private", True))
        groups = normalize_group_whitelist(settings.get("group_whitelist", []))
        return bool(settings.get("enable_group", True)) and (not groups or event.get_group_id() in groups)

    @staticmethod
    def _automatic_policy_current(event: MessageEvent) -> bool:
        """
        防止已失效的自动规则向新窗口提交内容

        参数:
        - event: 携带接收时策略快照的事件

        返回:
        - bool: 自动参与相关配置是否仍与当前实例一致
        """
        if not isinstance(event.adapter, PlatformAdapter):
            return False
        current = resolve_wake_settings(event.adapter.config.settings, event.call_origin.chat_id if not event.is_private_chat() else "")
        keys = {key for key in set(current) | set(event.policy_settings) if key.startswith("wake_") or key == "context_scope"}
        return all(current.get(key) == event.policy_settings.get(key) for key in keys)

    @staticmethod
    async def _apply_quote_wake(event: MessageEvent) -> None:
        """
        用引用回源预算确认被引用者是否为机器人, 是则视为明确唤醒

        参数:
        - event: 未被其他规则唤醒且开启 wake_on_quote_self 的群消息
        """
        if not any(c.type == PlatformComponentType.Reply for c in event.get_messages()):
            return
        status = await resolve_quotes(event)
        reply = next(c for c in event.get_messages() if c.type == PlatformComponentType.Reply)
        if status == "resolved" and safe_getattr_str(reply, "sender_id") == event.call_origin.self_id:
            event.is_wake = True
            event.is_at_or_wake_command = True
            event.set_extra("wake_decision", WakeDecision(True, "quote_self", "引用了机器人的消息", event.call_origin.self_id))
        # 回源结果保留在 Reply 字段上, 后续投影不重复请求; 失败或引用他人不改变未唤醒状态

    @staticmethod
    def _apply_wake_policy(event: MessageEvent) -> None:
        """
        根据可信组件设置唤醒标志并保留上游明确标志

        参数:
        - event: 待评估事件, 仅扫描顶层正文与提及
        """
        decision = evaluate_wake(event)
        event.set_extra("wake_decision", decision)
        if decision.triggered and not event.is_private_chat():
            event.is_wake = True
            event.is_at_or_wake_command = True

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
    def _extract_img_urls(event: MessageEvent, media_type: str = "image") -> list[str]:
        """
        从 event 中提取指定类型的媒体来源

        参数:
        - event: 事件
        - media_type: image 或 video, 默认 image 保持既有调用含义

        返回:
        - list[str]: 对应媒体的 URL 或文件路径列表
        """
        urls: list[str] = []
        try:
            for comp in event.get_messages():
                ctype = safe_getattr(comp, 'type')
                if ctype is not None:
                    ctype_str = ctype.value if hasattr(ctype, 'value') else str(ctype)
                    if ctype_str.lower() == media_type:
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
