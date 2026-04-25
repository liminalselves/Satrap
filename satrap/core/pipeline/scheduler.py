from __future__ import annotations

from typing import TYPE_CHECKING

from satrap.core.framework.SessionManager import SessionManager
from satrap.core.log import logger
from satrap.core.type import UserCall

if TYPE_CHECKING:
    from satrap.core.platform.event import MessageChain, MessageEvent

    from satrap.core.pipeline.rate_limiter import RateLimiter


class PipelineScheduler:
    """消息管线调度器

    接收 MessageEvent, 执行 Stage 1 预处理 (限流/权限/唤醒词检查)
    委托 SessionManager 做 LLM 请求, 兜底发送回复
    """
    def __init__(
        self,
        session_manager: SessionManager,
        rate_limiter: RateLimiter | None = None,
    ):
        """
        参数:
        - session_manager: 会话管理器实例
        - rate_limiter: 可选的限流器, 传 None 禁用限流
        """
        self.session_manager = session_manager
        self.rate_limiter = rate_limiter

    async def execute(self, event: MessageEvent) -> None:
        """执行完整管线

        参数:
        - event: 消息事件
        """
        try:
            # ── Stage 1a: 限流 ──
            if self.rate_limiter:
                allowed, wait = await self.rate_limiter.check(event.session_id)
                if not allowed:
                    logger.debug(
                        f"[PipelineScheduler] 限流丢弃事件: session={event.session_id}, "
                        f"需等待 {wait:.1f}s"
                    )
                    return

            # ── Stage 1b: 唤醒词/ @检查 ──
            # 群聊且无艾特/唤醒词时跳过
            if not event.is_private_chat() and not event.is_wake_up() and not event.is_at_or_wake_command:
                return

            # ── Stage 1c: 权限检查 (可扩展点) ──
            # 子类可覆写 _check_permission(event) 实现
            message = event.get_message_str()
            if not message:
                return

            # ── Stage 2: LLM 请求 via Session ──
            user_call = UserCall(
                session_id=event.session_id,
                message=message,
            )
            response = await self.session_manager.handle_call_async(user_call)

            # ── 后处理: 兜底发送回复 ──
            # 如果 Session 内部已通过 content_callback 发送过消息
            # event.has_send_operation() 返回 True, 避免重复发送
            if response and not event.has_send_operation():
                from satrap.core.platform.event import MessageChain
                await event.send(MessageChain.from_text(response))

        except Exception as e:
            logger.error(f"[PipelineScheduler] 管线执行错误: {e}")
        finally:
            event.cleanup_temporary_local_files()
