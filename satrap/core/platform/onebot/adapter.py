from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator
from typing import Any

from satrap.core.log import logger
from satrap.core.platform import PlatformAdapter, PlatformConfig, register_platform_adapter
from satrap.core.platform.event import MessageChain, MessageEvent, PlatformMetadata
from satrap.core.platform.onebot.onebot_utils import (
    create_platform_message,
    extract_group_id,
    extract_private_user_id,
    is_group_session,
    is_private_session,
    message_chain_to_onebot_segments,
)
from satrap.core.type import PlatformMessage


class _MissingCQHttp:
    """缺少 aiocqhttp 时的占位类型, 用于给出清晰错误"""


try:
    from aiocqhttp import CQHttp
except ImportError:  # pragma: no cover - 在安装依赖后走真实分支
    CQHttp = _MissingCQHttp  # type: ignore


@register_platform_adapter("aiocqhttp")
@register_platform_adapter("onebot")
class OneBotAdapter(PlatformAdapter):
    """OneBot v11 平台适配器, 使用 aiocqhttp 反向 WebSocket"""

    adapter_type = "onebot"

    def __init__(
        self,
        config: PlatformConfig,
        event_handler=None,
        event_queue=None,
    ) -> None:
        """初始化 OneBotAdapter 实例"""
        super().__init__(config, event_handler, event_queue)
        settings = config.settings or {}
        self.host = str(settings.get("host") or settings.get("listen_host") or "127.0.0.1")
        self.port = int(settings.get("port") or settings.get("listen_port") or 8080)
        self.access_token = str(settings.get("access_token") or "")
        self.secret = str(settings.get("secret") or "")
        self.enable_private = bool(settings.get("enable_private", True))
        self.enable_group = bool(settings.get("enable_group", True))
        self.bot_self_id = str(settings.get("self_id") or "")
        self._bot: Any = None
        self._running = False

    def meta(self) -> PlatformMetadata:
        """返回平台元信息"""
        return PlatformMetadata(
            name=f"OneBot({self.host}:{self.port})",
            id=self.config.id,
            adapter_display_name="OneBot",
            description="OneBot v11 反向 WebSocket 平台适配器",
            support_streaming_message=False,
            support_proactive_message=True,
        )

    async def run(self) -> None:
        """启动 aiocqhttp 反向 WebSocket 服务"""
        if CQHttp is _MissingCQHttp:
            self.record_error("[OneBotAdapter] 未安装 aiocqhttp, 无法启动 OneBot 适配器")
            return

        kwargs: dict[str, Any] = {"use_ws_reverse": True}
        if self.access_token:
            kwargs["access_token"] = self.access_token
        if self.secret:
            kwargs["secret"] = self.secret
        self._bot = CQHttp(**kwargs)
        self._register_handlers()
        self._running = True

        try:
            run_task = getattr(self._bot, "run_task", None)
            if callable(run_task):
                result = run_task(host=self.host, port=self.port)
            else:
                result = self._bot.run(host=self.host, port=self.port)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._running = False
            self.record_error(f"[OneBotAdapter] 服务运行失败: {e}")

    def _register_handlers(self) -> None:
        """注册 OneBot 事件处理器"""
        if not self._bot:
            return

        self._bot.on_message("private")(self._handle_private_message)
        self._bot.on_message("group")(self._handle_group_message)
        self._register_optional_handler("on_notice", self._handle_notice)
        self._register_optional_handler("on_request", self._handle_request)

    def _register_optional_handler(self, method_name: str, handler: Any) -> None:
        """兼容不同 aiocqhttp 版本的可选事件装饰器"""
        method = getattr(self._bot, method_name, None)
        if not callable(method):
            return
        try:
            decorator = method()
            if callable(decorator):
                decorator(handler)
        except TypeError:
            logger.debug(f"[OneBotAdapter] 当前 aiocqhttp 版本不支持空参数 {method_name}, 已跳过")

    async def _handle_private_message(self, event: dict[str, Any]) -> None:
        """处理私聊消息"""
        if not self.enable_private:
            return
        await self._handle_message_event(event)

    async def _handle_group_message(self, event: dict[str, Any]) -> None:
        """处理群聊消息"""
        if not self.enable_group:
            return
        await self._handle_message_event(event)

    async def _handle_message_event(self, event: dict[str, Any]) -> None:
        """将 OneBot 消息事件转换并提交到 Satrap 管线"""
        try:
            if event.get("self_id"):
                self.bot_self_id = str(event.get("self_id"))
                self.client_self_id = self.bot_self_id
            message = await self.convert_message(event)
            self._commit_platform_message(message)
        except Exception as e:
            logger.error(f"[OneBotAdapter] 处理消息失败: {e}")

    async def _handle_notice(self, event: dict[str, Any]) -> None:
        """记录暂未接入会话管线的 notice 事件"""
        logger.debug(f"[OneBotAdapter] notice 事件暂未处理: {event.get('notice_type')}")

    async def _handle_request(self, event: dict[str, Any]) -> None:
        """记录暂未接入会话管线的 request 事件"""
        logger.debug(f"[OneBotAdapter] request 事件暂未处理: {event.get('request_type')}")

    async def convert_message(self, raw_event: dict[str, Any]) -> PlatformMessage:
        """将 OneBot 消息事件转换为 Satrap PlatformMessage"""
        return create_platform_message(raw_event, self.bot_self_id)

    def _commit_platform_message(self, message: PlatformMessage) -> None:
        """将 PlatformMessage 封装为 MessageEvent 并提交"""
        event = MessageEvent(
            message_str=message.message_str,
            platform_message=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            session_type=self.adapter_type,
            adapter=self,
        )
        self.commit_event(event)

    async def send_text(self, session_id: str, text: str) -> Any:
        """发送纯文本消息"""
        return await self.send_message(session_id, MessageChain.from_text(text))

    async def send_message(self, session_id: str, message: MessageChain) -> Any:
        """按 OneBot 会话 ID 发送完整消息链"""
        if not self._bot:
            logger.error("[OneBotAdapter] 客户端未初始化, 无法发送消息")
            return None

        segments = await message_chain_to_onebot_segments(message.components)
        if not segments:
            logger.warning("[OneBotAdapter] 消息为空, 跳过发送")
            return None

        if is_private_session(session_id):
            return await self._bot.send_private_msg(
                user_id=int(extract_private_user_id(session_id)),
                message=segments,
            )
        if is_group_session(session_id):
            return await self._bot.send_group_msg(
                group_id=int(extract_group_id(session_id)),
                message=segments,
            )

        logger.error(f"[OneBotAdapter] 无法识别的会话 ID: {session_id}")
        return None

    async def send_stream(
        self,
        session_id: str,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> Any:
        """OneBot 流式发送降级为合并或分段发送"""
        if use_fallback:
            result = None
            async for chain in generator:
                result = await self.send_message(session_id, chain)
            return result

        components = []
        async for chain in generator:
            components.extend(chain.components)
        if components:
            return await self.send_message(session_id, MessageChain(components))
        return None

    async def terminate(self) -> None:
        """终止 OneBot 适配器并释放资源"""
        self._running = False
        if self._bot:
            close = getattr(self._bot, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
            self._bot = None
        await super().terminate()

    def get_client(self) -> Any:
        """返回底层 aiocqhttp 客户端"""
        return self._bot
