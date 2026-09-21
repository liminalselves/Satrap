"""
OneBot 平台事件与消息收发适配器

负责账号绑定, 有界消息去重和入站转换,
将发送结果归一为明确回执并协调反向 WebSocket 生命周期
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from collections import OrderedDict
import asyncio
import inspect
import secrets
import aiohttp
from typing import Any, cast
from time import monotonic, time
import json

from satrap.core.platform.onebot.onebot_utils import (
    create_platform_message,
    extract_group_id,
    extract_private_user_id,
    is_group_session,
    is_private_session,
    message_chain_to_onebot_segments,
    onebot_segments_to_components,
    private_session_id,
    normalize_segments,
    group_session_id,
)
from satrap.core.config.platform_policy import validate_wake_policy, validate_context_scope, normalize_group_whitelist, normalize_wake_words
from satrap.core.platform.onebot.outbound import OutboundTurns, split_components
from satrap.core.platform.notices import build_onebot_notice
from satrap.core.platform.receipt import SendReceipt, combine_receipts
from satrap.core.platform.event import MessageChain, MessageEvent, PlatformMetadata
from satrap.core.platform import EventHandler, PlatformAdapter, PlatformConfig, PlatformEvent, register_platform_adapter
from satrap.core.type import PlatformMessage, safe_getattr_callable

from satrap.core.log import logger


class _MissingCQHttp:
    """缺少 aiocqhttp 时的占位类型, 用于给出清晰错误"""


_action_failures: tuple[type[Exception], ...] = ()
try:
    from aiocqhttp.exceptions import ApiNotAvailable, ActionFailed
    from aiocqhttp import CQHttp
    _action_failures = (ApiNotAvailable, ActionFailed)
except ImportError:   # pragma: no cover - 在安装依赖后走真实分支
    CQHttp = _MissingCQHttp


@register_platform_adapter("aiocqhttp")
@register_platform_adapter("onebot")
class OneBotAdapter(PlatformAdapter):
    """OneBot v11 平台适配器, 使用 aiocqhttp 反向 WebSocket"""

    adapter_type = "onebot"

    def __init__(
        self,
        config: PlatformConfig,
        event_handler: EventHandler | None = None,
        event_queue: asyncio.Queue[Any] | None = None,
    ) -> None:
        """
        初始化 OneBotAdapter 实例

        参数:
        - config: 配置信息
        - event_handler: 事件处理器
        - event_queue: 事件queue
        """
        super().__init__(config, event_handler, event_queue)
        settings = config.settings or {}
        self.host = str(settings.get("host") or settings.get("listen_host") or "127.0.0.1")
        self.port = int(settings.get("port") or settings.get("listen_port") or 8080)
        self.access_token = str(settings.get("access_token") or "")
        self.secret = str(settings.get("secret") or "")
        self.enable_private = bool(settings.get("enable_private", True))
        self.enable_group = bool(settings.get("enable_group", True))
        validate_context_scope(settings.get("context_scope", "legacy_user"))
        normalize_group_whitelist(settings.get("group_whitelist", []))
        validate_wake_policy(settings)
        normalize_wake_words(settings.get("wake_words", []))
        normalize_wake_words(settings.get("wake_aliases", []))
        self.bot_self_id = str(settings.get("self_id") or "")
        self.client_self_id = self.bot_self_id
        self._seen_messages: OrderedDict[tuple[str, str, str, str], float] = OrderedDict()
        self._ingress_rejections: dict[str, int] = {"account": 0, "self_echo": 0, "duplicate": 0}
        self._bot: Any = None
        self._running = False
        self._message_lookup_slots = asyncio.Semaphore(4)
        self._outbound = OutboundTurns()
        self._ready_path = "/_satrap_ready/" + secrets.token_urlsafe(24)

    def meta(self) -> PlatformMetadata:
        """
        返回平台元信息

        返回:
        - PlatformMetadata: 平台元信息
        """
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
        async def ready() -> str:
            """返回当前实例的就绪标识, 供本机启动探针核验"""
            return self._ready_path

        self._bot.server_app.add_url_rule(self._ready_path, view_func=ready)
        self._register_handlers()
        self._running = True

        try:
            run_task = safe_getattr_callable(self._bot, "run_task")
            if run_task is not None:
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
        """
        兼容不同 aiocqhttp 版本的可选事件装饰器

        参数:
        - method_name: method名称
        - handler: 处理器
        """
        method = safe_getattr_callable(self._bot, method_name)
        if method is None:
            return
        try:
            decorator = method()
            if callable(decorator):
                decorator(handler)
        except TypeError:
            logger.debug(f"[OneBotAdapter] 当前 aiocqhttp 版本不支持空参数 {method_name}, 已跳过")

    async def _handle_private_message(self, event: dict[str, Any]) -> None:
        """
        处理私聊消息

        参数:
        - event: 事件
        """
        if not self.config.enable or not self.config.settings.get("enable_private", True):
            return
        await self._handle_message_event(event)

    async def _handle_group_message(self, event: dict[str, Any]) -> None:
        """
        处理群聊消息

        参数:
        - event: 事件
        """
        if not self.allows_group(str(event.get("group_id", ""))):
            return
        await self._handle_message_event(event)

    def allows_group(self, group_id: str) -> bool:
        """
        检查当前实例的群范围

        参数:
        - group_id: 目标群 ID

        返回:
        - 群聊已启用且目标在允许范围内时返回 True
        """
        settings = self.config.settings
        groups = normalize_group_whitelist(settings.get("group_whitelist", []))
        return self.config.enable and bool(settings.get("enable_group", True)) and (not groups or group_id in groups)

    async def fetch_group_message(self, message_id: str, group_id: str, user_id: str) -> dict[str, Any]:
        """
        有界回源并核验群与发送者, 不下载附件

        参数:
        - message_id: OneBot 消息 ID
        - group_id: 已授权目标群
        - user_id: 目标会话的路由用户

        返回:
        - dict[str, Any]: 已核验的标准消息事件, 越界或回源失败时抛出异常
        """
        if not self.allows_group(group_id) or self._bot is None:
            raise PermissionError("消息来源不可用")
        async def lookup() -> Any:
            """将等待并发槽位计入总超时"""
            async with self._message_lookup_slots:
                return await self._bot.get_msg(message_id=int(message_id))
        result = await asyncio.wait_for(lookup(), timeout=5)
        if not isinstance(result, dict) or len(json.dumps(result, ensure_ascii=False)) > 65536:
            raise ValueError("消息回源格式或长度不符合限制")
        result = cast(dict[str, Any], result)
        sender = result.get("sender", {})
        if (result.get("message_type") != "group" or str(result.get("group_id")) != group_id
                or str(result.get("message_id")) != message_id or not isinstance(sender, dict)
                or str(cast(dict[str, Any], sender).get("user_id")) != user_id
                or (result.get("self_id") is not None and str(result["self_id"]) != self.bot_self_id)):
            raise PermissionError("消息回源与请求来源不一致")
        if not self.allows_group(group_id):
            raise PermissionError("消息来源已停用")
        return {**result, "self_id": self.bot_self_id, "user_id": user_id, "post_type": "message"}

    async def fetch_quoted_message(self, message_id: str, session_id: str) -> dict[str, Any] | None:
        """
        有界回源被引用消息并核验会话归属, 不递归回源二级引用, 不下载附件

        参数:
        - message_id: OneBot 消息 ID
        - session_id: 当前事件的平台会话 ID

        返回:
        - dict[str, Any] | None: 已转换的组件与元信息; 越界, 超时, 格式不符或群范围外返回 None
        """
        if self._bot is None or not message_id.lstrip("-").isdecimal():
            return None
        if is_group_session(session_id) and not self.allows_group(extract_group_id(session_id)):
            return None
        async def lookup() -> Any:
            """等待并发槽位计入总超时"""
            async with self._message_lookup_slots:
                return await self._bot.get_msg(message_id=int(message_id))
        try:
            result = await asyncio.wait_for(lookup(), timeout=5)
        except Exception:
            return None
        if not isinstance(result, dict) or len(json.dumps(result, ensure_ascii=False)) > 65536:
            return None
        result = cast(dict[str, Any], result)
        if result.get("self_id") is not None and str(result["self_id"]) != self.bot_self_id:
            return None
        sender = result.get("sender", {})
        sender = cast(dict[str, Any], sender) if isinstance(sender, dict) else {}
        sender_id = str(sender.get("user_id") or result.get("user_id") or "")
        if is_group_session(session_id):
            if str(result.get("group_id", "")) != extract_group_id(session_id):
                return None
        elif is_private_session(session_id):
            if str(result.get("group_id", "")) or sender_id not in {extract_private_user_id(session_id), self.bot_self_id}:
                return None
        else:
            return None
        components, message_str = onebot_segments_to_components(normalize_segments(result.get("message")))
        raw_time = result.get("time")
        return {
            "components": components, "message_str": message_str, "sender_id": sender_id,
            "sender_nickname": str(sender.get("card") or sender.get("nickname") or ""),
            "time": int(raw_time) if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool) else 0,
        }

    async def _handle_message_event(self, event: dict[str, Any]) -> None:
        """
        将 OneBot 消息事件转换并提交到 Satrap 管线

        参数:
        - event: 事件
        """
        incoming_self = str(event.get("self_id") or "")
        if not incoming_self or (self.bot_self_id and incoming_self != self.bot_self_id):
            self._ingress_rejections["account"] += 1
            return
        self.bot_self_id = incoming_self
        self.client_self_id = incoming_self
        if str(event.get("user_id")) == incoming_self:
            self._ingress_rejections["self_echo"] += 1
            return
        now = monotonic()
        while self._seen_messages and next(iter(self._seen_messages.values())) <= now:
            self._seen_messages.popitem(last=False)
        raw_message_id = event.get("message_id")
        message_id = str(raw_message_id) if isinstance(raw_message_id, (str, int)) and not isinstance(raw_message_id, bool) else ""
        key = (incoming_self, str(event.get("message_type", "")),
               str(event.get("group_id") or event.get("user_id") or ""), message_id)
        if message_id and key in self._seen_messages:
            self._ingress_rejections["duplicate"] += 1
            return
        if message_id:
            self._seen_messages[key] = now + 120
            while len(self._seen_messages) > 4096:
                self._seen_messages.popitem(last=False)
        # 转换前预占消息标识, 防止并发回调在 await 期间重复提交; 不缓存正文
        accepted = False
        try:
            message = await self.convert_message(event)
            accepted = self._commit_platform_message(message)
        except Exception as e:
            logger.error(f"[OneBotAdapter] 处理消息失败: {e}")
        finally:
            if message_id and not accepted:
                self._seen_messages.pop(key, None)
            # 转换失败, 取消或队列满时允许上游再次投递

    async def _handle_notice(self, event: dict[str, Any]) -> None:
        """
        将 notice 归一为 PlatformEvent 并派发, 不进入会话管线

        参数:
        - event: OneBot 原始 notice
        """
        await self._emit_notice(event)

    async def _handle_request(self, event: dict[str, Any]) -> None:
        """
        将 request 归一为 PlatformEvent 并派发, 不自动审批

        参数:
        - event: OneBot 原始 request
        """
        await self._emit_notice(event)

    async def _emit_notice(self, raw: dict[str, Any]) -> None:
        """
        按订阅类型与群范围过滤后提交通知事件

        参数:
        - raw: notice 或 request 原始事件
        """
        payload = build_onebot_notice(raw, self.bot_self_id)
        if payload is None:
            self._ingress_rejections["account"] += 1
            return
        event_type = f"{payload.category}.{payload.kind}"
        enabled = self.config.settings.get("notice_types")
        if isinstance(enabled, list) and event_type not in cast(list[object], enabled) and payload.category not in cast(list[object], enabled):
            return
        if payload.group_id and not self.allows_group(payload.group_id):
            return
        # 群范围外的通知静默丢弃, 好友请求等无群事件不受白名单影响
        session_id = group_session_id(payload.group_id) if payload.group_id else private_session_id(payload.user_id) if payload.user_id else ""
        await self.emit_event(PlatformEvent(
            platform_id=self.config.id, platform_type=self.config.type, event_type=event_type,
            session_id=session_id, user_id=payload.user_id, group_id=payload.group_id,
            raw_event=raw, timestamp=float(payload.time or time()), extras={"payload": payload},
        ))

    async def convert_message(self, raw_event: dict[str, Any]) -> PlatformMessage:
        """
        将 OneBot 消息事件转换为 Satrap PlatformMessage

        参数:
        - raw_event: raw事件

        返回:
        - PlatformMessage: 将 OneBot 消息事件转换为 Satrap PlatformMessage
        """
        return create_platform_message(raw_event, self.bot_self_id)

    def _commit_platform_message(self, message: PlatformMessage) -> bool:
        """
        将 PlatformMessage 封装为 MessageEvent 并提交

        参数:
        - message: 消息内容

        返回:
        - bool: 入队成功返回 True, 队列满返回 False
        """
        event = MessageEvent(
            message_str=message.message_str,
            platform_message=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            session_provider=self.get_session_provider(),
            session_type=self.get_session_type(),
            adapter=self,
        )
        return self.commit_event(event)

    def get_stats(self) -> dict[str, Any]:
        """
        返回平台运行状态及有界入站过滤统计

        返回:
        - dict[str, Any]: 通用状态与账号绑定, 回声和重复消息拒绝计数
        """
        return {**super().get_stats(), "ingress": {
            **self._ingress_rejections, "dedup_entries": len(self._seen_messages),
            "dedup_capacity": 4096, "dedup_ttl": 120,
        }}

    async def send_text(self, session_id: str, text: str) -> SendReceipt:
        """
        发送纯文本消息

        参数:
        - session_id: 会话 ID
        - text: 待处理文本

        返回:
        - SendReceipt: 平台确认或失败状态
        """
        return await self.send_message(session_id, MessageChain.from_text(text))

    async def send_message(self, session_id: str, message: MessageChain) -> SendReceipt:
        """
        在有界队列中按顺序发送完整逻辑回复

        参数:
        - session_id: 平台会话 ID
        - message: 待发送消息链

        返回:
        - SendReceipt: 全部已尝试块的回执, 满载或等待超时为明确失败
        """
        try:
            async with self._outbound.turn(session_id):
                return await self._send_message(session_id, message)
        except (RuntimeError, asyncio.TimeoutError):
            return SendReceipt("failed", reason="send_queue_unavailable")

    async def _send_message(self, session_id: str, message: MessageChain) -> SendReceipt:
        """
        为已获取执行权的逻辑回复创建并执行分块计划

        参数:
        - session_id: 平台会话 ID
        - message: 待拆分组件

        返回:
        - SendReceipt: 首次失败即停止的聚合结果
        """
        limit = int(self.config.settings.get("message_text_limit", 2000))
        chunks = split_components(message.components, limit)
        if len(chunks) == 1:
            return await self._send_chunk(session_id, MessageChain(chunks[0]))
        receipts: list[SendReceipt] = []
        for chunk in chunks:
            try:
                result = await self._send_chunk(session_id, MessageChain(chunk))
            except PermissionError:
                if not receipts:
                    raise
                result = SendReceipt("failed", reason="target_unavailable")
            except Exception:
                result = SendReceipt("failed", reason="message_conversion_failed")
            receipts.append(result)
            if result.status != "success":
                break
        return combine_receipts(receipts)

    async def _send_chunk(self, session_id: str, message: MessageChain) -> SendReceipt:
        """
        按 OneBot 会话 ID 发送完整消息链

        参数:
        - session_id: 会话 ID
        - message: 消息内容

        返回:
        - SendReceipt: 平台确认或失败状态, 无回包不视为成功
        """
        if not self._bot:
            logger.error("[OneBotAdapter] 客户端未初始化, 无法发送消息")
            return SendReceipt("failed", reason="client_unavailable")

        if is_group_session(session_id) and not self.allows_group(extract_group_id(session_id)):
            raise PermissionError("目标群不在当前适配器允许范围内")

        segments = await message_chain_to_onebot_segments(message.components)
        if not segments:
            logger.warning("[OneBotAdapter] 消息为空, 跳过发送")
            return SendReceipt("failed", reason="empty_message")

        try:
            if is_private_session(session_id):
                result = await self._bot.send_private_msg(
                    user_id=int(extract_private_user_id(session_id)), message=segments,
                )
            elif is_group_session(session_id):
                result = await self._bot.send_group_msg(
                    group_id=int(extract_group_id(session_id)), message=segments,
                )
            else:
                return SendReceipt("failed", reason="invalid_session")
        except _action_failures:
            return SendReceipt("failed", reason="action_rejected")
        except Exception:
            return SendReceipt("unknown", reason="action_unconfirmed")
        # 动作提交后异常无法证明平台未发送, 不向上层提供自动重试依据
        if isinstance(result, dict):
            message_id = cast(dict[str, Any], result).get("message_id")
            if isinstance(message_id, (str, int)) and not isinstance(message_id, bool) and str(message_id):
                return SendReceipt("success", (str(message_id),))
        return SendReceipt("unknown", reason="missing_message_id")

    async def send_stream(
        self,
        session_id: str,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> SendReceipt:
        """
        为整个流式回复持有目标执行权

        参数:
        - session_id: 平台会话 ID
        - generator: 消息块生成器
        - use_fallback: 是否逐块发送

        返回:
        - SendReceipt: 流式聚合回执, 等待失败不会提交动作
        """
        try:
            async with self._outbound.turn(session_id):
                return await self._send_stream(session_id, generator, use_fallback)
        except (RuntimeError, asyncio.TimeoutError):
            return SendReceipt("failed", reason="send_queue_unavailable")

    async def _send_stream(
        self,
        session_id: str,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> SendReceipt:
        """
        OneBot 流式发送降级为合并或分段发送

        参数:
        - session_id: 会话 ID
        - generator: 生成器
        - use_fallback: True 按生成器分块发送, False 合并后发送

        返回:
        - SendReceipt: 汇总各块确认结果, 首次失败后停止发送
        """
        if use_fallback:
            receipts: list[SendReceipt] = []
            try:
                async for chain in generator:
                    result = await self._send_message(session_id, chain)
                    receipts.append(result)
                    if result.status != "success":
                        break
            except PermissionError:
                receipts.append(SendReceipt("failed", reason="target_unavailable"))
            except Exception:
                receipts.append(SendReceipt("unknown", reason="stream_interrupted"))
            return combine_receipts(receipts)

        components: list[Any] = []
        async for chain in generator:
            components.extend(chain.components)
        if components:
            return await self._send_message(session_id, MessageChain(components))
        return SendReceipt("failed", reason="empty_message")

    async def wait_ready(self, timeout: float = 5.0) -> None:
        """
        验证当前实例已实际监听, 不将其他进程占用的端口误判为就绪

        参数:
        - timeout: 最长就绪等待秒数
        """
        host = "127.0.0.1" if self.host in {"0.0.0.0", "localhost"} else self.host
        host = "[::1]" if host == "::" else f"[{host}]" if ":" in host and not host.startswith("[") else host
        deadline = asyncio.get_running_loop().time() + timeout
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.5)) as client:
            while asyncio.get_running_loop().time() < deadline:
                await super().wait_ready(timeout)
                try:
                    async with client.get(f"http://{host}:{self.port}{self._ready_path}") as response:
                        if response.status == 200 and await response.text() == self._ready_path:
                            return
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                await asyncio.sleep(0.05)
        raise TimeoutError("OneBot 监听服务未就绪")

    async def terminate(self) -> None:
        """终止 OneBot 适配器并释放资源"""
        self._running = False
        await self._outbound.close()
        await super().terminate()
        self._bot = None
        self._seen_messages.clear()

    def get_client(self) -> Any:
        """
        返回底层 aiocqhttp 客户端

        返回:
        - Any: 底层 aiocqhttp 客户端
        """
        return self._bot
