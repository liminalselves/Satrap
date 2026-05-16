from __future__ import annotations

import asyncio
from typing import Callable, List

from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.UserManager import UserManager
from satrap.core.log import logger
from satrap.core.pipeline.rate_limiter import RateLimiter
from satrap.core.platform.event import MessageChain, MessageEvent
from satrap.core.type import UserCall


class PipelineScheduler:
    """消息管线调度器

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
        self.preprocessors: List[Callable[[MessageEvent], bool]] = []
        self.adapter_ids: set[str] = set()

    def add_preprocessor(self, fn: Callable[[MessageEvent], bool]):
        """添加预处理器, 在 stage 1a 前依次调用; 返回 False 则丢弃事件"""
        self.preprocessors.append(fn)

    def set_adapter_ids(self, adapter_ids: set[str]):
        """设置当前后端已注册的平台适配器实例 ID"""
        self.adapter_ids = set(adapter_ids)

    async def execute(self, event: MessageEvent) -> None:
        """执行完整管线

        参数:
        - event: 消息事件
        """
        try:
            # ── Stage 0: preprocessor 链 ──
            for processor in self.preprocessors:
                if not await self._await_if_needed(processor(event)):
                    logger.debug(f"[PipelineScheduler] preprocessor 丢弃事件: {event.session_id}")
                    return

            # ── Stage 1a: 限流 ──
            if self.rate_limiter:
                allowed, wait = await self.rate_limiter.check(event.session_id)
                if not allowed:
                    logger.debug(
                        f"[PipelineScheduler] 限流丢弃事件: session={event.session_id}, "
                        f"需等待 {wait:.1f}s"
                    )
                    if self.error_feedback:
                        await self._send_feedback(event, "请求频率过高, 请稍后再试")
                    return

            # ── Stage 1b: 唤醒词/ @检查 ──
            if not event.is_private_chat() and not event.is_wake_up() and not event.is_at_or_wake_command:
                return

            # ── Stage 1c: 权限检查 ──
            if not await self._check_permission(event):
                return

            message = event.get_message_str()
            if not message:
                return

            # ── Stage 1d: 通过 UserManager 解析 session_id ──
            session_id = event.session_id
            if self.user_manager and event.session_type:
                platform_id, extra_params = self._resolve_route_adapter(event)
                resolved = self.user_manager.resolve_session(
                    user_id=event.get_sender_id(),
                    platform=platform_id,
                    session_type=event.session_type,
                    class_cfg_mgr=getattr(self.session_manager, 'class_cfg_mgr', None),
                    extra_params=extra_params,
                )
                if resolved:
                    session_id = resolved

            # ── Stage 2: LLM 请求 via Session (带超时保护) ──
            user_call = UserCall(
                session_id=session_id,
                session_type=event.session_type,
                message=message,
                img_urls=self._extract_img_urls(event),
            )
            try:
                response = await asyncio.wait_for(
                    self.session_manager.handle_call_async(user_call),
                    timeout=self.llm_timeout,
                )
            except asyncio.TimeoutError:
                logger.error(f"[PipelineScheduler] LLM 调用超时: {event.session_id}")
                if self.error_feedback:
                    await self._send_feedback(event, "请求超时, 请稍后重试")
                return

            # ── 后处理: 兜底发送回复 ──
            # 如果 Session 内部已通过 content_callback 发送过消息
            # event.has_send_operation() 返回 True, 避免重复发送
            if response and not event.has_send_operation():
                await event.send(MessageChain.from_text(response))

        except Exception as e:
            logger.error(f"[PipelineScheduler] 管线执行错误: {e}")
            if self.error_feedback:
                await self._send_feedback(event, "处理失败, 请稍后重试")
        finally:
            event.cleanup_temporary_local_files()

    # ── 可覆写钩子 ──

    async def _check_permission(self, event: MessageEvent) -> bool:
        """权限检查, 默认通过; 子类可覆写"""
        return True

    async def _send_feedback(self, event: MessageEvent, text: str):
        """发送反馈消息给用户"""
        try:
            await event.send(MessageChain.from_text(text))
        except Exception as e:
            logger.warning(f"[PipelineScheduler] 发送反馈消息失败: {e}")

    def _resolve_route_adapter(self, event: MessageEvent) -> tuple[str, dict[str, str] | None]:
        """解析事件应绑定到哪个适配器实例"""
        source_adapter_id = event.get_platform_id()
        class_cfg_mgr = getattr(self.session_manager, 'class_cfg_mgr', None)
        requested = ""
        if class_cfg_mgr is not None and event.session_type:
            try:
                params = class_cfg_mgr.get_params(event.session_type)
                requested = str(params.get("adapter_id", "") or "").strip()
            except Exception:
                requested = ""

        if not requested:
            return source_adapter_id, None

        if self.adapter_ids and requested not in self.adapter_ids:
            logger.warning(
                f"[PipelineScheduler] 适配器实例不存在: {requested}, "
                f"回退到事件来源: {source_adapter_id}"
            )
            return source_adapter_id, None

        return requested, {"adapter_id": requested}

    @staticmethod
    def _extract_img_urls(event: MessageEvent) -> list[str]:
        """从 event 中提取图片 URL 列表"""
        urls: list[str] = []
        try:
            for comp in event.get_messages():
                ctype = getattr(comp, 'type', None)
                if ctype is not None:
                    ctype_str = ctype.value if hasattr(ctype, 'value') else str(ctype)
                    if ctype_str.lower() == 'image':
                        url = getattr(comp, 'url', None) or getattr(comp, 'file', None) or ''
                        if url:
                            urls.append(str(url))
        except Exception:
            pass
        return urls

    @staticmethod
    async def _await_if_needed(value):
        if asyncio.iscoroutine(value):
            return await value
        return value
