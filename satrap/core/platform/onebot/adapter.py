"""
OneBot 平台事件与消息收发适配器

负责账号绑定, 有界消息去重和入站转换,
将发送结果归一为明确回执并协调反向 WebSocket 生命周期
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from collections import OrderedDict
import asyncio
import hashlib
import inspect
import os
import secrets
import aiohttp
from typing import Any, Literal, cast
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
    parse_forward_nodes,
    private_session_id,
    normalize_segments,
    group_session_id,
)
from satrap.core.config.platform_policy import validate_wake_policy, validate_context_scope, normalize_group_whitelist, normalize_wake_words
from satrap.core.platform.onebot.outbound import OutboundTurns, flatten_forward_nodes, split_components, split_forward_turns
from satrap.core.platform.onebot.admin import ADMIN_CAPABILITIES, _CAPABILITY_ACTIONS, OneBotAdmin, is_missing_action_error
from satrap.core.platform.onebot.request_registry import RequestApprovalLedger, RequestFlagRegistry
from satrap.core.platform.notices import build_onebot_notice, notice_attachment
from satrap.core.platform.receipt import SendAttemptRecorder, SendReceipt, combine_receipts
from satrap.core.components import At, BaseMessageComponent, File, Node, Plain, Reply
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


_RECEIPT_TO_SEGMENT_STATUS = {"success": "sent", "partial": "partial", "failed": "failed", "unknown": "unknown"}
"""回执状态到发送尝试段状态的映射"""

_ATTEMPT_FINALIZE_TIMEOUT = 2.0
"""发送尝试收尾的有界等待秒数, 超时保持未确认而不是谎报终态"""


def _turn_signature(payload: list[BaseMessageComponent]) -> tuple[str, int]:
    """段摘要: 组件类型与关键字段的散列及正文字符数, 不落正文原文"""
    parts: list[str] = []
    chars = 0
    for component in payload:
        marker = component.type.value
        text = ""
        if isinstance(component, Plain):
            text = component.text
            chars += len(text)
        elif isinstance(component, At):
            text = str(component.qq)
        elif isinstance(component, Reply):
            text = str(component.id)
        elif isinstance(component, File):
            text = f"{component.name}|{component.file_}|{component.url}"
        elif isinstance(component, Node):
            inner = "".join(item.text for item in component.content if isinstance(item, Plain))
            text = f"{component.name}|{inner}"
            chars += len(inner)
        parts.append(f"{marker}:{text}")
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]
    return digest, chars


def _plan_send_steps(
    turns: list[tuple[str, list[BaseMessageComponent]]], limit: int,
) -> list[tuple[str, list[BaseMessageComponent]]]:
    """把轮次计划展开为与计划段一一对应的执行步骤 (forward/file 各一步, 普通轮按分块展开)"""
    steps: list[tuple[str, list[BaseMessageComponent]]] = []
    for kind, payload in turns:
        if kind == "forward":
            nodes: list[BaseMessageComponent] = [component for component in payload if isinstance(component, Node)]
            steps.append(("forward", nodes))
            continue
        if kind == "file":
            files: list[BaseMessageComponent] = [component for component in payload if isinstance(component, File)]
            if files:
                steps.append(("file", files[:1]))
            continue
        for chunk in split_components(payload, limit):
            steps.append(("chunk", chunk))
    return steps


def _plan_send_segments(turns: list[tuple[str, list[BaseMessageComponent]]], limit: int) -> list[dict[str, Any]]:
    """由执行步骤生成段摘要, 与发送循环的下标严格一一对应"""
    plan: list[dict[str, Any]] = []
    for kind, payload in _plan_send_steps(turns, limit):
        digest, chars = _turn_signature(payload)
        plan.append({"index": len(plan), "kind": kind, "chars": 0 if kind == "file" else chars, "digest": digest})
    return plan


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
        self._ingress_rejections: dict[str, int] = {"account": 0, "self_echo": 0, "duplicate": 0, "handler_error": 0}
        self._bot: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._message_lookup_slots = asyncio.Semaphore(4)
        self._warned_at: dict[str, float] = {}
        self._outbound = OutboundTurns()
        self.admin = OneBotAdmin(self, _action_failures)
        self.request_flags = RequestFlagRegistry(adapter_id=config.id)
        self._ready_path = "/_satrap_ready/" + secrets.token_urlsafe(24)
        self._capability_states: dict[str, tuple[int, str]] = {}
        self._connection_generation = 0
        self._client_connected = False
        self._meta_hooked = False
        self._heartbeats = 0
        self._last_heartbeat_at = 0.0
        self._send_attempt_recorder: SendAttemptRecorder | None = None

    def set_send_attempt_recorder(self, recorder: SendAttemptRecorder | None) -> None:
        """
        装配发送尝试持久化记录器 (由后端在适配器创建后注入)

        参数:
        - recorder: 状态存储, None 表示仅进程内回执 (测试与精简运行时)
        """
        self._send_attempt_recorder = recorder

    def set_request_ledger(self, ledger: RequestApprovalLedger | None) -> None:
        """
        装配跨重启的审批身份账本 (由后端在适配器创建后注入)

        参数:
        - ledger: 审批账本, None 表示回落到仅进程内账本 (测试与精简运行时)
        """
        self.request_flags.set_ledger(ledger if ledger is not None else RequestApprovalLedger())

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
        self._loop = asyncio.get_running_loop()
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

        self._bot.on_message("private")(self._guard_handler("private_message", self._handle_private_message))
        self._bot.on_message("group")(self._guard_handler("group_message", self._handle_group_message))
        self._register_optional_handler("on_notice", self._guard_handler("notice", self._handle_notice))
        self._register_optional_handler("on_request", self._guard_handler("request", self._handle_request))
        self._meta_hooked = self._register_optional_handler("on_meta_event", self._guard_handler("meta_event", self._handle_meta))

    def _warn_limited(self, key: str, message: str, interval: float = 60.0) -> None:
        """
        同一类告警在 interval 秒内只输出一次, 其余降为 debug, 防止持续故障期间刷屏

        参数:
        - key: 告警类别
        - message: 日志内容
        - interval: 抑制窗口秒数
        """
        now = monotonic()
        if now - self._warned_at.get(key, float("-inf")) >= interval:
            self._warned_at[key] = now
            logger.warning(message)
        else:
            logger.debug(message)

    def _guard_handler(self, name: str, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> Callable[[dict[str, Any]], Awaitable[None]]:
        """
        为 aiocqhttp 回调加顶层兜底: aiocqhttp 用无回调的 create_task 派发, 异常否则只在 GC 时可见

        参数:
        - name: 日志中的处理器名
        - handler: 原始处理协程

        返回:
        - 包装后的处理协程, 单条坏事件只计数与记日志, 不改变适配器状态
        """
        async def guarded(event: dict[str, Any]) -> None:
            try:
                await handler(event)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._ingress_rejections["handler_error"] += 1
                logger.error(f"[OneBotAdapter] {name} 处理异常: {type(error).__name__}: {error}")
        return guarded

    def _register_optional_handler(self, method_name: str, handler: Any) -> bool:
        """
        兼容不同 aiocqhttp 版本的可选事件装饰器

        参数:
        - method_name: method名称
        - handler: 处理器

        返回:
        - bool: 装饰器实际完成注册时返回 True
        """
        method = safe_getattr_callable(self._bot, method_name)
        if method is None:
            return False
        try:
            decorator = method()
            if callable(decorator):
                decorator(handler)
                return True
        except TypeError:
            logger.debug(f"[OneBotAdapter] 当前 aiocqhttp 版本不支持空参数 {method_name}, 已跳过")
        return False

    async def _handle_meta(self, event: dict[str, Any]) -> None:
        """
        跟踪客户端连接生命周期与心跳, 能力学习按连接代次失效

        参数:
        - event: OneBot 原始 meta 事件

        lifecycle/connect 开启新连接代次并清空已学习状态, 心跳只证明连接存活;
        无法核验账号身份的 meta 事件不采纳
        """
        incoming_self = str(event.get("self_id") or "")
        if not incoming_self or (self.bot_self_id and incoming_self != self.bot_self_id):
            self._ingress_rejections["account"] += 1
            return
        if not self.bot_self_id:
            self.bot_self_id = incoming_self
            self.client_self_id = incoming_self
        meta_type = str(event.get("meta_event_type") or "")
        if meta_type == "lifecycle":
            sub_type = str(event.get("sub_type") or "")
            if sub_type == "connect":
                self._connection_generation += 1
                self._client_connected = True
                self._heartbeats = 0
                self._last_heartbeat_at = 0.0
                self._capability_states.clear()
                # 新代次被动重新学习, 不沿用上一连接的任何结论
                logger.info(f"[OneBotAdapter] OneBot 客户端已连接 adapter={self.config.id} self_id={incoming_self} generation={self._connection_generation}")
            elif sub_type == "disable":
                self._client_connected = False
        elif meta_type == "heartbeat":
            self._client_connected = True
            self._heartbeats += 1
            self._last_heartbeat_at = monotonic()

    def note_action_outcome(self, action: str, supported: bool) -> None:
        """
        被动记录协议动作在当前连接代次的可用性, 不做主动探测

        参数:
        - action: OneBot 协议动作名
        - supported: 动作成功执行为 True, 实现缺失 (10002/1404) 为 False
        """
        if len(self._capability_states) >= 128:
            self._capability_states = {
                key: value for key, value in self._capability_states.items()
                if value[0] == self._connection_generation
            }
        self._capability_states[action] = (self._connection_generation, "supported" if supported else "unsupported")

    def connection_generation(self) -> int:
        """
        返回当前连接代次, 供回源等待后的来源证明复查

        返回:
        - int: 连接代次, 未收到过连接生命周期事件时为 0
        """
        return self._connection_generation

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
            if not isinstance(result, dict) or len(json.dumps(result, ensure_ascii=False)) > 65536:
                return None
        except Exception as error:
            logger.debug(f"[OneBotAdapter] 引用回源失败 message_id={message_id}: {type(error).__name__}")
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

    async def fetch_forward_message(self, forward_id: str, session_id: str, *, expect_group_id: str = "") -> list[Node] | None:
        """
        有界回源合并转发节点, 不递归展开嵌套转发, 不下载附件

        参数:
        - forward_id: OneBot 转发消息 ID
        - session_id: 当前事件的平台会话 ID, 用于校验群范围
        - expect_group_id: 期望的群号, 非空时回包若明确携带其他群号即拒绝

        返回:
        - list[Node] | None: 至多 20 个已归一节点; 越界, 超时, 格式不符, 群范围外或群号矛盾返回 None
        """
        if self._bot is None or not forward_id:
            return None
        if is_group_session(session_id) and not self.allows_group(extract_group_id(session_id)):
            return None

        async def lookup() -> Any:
            """等待并发槽位计入总超时"""
            async with self._message_lookup_slots:
                return await self._bot.get_forward_msg(id=forward_id)
        try:
            result = await asyncio.wait_for(lookup(), timeout=5)
            if not isinstance(result, dict) or len(json.dumps(result, ensure_ascii=False)) > 262144:
                return None
        except Exception as error:
            if _action_failures and isinstance(error, _action_failures) and is_missing_action_error(error):
                self.note_action_outcome("get_forward_msg", False)
            logger.debug(f"[OneBotAdapter] 转发回源失败 forward_id={forward_id}: {type(error).__name__}")
            return None
        self.note_action_outcome("get_forward_msg", True)
        result = cast(dict[str, Any], result)
        if result.get("self_id") is not None and str(result["self_id"]) != self.bot_self_id:
            return None
        raw_group = result.get("group_id")
        if expect_group_id and raw_group is not None and str(raw_group) != expect_group_id:
            # 回包群号与来源证明矛盾时拒绝, 字段缺失不构成独立授权依据
            logger.debug(f"[OneBotAdapter] 转发回源群号与来源不一致 forward_id={forward_id}")
            return None
        raw_messages = result.get("messages", result.get("message"))
        if not isinstance(raw_messages, list):
            return None
        return parse_forward_nodes(cast(list[Any], raw_messages))

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
        if not self.bot_self_id:
            logger.info(f"[OneBotAdapter] OneBot 客户端已连接 adapter={self.config.id} self_id={incoming_self}")
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
        await self._register_request_flag(event)
        await self._emit_notice(event)

    async def _register_request_flag(self, event: dict[str, Any]) -> None:
        """
        登记 request 事件 flag 供审批动作核验归属, 可信账号身份核验先于登记

        参数:
        - event: OneBot 原始 request

        登记是安全机制, 不依赖通知订阅与群白名单过滤; 账号不符或无法核验时不登记,
        对应 flag 后续审批将因未登记被拒绝; 登记写入持久账本, 失败只告警不阻断通知派发
        """
        incoming_self = str(event.get("self_id") or "")
        if not incoming_self or (self.bot_self_id and incoming_self != self.bot_self_id):
            self._ingress_rejections["account"] += 1
            return
        flag = str(event.get("flag") or "").strip()
        if not flag:
            return
        request_type = str(event.get("request_type") or "")
        user_id = str(event.get("user_id") or "")
        try:
            if request_type == "group":
                sub_type = str(event.get("sub_type") or "")
                group_id = str(event.get("group_id") or "")
                if sub_type not in {"add", "invite"} or not group_id.isdecimal():
                    return
                await self.request_flags.register(
                    "group", flag, self_id=incoming_self, group_id=group_id, sub_type=sub_type, user_id=user_id,
                )
            elif request_type == "friend":
                await self.request_flags.register("friend", flag, self_id=incoming_self, user_id=user_id)
        except Exception as error:
            # 登记失败的 flag 无法被审批, 保守行为是拒绝执行而不是放行
            logger.error(f"[OneBotAdapter] request flag 登记失败: {type(error).__name__}: {error}")

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
        extras: dict[str, Any] = {"payload": payload}
        attachment = notice_attachment(payload)
        if attachment is not None:
            # 群文件上传归一为附件事件, 下载与模型处理留给会话触发策略
            extras["attachment"] = attachment
        await self.emit_event(PlatformEvent(
            platform_id=self.config.id, platform_type=self.config.type, event_type=event_type,
            session_id=session_id, user_id=payload.user_id, group_id=payload.group_id,
            raw_event=raw, timestamp=float(payload.time or time()), extras=extras,
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
        }, "capabilities": self.admin_capabilities()}

    def admin_capabilities(self) -> dict[str, str]:
        """
        返回管理动作的 unavailable/unknown/supported/unsupported 四态, 不执行写动作探测

        返回:
        - dict[str, str]: 动作名到状态; 连接状态来自 meta 事件 (lifecycle/heartbeat),
          当前 aiocqhttp 版本不支持 meta 订阅时无法判定连接, 保持 unknown;
          单动作状态来自被动学习且按连接代次失效, 未学习到的动作一律 unknown
        """
        if self._bot is None or not self._running:
            return {name: "unavailable" for name in ADMIN_CAPABILITIES}
        if self._meta_hooked:
            connected = self._client_connected
            if connected and self._heartbeats >= 2 and monotonic() - self._last_heartbeat_at > 90:
                # 心跳流已建立却长期静默, 连接大概率已断开而服务端无从感知
                connected = False
            if not connected:
                return {name: "unavailable" for name in ADMIN_CAPABILITIES}
        generation = self._connection_generation
        states: dict[str, str] = {}
        for name in ADMIN_CAPABILITIES:
            actions = _CAPABILITY_ACTIONS.get(name, ())
            learned = [
                entry[1] for action in actions
                if (entry := self._capability_states.get(action)) is not None and entry[0] == generation
            ]
            if any(state == "unsupported" for state in learned):
                states[name] = "unsupported"
            elif actions and len(learned) == len(actions) and all(state == "supported" for state in learned):
                states[name] = "supported"
            else:
                states[name] = "unknown"
        return states

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

    async def send_message(
        self, session_id: str, message: MessageChain, *, request_id: str = "",
        purpose: str = "business", require_tracking: bool = False,
    ) -> SendReceipt:
        """
        在有界队列中按顺序发送完整逻辑回复

        参数:
        - session_id: 平台会话 ID
        - message: 待发送消息链
        - request_id: 可选的逻辑请求标识, 发送尝试记录据此关联手动唤醒请求
        - purpose: business 业务输出或 error_feedback 错误反馈
        - require_tracking: 发送前记录不可用时不发业务输出 (已受理请求不冒充可恢复)

        返回:
        - SendReceipt: 全部已尝试块的回执, 满载或等待超时为明确失败
        """
        try:
            return await self._outbound.run(
                session_id,
                lambda: self._send_message(
                    session_id, message, request_id=request_id, purpose=purpose, require_tracking=require_tracking,
                ),
            )
        except (RuntimeError, asyncio.TimeoutError):
            return SendReceipt("failed", reason="send_queue_unavailable")

    async def _send_message(
        self, session_id: str, message: MessageChain, *, request_id: str = "",
        purpose: str = "business", require_tracking: bool = False,
    ) -> SendReceipt:
        """
        为已获取执行权的逻辑回复创建并执行分块计划, 逐段落盘发送证据

        计划在 I/O 之前落盘 (段状态 planned), 每段 I/O 前推进 submitted, 确认后立即保存该段结果,
        未尝试段由收尾标 skipped; 启用 require_tracking 而记录不可用时不发业务输出

        参数:
        - session_id: 平台会话 ID
        - message: 待拆分组件
        - request_id: 可选的逻辑请求标识
        - purpose: business 或 error_feedback
        - require_tracking: 是否要求发送前的必要记录

        返回:
        - SendReceipt: 首次失败即停止的聚合结果
        """
        limit = int(self.config.settings.get("message_text_limit", 2000))
        turns = split_forward_turns(message.components)
        steps = _plan_send_steps(turns, limit)
        plan = _plan_send_segments(turns, limit)
        recorder = self._send_attempt_recorder
        turn_id = ""
        if recorder is not None and plan and not recorder.degraded:
            turn_id = secrets.token_hex(12)
            try:
                recorded = await asyncio.to_thread(
                    recorder.record_send_attempt, turn_id, self.config.id, session_id, request_id, plan, purpose,
                )
            except Exception as error:
                logger.warning(f"[OneBotAdapter] 发送尝试落盘失败, 继续发送 turn={turn_id}: {type(error).__name__}")
                recorded = False
            if not recorded:
                turn_id = ""
        if require_tracking and plan and not turn_id:
            # 已受理请求的发送证据缺失: 不发业务输出, 保持不可确认而不是假装可恢复
            logger.error(
                f"[OneBotAdapter] 发送前记录不可用, 拒绝业务发送 session={session_id} request={request_id}",
            )
            return SendReceipt("unknown", reason="tracking_unavailable")
        receipts: list[SendReceipt] = []
        combined: SendReceipt | None = None
        gaps: list[int] = []
        try:
            for index, (kind, payload) in enumerate(steps):
                marked = await self._mark_segment_submitted(turn_id, index)
                if kind == "forward":
                    try:
                        result = await self._send_forward(session_id, cast(list[Node], payload), limit)
                    except PermissionError:
                        result = SendReceipt("failed", reason="target_unavailable")
                    except Exception:
                        result = SendReceipt("failed", reason="message_conversion_failed")
                elif kind == "file":
                    try:
                        result = await self._send_file(session_id, cast(File, payload[0]))
                    except PermissionError:
                        result = SendReceipt("failed", reason="target_unavailable")
                    except Exception:
                        result = SendReceipt("failed", reason="message_conversion_failed")
                else:
                    result = await self._send_chunk_guarded(session_id, MessageChain(payload))
                receipts.append(result)
                stored = await self._record_segment_result(
                    turn_id, index, _RECEIPT_TO_SEGMENT_STATUS[result.status],
                    advance_to=index + 1 if result.status == "success" and index + 1 < len(steps) else None,
                )
                if not marked and not stored:
                    # 该段已经发出却没有留下任何证据
                    gaps.append(index)
                if result.status != "success":
                    break
            combined = combine_receipts(receipts)
            return combined
        finally:
            if turn_id:
                # 收尾归本次尝试所有: 外层取消或超时也保留已落定的段证据
                if gaps:
                    # 已经发出但没有任何落盘证据的段: 只能按无法确认收尾
                    await self._finalize_attempt(turn_id, "unknown", "tracking_incomplete", untracked=gaps)
                else:
                    await self._finalize_attempt(
                        turn_id,
                        _RECEIPT_TO_SEGMENT_STATUS[combined.status] if combined is not None else "unknown",
                        combined.reason if combined is not None else "send_interrupted",
                    )

    async def _mark_segment_submitted(self, turn_id: str, index: int) -> bool:
        """段 I/O 之前推进 submitted, 记录失败只告警不阻断发送"""
        recorder = self._send_attempt_recorder
        if not turn_id or recorder is None:
            return False
        try:
            recorded = await asyncio.to_thread(recorder.mark_segment_submitted, turn_id, index)
        except Exception as error:
            logger.warning(f"[OneBotAdapter] 段提交状态落盘失败 turn={turn_id} index={index}: {type(error).__name__}")
            return False
        if not recorded:
            logger.warning(f"[OneBotAdapter] 段提交状态未生效 turn={turn_id} index={index}")
        return recorded

    async def _record_segment_result(self, turn_id: str, index: int, status: str, *, advance_to: int | None) -> bool:
        """段结果确认后立即落盘, 失败只告警; 证据缺失的段按未确认处理"""
        recorder = self._send_attempt_recorder
        if not turn_id or recorder is None:
            return False
        try:
            recorded = await asyncio.to_thread(recorder.record_segment_result, turn_id, index, status, advance_to=advance_to)
        except Exception as error:
            logger.warning(f"[OneBotAdapter] 段结果落盘失败 turn={turn_id} index={index}: {type(error).__name__}")
            return False
        if not recorded:
            logger.warning(f"[OneBotAdapter] 段结果未落盘 turn={turn_id} index={index} status={status}")
        return recorded

    async def _finalize_attempt(
        self, turn_id: str, status: str, detail: str, *, untracked: list[int] | None = None,
    ) -> None:
        """
        收尾发送尝试记录, 有界等待且不被外层取消打断

        参数:
        - turn_id: 本次尝试标识
        - status: 聚合结论
        - detail: 脱敏原因
        - untracked: 已发出但没有落盘证据的段序号

        收尾超时或被取消时记录保持未确认: 后续可信确认可精化结果, 但不会重发
        """
        recorder = self._send_attempt_recorder
        if recorder is None:
            return
        worker = asyncio.ensure_future(
            asyncio.to_thread(recorder.complete_send_attempt, turn_id, status, detail, untracked or []),
        )
        try:
            await asyncio.wait_for(asyncio.shield(worker), _ATTEMPT_FINALIZE_TIMEOUT)
        except asyncio.CancelledError:
            logger.warning(f"[OneBotAdapter] 发送收尾被取消, 记录保持未确认 turn={turn_id}")
        except asyncio.TimeoutError:
            logger.warning(f"[OneBotAdapter] 发送收尾超时, 记录保持未确认 turn={turn_id}")
        except Exception as error:
            logger.warning(f"[OneBotAdapter] 发送收尾失败 turn={turn_id}: {type(error).__name__}: {error}")

    async def _send_chunk_guarded(self, session_id: str, chain: MessageChain) -> SendReceipt:
        """
        发送单个分块, 把范围拒绝与转换异常归一为失败回执而不是抛给调用方

        参数:
        - session_id: 目标会话
        - chain: 单个分块的消息链

        返回:
        - SendReceipt: 平台确认或失败状态
        """
        try:
            return await self._send_chunk(session_id, chain)
        except PermissionError:
            return SendReceipt("failed", reason="target_unavailable")
        except Exception:
            return SendReceipt("failed", reason="message_conversion_failed")

    async def _send_forward(self, session_id: str, nodes: list[Node], limit: int) -> SendReceipt:
        """
        通过专用转发接口发送合并转发节点, 实现不支持时降级为分段发送

        参数:
        - session_id: 平台会话 ID
        - nodes: 待发送的合并转发节点
        - limit: 降级分段时的每块文本字符上限

        返回:
        - SendReceipt: 平台确认或失败状态, 无回包不视为成功
        """
        if not self._bot:
            self._warn_limited("client_unavailable", "[OneBotAdapter] 客户端未初始化, 无法发送消息")
            return SendReceipt("failed", reason="client_unavailable")
        if is_group_session(session_id) and not self.allows_group(extract_group_id(session_id)):
            raise PermissionError("目标群不在当前适配器允许范围内")
        if not nodes:
            return SendReceipt("failed", reason="empty_message")

        messages = [await node.to_dict() for node in nodes]
        forward_action = "send_group_forward_msg" if is_group_session(session_id) else "send_private_forward_msg"
        try:
            result = await self._dispatch_action(session_id, "send_private_forward_msg", "send_group_forward_msg", messages=messages)
        except ValueError:
            return self._failed_receipt(session_id, "forward", "invalid_session")
        except _action_failures as error:
            if not is_missing_action_error(error):
                return self._failed_receipt(session_id, "forward", "action_rejected", error)
            self.note_action_outcome(forward_action, False)
            self._warn_limited("forward_degraded", "[OneBotAdapter] 当前实现缺少合并转发接口, 降级为分段发送", interval=600)
            return await self._send_forward_degraded(session_id, nodes, limit)
        except Exception as error:
            return self._failed_receipt(session_id, "forward", "action_unconfirmed", error, status="unknown")
        self.note_action_outcome(forward_action, True)
        return self._receipt_from_result(result)

    async def _send_file(self, session_id: str, component: File) -> SendReceipt:
        """
        按目标实现能力经 upload_group_file/upload_private_file 上传文件组件

        参数:
        - session_id: 平台会话 ID
        - component: 文件组件, 本地路径优先, 其次 file 原始值或 URL, 由实现服务端处理

        返回:
        - SendReceipt: 无可用来源为明确失败; 上传成功响应含 file_id 不含 message_id,
          不套用普通消息回执假设; 实现缺失上传接口时记入能力缓存并回落未验证兼容路径,
          同连接代次内不重复试错
        """
        if not self._bot:
            self._warn_limited("client_unavailable", "[OneBotAdapter] 客户端未初始化, 无法发送消息")
            return SendReceipt("failed", reason="client_unavailable")
        if is_group_session(session_id) and not self.allows_group(extract_group_id(session_id)):
            raise PermissionError("目标群不在当前适配器允许范围内")
        raw_file = (component.file_ or "").strip()
        source = ""
        if raw_file:
            local_path = raw_file.removeprefix("file:///").removeprefix("file://")
            source = os.path.abspath(local_path) if os.path.exists(local_path) else raw_file
        elif component.url:
            source = component.url.strip()
        if not source:
            return SendReceipt("failed", reason="empty_file")
        name = (component.name or "").strip() or os.path.basename(source) or "file"

        upload_action = "upload_group_file" if is_group_session(session_id) else "upload_private_file"
        learned = self._capability_states.get(upload_action)
        if learned is not None and learned[0] == self._connection_generation and learned[1] == "unsupported":
            return await self._send_file_fallback(session_id, component)
        try:
            result = await self._dispatch_action(session_id, "upload_private_file", "upload_group_file", file=source, name=name)
        except ValueError:
            return self._failed_receipt(session_id, "file", "invalid_session")
        except _action_failures as error:
            if is_missing_action_error(error):
                self.note_action_outcome(upload_action, False)
                return await self._send_file_fallback(session_id, component)
            return self._failed_receipt(session_id, "file", "action_rejected", error)
        except Exception as error:
            return self._failed_receipt(session_id, "file", "action_unconfirmed", error, status="unknown")
        self.note_action_outcome(upload_action, True)
        payload = cast(dict[str, Any], result) if isinstance(result, dict) else {}
        raw_file_id = payload.get("file_id")
        file_ids = (str(raw_file_id),) if isinstance(raw_file_id, (str, int)) and not isinstance(raw_file_id, bool) and str(raw_file_id) else ()
        return SendReceipt("success", file_ids, reason="file_uploaded")

    async def _send_file_fallback(self, session_id: str, component: File) -> SendReceipt:
        """
        未验证兼容尝试: 以 file 段普通消息发送, 回执保持未确认语义

        参数:
        - session_id: 平台会话 ID
        - component: 文件组件

        返回:
        - SendReceipt: 普通发送被平台明确拒绝时为 failed, 其余保留传输层确认信息但标为 unknown,
          该路径不构成文件送达的证据
        """
        self._warn_limited(
            "file_fallback",
            "[OneBotAdapter] 当前实现缺少文件上传接口, 尝试以 file 段普通消息发送 (兼容性未验证, 不构成文件送达证据)",
            interval=600,
        )
        receipt = await self._send_chunk(session_id, MessageChain([component]))
        note = f"file_delivery_unconfirmed:{receipt.reason}" if receipt.reason else "file_delivery_unconfirmed"
        # 平台明确拒绝该消息动作时保留失败语义, 其余一律不是文件送达的证据
        status: Literal["failed", "unknown"] = "failed" if receipt.status == "failed" else "unknown"
        return SendReceipt(status, receipt.message_ids, receipt.failed_index, reason=note)

    def _failed_receipt(self, session_id: str, action: str, reason: str, error: BaseException | None = None, *, status: Literal["failed", "unknown"] = "failed") -> SendReceipt:
        """
        构造失败回执并记录 warning, 让发送失败对运维可见

        参数:
        - session_id: 目标会话
        - action: 发送类型
        - reason: 回执原因
        - error: 触发异常, 只记类型名
        - status: 回执状态
        """
        detail = f" {type(error).__name__}" if error is not None else ""
        logger.warning(f"[OneBotAdapter] 发送失败 action={action} session={session_id} reason={reason}{detail}")
        return SendReceipt(status, reason=reason)

    async def _send_forward_degraded(self, session_id: str, nodes: list[Node], limit: int) -> SendReceipt:
        """
        将转发节点展开为普通分段发送, 嵌套转发保留占位文本

        参数:
        - session_id: 平台会话 ID
        - nodes: 待展开的转发节点
        - limit: 每块文本字符上限

        返回:
        - SendReceipt: 首次失败即停止的聚合结果
        """
        components = flatten_forward_nodes(nodes)
        if not components:
            return SendReceipt("failed", reason="empty_message")
        receipts: list[SendReceipt] = []
        for chunk in split_components(components, limit):
            result = await self._send_chunk(session_id, MessageChain(chunk))
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
            self._warn_limited("client_unavailable", "[OneBotAdapter] 客户端未初始化, 无法发送消息")
            return SendReceipt("failed", reason="client_unavailable")

        if is_group_session(session_id) and not self.allows_group(extract_group_id(session_id)):
            raise PermissionError("目标群不在当前适配器允许范围内")

        segments = await message_chain_to_onebot_segments(message.components)
        if not segments:
            logger.warning("[OneBotAdapter] 消息为空, 跳过发送")
            return SendReceipt("failed", reason="empty_message")

        try:
            result = await self._dispatch_action(session_id, "send_private_msg", "send_group_msg", message=segments)
        except ValueError:
            return self._failed_receipt(session_id, "message", "invalid_session")
        except _action_failures as error:
            return self._failed_receipt(session_id, "message", "action_rejected", error)
        except Exception as error:
            return self._failed_receipt(session_id, "message", "action_unconfirmed", error, status="unknown")
        return self._receipt_from_result(result)

    async def _dispatch_action(self, session_id: str, private_action: str, group_action: str, **params: Any) -> Any:
        """
        按会话类型选择私聊或群动作并附上目标 ID

        参数:
        - session_id: 平台会话 ID
        - private_action: 私聊动作名
        - group_action: 群动作名
        - params: 动作其余参数

        返回:
        - Any: 平台原始响应; 会话格式非法时抛出 ValueError
        """
        if is_private_session(session_id):
            return await getattr(self._bot, private_action)(user_id=int(extract_private_user_id(session_id)), **params)
        if is_group_session(session_id):
            return await getattr(self._bot, group_action)(group_id=int(extract_group_id(session_id)), **params)
        raise ValueError("invalid_session")

    @staticmethod
    def _receipt_from_result(result: Any) -> SendReceipt:
        """
        从平台响应提取消息 ID

        参数:
        - result: 动作响应

        返回:
        - SendReceipt: 含 message_id 时成功, 否则结果未知; 动作已提交, 不提供自动重试依据
        """
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
            return await self._outbound.run(session_id, lambda: self._send_stream(session_id, generator, use_fallback))
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
        # 回滚复用同一实例时需要可再次发送, 关闭后的队列不可逆, 直接重建
        self._outbound = OutboundTurns()
        await super().terminate()
        self._bot = None
        self._loop = None
        self._seen_messages.clear()

    def get_client(self) -> Any:
        """
        返回底层 aiocqhttp 客户端

        返回:
        - Any: 底层 aiocqhttp 客户端
        """
        return self._bot
