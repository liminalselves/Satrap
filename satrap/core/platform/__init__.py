"""平台适配器协议, 注册表与运行时管理器"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import asyncio
import inspect
import sqlite3
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Type, TypeVar
import time
import uuid
from abc import ABC, abstractmethod

from satrap.core.framework.providers.base import SESSION_CLASS_PROVIDER
from satrap.core.config.platform_policy import validate_event_limits
from satrap.core.config.agent_routing import AgentRouteStore, resolve_agent_binding, validate_session_bindings
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.type import Group, PlatformError, PlatformStatus, PlatformMessage, safe_getattr, safe_getattr_str

from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.core.components import BaseMessageComponent
    from collections.abc import AsyncGenerator
    from satrap.core.platform.event import (
        MessageChain,
        MessageEvent,
        MessageSession,
        PlatformMetadata,
    )
    from satrap.core.pipeline.scheduler import PipelineScheduler
    from satrap.core.call_context import CallOrigin
    from satrap.core.group_chat.types import MemberSnapshot, VerifiedMember, VerifiedMessage, VerifiedGroup, GroupSnapshot


EventHandler = Callable[["PlatformEvent"], Awaitable[Any] | Any]
# 统一事件回调签名:
# - 输入: PlatformEvent
# - 输出: 可等待对象或 None (均可)


@dataclass
class PlatformConfig:
    """
    平台适配器配置
    参考 AstrBot 的适配器配置思想, 保留统一字段:
    - id: 适配器实例唯一标识
    - type: 适配器类型 (如 misskey / aiocqhttp / telegram)
    - session_provider: 入站消息使用的会话 Provider 名称
    - session_type: 入站消息使用的命名会话配置
    - session_bindings: 按适配器声明的对话类型覆盖完整 Agent 绑定
    - enable: 是否启用
    - settings: 适配器专属配置
    """

    id: str = "default"
    type: str = ""
    enable: bool = True
    settings: Dict[str, Any] = field(default_factory=dict[str, Any])
    session_provider: str = SESSION_CLASS_PROVIDER
    session_type: str = ""
    session_bindings: Dict[str, Dict[str, str]] = field(default_factory=dict)


@dataclass
class PlatformEvent:
    """
    统一平台事件模型

    所有平台原始事件应尽量归一为此结构, 便于上层统一处理
    """

    platform_id: str
    platform_type: str
    event_type: str
    session_id: str = ""
    user_id: str = ""
    group_id: str = ""
    message: str = ""
    raw_event: Any = None
    timestamp: float = field(default_factory=lambda: time.time())
    extras: Dict[str, Any] = field(default_factory=dict[str, Any])


class PlatformAdapter(ABC):
    """
    平台适配器基类
    平台实现建议:
    1. 继承此类并实现 `start/stop`
    2. 收到平台消息后构造 PlatformEvent 并调用 `emit_event`
    3. 需要发送消息时实现 `send_text`
    """

    adapter_type: str = ""
    display_name: str = ""
    conversation_catalog_fields: dict[str, str] = {}
    conversation_kinds: dict[str, str] = {"private": "私聊", "group": "群聊"}

    @classmethod
    def conversation_catalog_metadata(cls, connection: sqlite3.Connection, route: dict[str, str]) -> dict[str, str]:
        """
        从已有存储补充对话分类标签, 不创建适配器或访问平台网络

        参数:
        - connection: 平台数据库只读连接
        - route: 已解码的路由元数据, 可包含平台扩展字段

        返回:
        - 分类字段到展示标签的映射, 默认不补充标签
        """
        return {}

    def __init__(self, config: PlatformConfig, event_handler: EventHandler | None = None, event_queue: asyncio.Queue[Any] | None = None):
        """
        初始化 PlatformAdapter

        参数:
        - config: 配置信息
        - event_handler: 事件处理器
        - event_queue: 事件队列
        """
        self.config = config
        config.session_bindings = validate_session_bindings(config.session_bindings)
        self.agent_route_store: AgentRouteStore | None = None
        self.message_archive: PlatformMessageStore | None = None
        self._archive_tasks: set[asyncio.Task[None]] = set()
        self._agent_route_memory: dict[tuple[str, str, str], tuple[tuple[object, ...], int]] = {}
        self.event_handler = event_handler
        self.started = False

        self.client_self_id = uuid.uuid4().hex
        validate_event_limits(config.settings)
        capacity = int(config.settings.get("event_queue_capacity", 256))
        if capacity <= 0:
            raise ValueError("event_queue_capacity 必须大于 0")
        self._archive_capacity = min(capacity, 256)
        self._event_queue = event_queue if event_queue is not None else asyncio.Queue[Any](maxsize=capacity)
        self.dropped_events = 0
        self.expired_events = 0
        self.active_events = 0
        self.pending_events = 0
        self._status: PlatformStatus = PlatformStatus.PENDING
        self._errors: list[PlatformError] = []
        self._started_at: datetime | None = None
        self._run_task: asyncio.Task[Any] | None = None

    def set_event_handler(self, handler: EventHandler | None):
        """
        设置/替换事件回调函数

        参数:
        - handler: 事件处理函数, 输入为 PlatformEvent, 输出为可等待对象或 None
        """
        self.event_handler = handler

    def get_session_type(self) -> str:
        """
        获取入站消息应使用的会话类配置名称

        返回:
        - str: 显式绑定的会话类名称, 未绑定时兼容回退到适配器类型
        """
        return (self.config.session_type or "").strip() or self.adapter_type

    def conversation_kind(self, message: PlatformMessage) -> str:
        """
        将统一消息语义映射为对话类型, 适配器可覆盖扩展

        参数:
        - message: 已归一的平台消息

        返回:
        - private, group 或未识别的 other
        """
        value = getattr(message, "type", "")
        value = getattr(value, "value", value)
        return {"FriendMessage": "private", "GroupMessage": "group"}.get(str(value), "other")

    def agent_route_state(self, message: PlatformMessage) -> tuple[dict[str, str], str, str, str, str, int]:
        """
        读取最终绑定和当前对话的持久隔离代次

        参数:
        - message: 已归一的平台消息, 身份来自入站边界

        返回:
        - 绑定, 来源, 范围, 对话类型, 对话 ID 与代次
        """
        from satrap.core.config.group_session import session_values

        kind = self.conversation_kind(message)
        if self.config.session_bindings and kind not in self.conversation_kinds:
            logger.warning(f"[Agent 路由] 未声明的对话类型: {self.config.id}/{kind}")
            raise ValueError("适配器尚未声明此事件的对话类型")
        sender = getattr(message, "sender", None)
        actor_id = str(getattr(sender, "user_id", "") or "")
        group_id = str(getattr(message, "group_id", "") or "")
        chat_id = actor_id if kind == "private" else group_id or str(getattr(message, "session_id", "") or "")
        self_id = str(getattr(message, "self_id", "") or "")
        binding, source = resolve_agent_binding({
            "session_provider": self.get_session_provider(), "session_type": self.get_session_type(),
            "session_bindings": self.config.session_bindings,
        }, kind)
        scope = str(self.config.settings.get("context_scope", "legacy_user")) if kind == "group" else "legacy_user"
        group_generation = 0
        group_scope_override = False
        group_route = getattr(self, "group_route", None)
        if kind == "group" and group_id and callable(group_route):
            route_result = group_route(group_id)
            if (not isinstance(route_result, tuple) or len(route_result) != 2
                    or not isinstance(route_result[0], dict) or type(route_result[1]) is not int):
                logger.error(f"[Agent 路由] 群路由快照无效: {self.config.id}/{group_id}")
                raise ValueError("群路由快照无效")
            explicit, group_generation = route_result
            values = session_values(explicit)
            selected = values.get("binding")
            if isinstance(selected, dict):
                binding = {"provider": str(selected["provider"]), "config_name": str(selected["config_name"])}
                source = "group"
            if "scope" in values:
                scope = "group" if values["scope"] == "group_shared" else "group_member"
                group_scope_override = True
        signature: tuple[object, ...] = (self.config.type, binding["provider"], binding["config_name"], scope,
                                         group_generation, source, group_scope_override)
        enabled = bool(self.config.session_bindings)
        if enabled and (not self_id or not chat_id):
            logger.warning(f"[Agent 路由] 缺少对话身份: {self.config.id}/{kind}")
            raise ValueError("隔离 Agent 路由需要机器人账号与完整对话身份")
        if self.agent_route_store is not None:
            revision = self.agent_route_store.revision(self_id, kind, chat_id, signature, enabled=enabled)
        else:
            key = (self_id, kind, chat_id)
            previous = self._agent_route_memory.get(key)
            revision = 0
            if enabled or previous is not None:
                revision = previous[1] if previous and previous[0] == signature else (previous[1] + 1 if previous else 1)
                self._agent_route_memory[key] = (signature, revision)
        if revision:
            if kind == "private":
                scope = "private"
            elif scope == "legacy_user":
                scope = "group_member" if kind == "group" else "conversation"
        return binding, source, scope, kind, chat_id, revision

    def message_archive_scope(self, message: PlatformMessage) -> MessageScope:
        """
        从适配器归一的真实消息取得档案身份, 不依赖 Agent 绑定

        参数:
        - message: 当前已准入的真实平台消息

        返回:
        - 包含平台实例, 账号, 对话类型和对话 ID 的档案身份
        """
        kind = self.conversation_kind(message)
        if kind not in self.conversation_kinds:
            raise ValueError("消息档案需要适配器声明的对话类型")
        chat_id = message.sender.user_id if kind == "private" else message.group_id or message.session_id
        if kind == "private" and message.sender.user_id == message.self_id:
            raise ValueError("自身私聊回显缺少已核验的接收方身份")
        return MessageScope(self.config.id, message.self_id, kind, chat_id)

    async def archive_message(self, message: PlatformMessage, *, direction: str = "inbound",
                              scope: MessageScope | None = None) -> bool:
        """
        在唤醒模型之前采集准入消息, 存储失败只记录日志

        参数:
        - message: 已由适配器核验的真实平台消息
        - direction: inbound 入站或 outbound 已确认出站
        - scope: 适配器为自身私聊回显等情况核验的实际对话身份

        返回:
        - 新消息成功入档时返回 True, 未装配存储或失败时返回 False
        """
        store = self.message_archive
        if store is None:
            return False
        try:
            from satrap.core.platform.message_archive import archive_snapshot

            identity = scope or self.message_archive_scope(message)
            if identity.self_id != message.self_id or identity.adapter_id != self.config.id:
                raise ValueError("消息档案身份与原始消息不一致")
            snapshot = archive_snapshot(message, direction=direction)
            label = message.group.group_name or "" if message.group else ""
            return await asyncio.to_thread(store.record, identity, snapshot, label=label)
        except Exception as exc:
            logger.error(f"[消息档案] 采集失败, 平台={self.config.id}, 原因={type(exc).__name__}: {exc}")
            return False

    def queue_confirmed_message(self, scope: MessageScope, snapshot: ArchiveMessage) -> bool:
        """
        将已确认的实际发送快照交给有界写入任务, 不改变发送回执

        参数:
        - scope: 提交平台动作时冻结的账号和目标身份
        - snapshot: 平台确认消息 ID 后生成的实际内容, 不含待发送草稿

        返回:
        - 已排入写入任务时返回 True, 未配置档案或拒绝采集时返回 False
        """
        store = self.message_archive
        if store is None:
            return False
        try:
            if scope.adapter_id != self.config.id or snapshot.sender_id != scope.self_id:
                raise ValueError("已确认发送的消息身份与档案范围不一致")
            if snapshot.direction != "outbound" or not snapshot.verified or not snapshot.message_id:
                raise ValueError("出站档案需要平台已确认的实际消息")
            if len(self._archive_tasks) >= self._archive_capacity:
                raise ValueError("已确认发送的档案写入队列已满")
            task = asyncio.create_task(self._write_confirmed_message(store, scope, snapshot))
            self._archive_tasks.add(task)
            task.add_done_callback(self._archive_tasks.discard)
            return True
        except Exception as exc:
            logger.error(f"[消息档案] 出站采集未入队, 平台={self.config.id}, 原因={type(exc).__name__}: {exc}")
            return False

    async def _write_confirmed_message(self, store: PlatformMessageStore, scope: MessageScope,
                                       snapshot: ArchiveMessage) -> None:
        """
        在发送任务之外写入确认消息, 保留提交时的存储和身份

        参数:
        - store: 入队时所属平台的档案存储
        - scope: 已冻结的真实发送范围
        - snapshot: 已确认消息快照
        """
        try:
            await asyncio.to_thread(store.record, scope, snapshot)
        except asyncio.CancelledError:
            logger.warning(f"[消息档案] 出站写入等待被取消, 平台={scope.adapter_id}, 消息={snapshot.message_id}")
            raise
        except Exception as exc:
            logger.error(f"[消息档案] 出站写入失败, 平台={scope.adapter_id}, 原因={type(exc).__name__}: {exc}")

    async def drain_message_archive(self, timeout: float = 5.0) -> None:
        """
        停止时有限等待已确认消息写入, 超时任务仍自行完成或记录失败

        参数:
        - timeout: 最多等待的秒数
        """
        if self._archive_tasks:
            _, pending = await asyncio.wait(tuple(self._archive_tasks), timeout=timeout)
            if pending:
                logger.warning(f"[消息档案] 停止等待超时, 平台={self.config.id}, 待写入={len(pending)}")

    async def archive_recall(self, scope: MessageScope, message_id: str) -> None:
        """
        标记已核验的撤回事件, 存储失败不打断平台事件接收

        参数:
        - scope: 适配器确认的撤回所属对话
        - message_id: 被撤回的平台消息 ID
        """
        if self.message_archive is None:
            return
        try:
            await asyncio.to_thread(self.message_archive.recall, scope, message_id)
        except Exception as exc:
            logger.error(f"[消息档案] 撤回标记失败, 平台={self.config.id}, 原因={type(exc).__name__}: {exc}")

    async def group_chat_scope(self, origin: CallOrigin) -> MessageScope:
        """
        为当前轮次核验群聊身份及路由代次, 不接受模型提供目标群

        参数:
        - origin: 宿主冻结的入站来源

        返回:
        - 当前仍可访问的群档案身份, 失效或非群来源抛出 GroupChatError
        """
        from satrap.core.group_chat.types import GroupChatError

        if (not self.config.enable or origin.adapter_id != self.config.id or not origin.self_id
                or origin.self_id != self.client_self_id):
            raise GroupChatError("stale_call", "来源平台或机器人账号已经失效")
        kind = origin.conversation_kind or {"GroupMessage": "group", "FriendMessage": "private"}.get(origin.chat_type, "")
        if kind != "group" or kind not in self.conversation_kinds:
            raise GroupChatError("wrong_conversation", "群聊工具只能在适配器声明的群聊中使用")
        try:
            scope = MessageScope(self.config.id, origin.self_id, kind, origin.conversation_id or origin.chat_id)
        except ValueError as exc:
            raise GroupChatError("wrong_conversation", "来源缺少有效群身份") from exc
        if scope.chat_id != origin.chat_id:
            raise GroupChatError("wrong_conversation", "归一对话身份与冻结来源群不一致")
        if origin.conversation_kind:
            if self.config.session_bindings and origin.agent_route_generation == 0:
                raise GroupChatError("stale_call", "来源轮次早于分类型 Agent 路由启用")
            if self.agent_route_store is not None:
                try:
                    revision = await asyncio.to_thread(self.agent_route_store.current_revision, scope.self_id, kind, scope.chat_id)
                except Exception as exc:
                    raise GroupChatError("unavailable", "来源 Agent 路由暂时无法核验", retryable=True) from exc
            else:
                state = self._agent_route_memory.get((scope.self_id, kind, scope.chat_id))
                revision = state[1] if state else 0
            if revision != origin.agent_route_generation:
                raise GroupChatError("stale_call", "来源 Agent 路由已经切换")
            group_route = getattr(self, "group_route", None)
            if callable(group_route):
                try:
                    route = group_route(scope.chat_id)
                except Exception as exc:
                    logger.error(f"[群聊来源] 群路由核验失败, 平台={self.config.id}, 原因={type(exc).__name__}")
                    raise GroupChatError("unavailable", "来源群会话路由暂时无法核验", retryable=True) from exc
                if not isinstance(route, tuple) or len(route) != 2 or type(route[1]) is not int:
                    raise GroupChatError("unavailable", "来源群会话路由无法核验")
                if route[1] != origin.group_route_generation:
                    raise GroupChatError("stale_call", "来源群会话路由已经切换")
        if not self.config.enable or self.client_self_id != origin.self_id:
            raise GroupChatError("stale_call", "核验期间来源平台或账号已变化")
        return scope

    def group_chat_connection_token(self) -> tuple[object, int]:
        """
        标识当前客户端和连接代次, 防止同一账号重连复用旧成员快照

        返回:
        - 客户端对象身份与适配器声明的连接代次
        """
        generation = getattr(self, "connection_generation", None)
        value = generation() if callable(generation) else 0
        return self.get_client(), value if type(value) is int else 0

    def group_chat_self_id(self) -> str:
        """
        提供当前已确认的平台账号, 不接受模型指定

        返回:
        - 当前账号 ID
        """
        return self.client_self_id

    async def group_chat_private_scope(self, origin: CallOrigin) -> MessageScope:
        """
        核验跨群查询的私聊来源, 路由变化后拒绝旧轮次

        参数:
        - origin: 宿主固定的平台来源

        返回:
        - 当前私聊身份, 非私聊或失效时抛出 GroupChatError
        """
        from satrap.core.group_chat.types import GroupChatError

        kind = origin.conversation_kind or ("private" if origin.chat_type == "FriendMessage" else "")
        if (not self.config.enable or origin.adapter_id != self.config.id or not origin.self_id
                or origin.self_id != self.group_chat_self_id() or kind != "private" or kind not in self.conversation_kinds):
            raise GroupChatError("wrong_conversation", "群列表仅在当前账号的管理者私聊中提供")
        scope = MessageScope(self.config.id, origin.self_id, kind, origin.conversation_id or origin.chat_id)
        if scope.chat_id != origin.chat_id:
            raise GroupChatError("stale_call", "私聊身份已经变化")
        if origin.conversation_kind:
            if self.config.session_bindings and not origin.agent_route_generation:
                raise GroupChatError("stale_call", "私聊 Agent 路由已经变化")
            if self.agent_route_store is not None:
                try:
                    revision = await asyncio.to_thread(self.agent_route_store.current_revision, scope.self_id, kind, scope.chat_id)
                except Exception as exc:
                    logger.error(f"[群列表来源] 私聊路由核验失败, 平台={self.config.id}, 原因={type(exc).__name__}")
                    raise GroupChatError("unavailable", "来源私聊路由暂时无法核验", retryable=True) from exc
            else:
                state = self._agent_route_memory.get((scope.self_id, kind, scope.chat_id))
                revision = state[1] if state else 0
            if revision != origin.agent_route_generation:
                raise GroupChatError("stale_call", "私聊 Agent 路由已经变化")
        return scope

    async def group_chat_groups(self, scope: MessageScope) -> GroupSnapshot:
        """
        查询当前账号所在群, 默认不支持

        参数:
        - scope: 获授权的私聊来源

        返回:
        - 带完整性声明的群列表
        """
        raise NotImplementedError("适配器未实现群列表查询")

    def group_chat_group_visible(self, group_id: str) -> bool:
        """
        检查群列表条目是否在当前平台允许的范围内, 默认拒绝

        参数:
        - group_id: 平台返回的真实群 ID

        返回:
        - 可向获授权的私聊管理者展示时为 True
        """
        return False

    async def group_chat_group(self, scope: MessageScope) -> VerifiedGroup:
        """
        查询当前群资料, 默认不支持

        参数:
        - scope: 当前群身份

        返回:
        - 已核验群资料
        """
        raise NotImplementedError("适配器未实现群资料查询")

    async def group_chat_set_nickname(self, scope: MessageScope, nickname: str) -> dict[str, Any]:
        """
        经宿主审批修改当前账号自身群昵称, 默认不支持

        参数:
        - scope: 当前群身份
        - nickname: 新群昵称, 空字符串表示清空

        返回:
        - 宿主审批或执行结果
        """
        raise NotImplementedError("适配器未实现自身群昵称修改")

    def group_chat_capabilities(self) -> dict[str, dict[str, str]]:
        """
        声明当前实例可供群聊工具使用的能力, 不执行平台探测

        返回:
        - 能力名称到 supported, unsupported, unavailable 及原因的映射
        """
        from satrap.core.group_chat.types import CAPABILITIES

        capabilities = {name: {"state": "unsupported", "reason": "adapter_not_implemented"} for name in CAPABILITIES}
        capabilities["archive_search"] = {"state": "supported" if self.message_archive is not None else "unavailable",
                                          "reason": "local_archive" if self.message_archive is not None else "archive_not_configured"}
        return capabilities

    def group_chat_media_limits(self) -> dict[str, int]:
        """返回适配器较小的媒体上限, 宿主和插件上限仍同时有效"""
        return {"max_images": 4, "max_stickers": 4, "max_attachments": 8, "max_bytes": 20 * 1024 * 1024}

    def group_chat_media_formats(self) -> tuple[str, ...]:
        """返回适配器实际可发送的图片 MIME 交集"""
        return ()

    def group_chat_native_stickers(self) -> list[dict[str, str]]:
        """提供已确认的原生表情目录, 缺少目录时返回空列表"""
        return self.group_chat_native_sticker_catalog(self.message_archive)

    @classmethod
    def group_chat_native_sticker_catalog(cls, archive: PlatformMessageStore | None) -> list[dict[str, str]]:
        """
        离线读取适配器提供的原生表情目录

        参数:
        - archive: 可信的平台消息档案

        返回:
        - 已确认目录, 默认不支持
        """
        return []

    def group_chat_native_sticker_component(self, key: str) -> BaseMessageComponent:
        """
        将受控目录键转换为原生组件

        参数:
        - key: 表情库中由适配器确认的目录键

        返回:
        - 原生消息组件, 未实现时明确拒绝
        """
        from satrap.core.group_chat.types import GroupChatError

        raise GroupChatError("unsupported", "当前平台没有原生表情发送实现")

    async def group_chat_refresh_image(self, scope: MessageScope, reference: dict[str, Any]) -> str | None:
        """
        从可信档案引用刷新图片地址, 缺少实现时明确返回不可刷新

        参数:
        - scope: 已核验的群身份
        - reference: 该群档案中的原生媒体引用

        返回:
        - 新地址或 None, 不接受模型提供的路径
        """
        return None

    async def group_chat_members(self, scope: MessageScope) -> MemberSnapshot:
        """
        读取指定可信群身份的成员快照, 默认不支持

        参数:
        - scope: 已由宿主核验的当前群身份

        返回:
        - 已核验成员快照, 未实现时抛出 GroupChatError
        """
        from satrap.core.group_chat.types import GroupChatError

        raise GroupChatError("unsupported", "当前适配器未实现成员列表读取")

    async def group_chat_member(self, scope: MessageScope, user_id: str) -> VerifiedMember:
        """
        核验当前群中的一个成员, 默认不支持

        参数:
        - scope: 已由宿主核验的当前群身份
        - user_id: 待核验的成员 ID

        返回:
        - 当前群成员资料, 未实现时抛出 GroupChatError
        """
        from satrap.core.group_chat.types import GroupChatError

        raise GroupChatError("unsupported", "当前适配器未实现成员详情读取")

    async def group_chat_message(self, scope: MessageScope, message_id: str) -> VerifiedMessage:
        """
        回源读取并核验一条当前群消息, 默认不支持

        参数:
        - scope: 已由宿主核验的当前群身份
        - message_id: 待读取的消息 ID

        返回:
        - 已核验的消息及所属对话, 未实现时抛出 GroupChatError
        """
        from satrap.core.group_chat.types import GroupChatError

        raise GroupChatError("unsupported", "当前适配器未实现单条消息回源")

    def apply_agent_routes(self) -> int:
        """
        根据当前平台配置协调已有对话路由代次

        返回:
        - 受影响的已持久对话数, 未装配持久存储时为 0
        """
        if self.agent_route_store is None:
            return 0
        return self.agent_route_store.apply_platform({
            "id": self.config.id, "type": self.config.type, "settings": self.config.settings,
            "session_provider": self.get_session_provider(), "session_type": self.get_session_type(),
            "session_bindings": self.config.session_bindings,
        })

    def get_session_provider(self) -> str:
        """
        获取入站消息应使用的会话 Provider 名称

        返回:
        - str: 显式绑定的 Provider 名称, 未绑定时使用 session_class
        """
        return (self.config.session_provider or "").strip() or SESSION_CLASS_PROVIDER

    async def emit_event(self, event: PlatformEvent):
        """
        向上层派发事件

        参数:
        - event: 要派发的事件
        """
        if self.event_handler is None:
            logger.warning(
                f"[PlatformAdapter] 未设置事件处理器，事件已丢弃: "
                f"{event.platform_type}:{event.event_type}"
            )
            return

        try:
            result = self.event_handler(event)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.error(
                f"[PlatformAdapter] 事件派发失败: "
                f"{event.platform_type}:{event.event_type}, 错误={e}",
            )

    # ---------- 状态管理 ----------

    @property
    def status(self) -> PlatformStatus:
        """
        获取当前适配器状态

        返回:
        - PlatformStatus: 当前适配器状态
        """
        return self._status

    @status.setter
    def status(self, value: PlatformStatus) -> None:
        """
        设置适配器状态

        参数:
        - value: 新状态
        """
        self._status = value

    @property
    def errors(self) -> list[PlatformError]:
        """
        获取适配器最近记录的错误列表

        返回:
        - list[PlatformError]: 适配器最近记录的错误列表
        """
        return list(self._errors)

    @property
    def last_error(self) -> PlatformError | None:
        """
        获取适配器最近记录的错误

        返回:
        - PlatformError | None: 适配器最近记录的错误
        """
        return self._errors[-1] if self._errors else None

    def record_error(self, message: str, traceback_str: str | None = None) -> None:
        """
        记录适配器错误

        参数:
        - message: 错误消息
        - traceback_str: 错误栈跟踪字符串
        """
        self._errors.append(PlatformError(message=message, traceback=traceback_str))
        self._status = PlatformStatus.ERROR
        logger.error(f"[PlatformAdapter] 记录错误: {message}")

    def clear_errors(self) -> None:
        """清除适配器最近记录的错误"""
        self._errors.clear()

    # ---------- 元数据 ----------

    @abstractmethod
    def meta(self) -> PlatformMetadata:
        """
        获取适配器元数据

        返回:
        - PlatformMetadata: 适配器元数据
        """
        ...

    # ---------- 生命周期 ----------

    @abstractmethod
    async def run(self) -> None:
        """返回一个协程, 作为平台的主循环"""
        ...

    async def start(self) -> None:
        """启动平台, 创建 run 任务并设置状态"""
        self._run_task = asyncio.create_task(self.run())
        self._run_task.add_done_callback(self._on_run_task_done)
        self.started = True
        self._status = PlatformStatus.RUNNING
        self._started_at = datetime.now()
        logger.info(
            f"[PlatformAdapter] 平台已启动: {self.config.id} "
            f"(task={id(self._run_task)})"
        )

    def _on_run_task_done(self, task: "asyncio.Task[None]") -> None:
        """
        主循环任务结束时同步状态, 让吞掉异常后正常返回的 run 也被标记为错误

        参数:
        - task: 已完成的 run 任务
        """
        if task.cancelled() or task is not self._run_task:
            return
        error = task.exception()
        if error is not None:
            self.record_error(f"[PlatformAdapter] 主循环异常退出: {type(error).__name__}: {error}")
        elif self._status is PlatformStatus.RUNNING:
            self.record_error("[PlatformAdapter] 主循环意外结束, 平台已停止接收消息")

    async def wait_ready(self, timeout: float = 5.0) -> None:
        """
        检查启动任务仍存活, 具体适配器可补充协议就绪验证

        参数:
        - timeout: 具体实现的最长就绪等待时间
        """
        await asyncio.sleep(0)
        if self._run_task is not None and self._run_task.done():
            self._run_task.result()
            raise RuntimeError("平台启动任务已退出")
        if not self.started or self._run_task is None:
            raise RuntimeError("平台启动任务未保持运行")

    async def stop(self) -> None:
        """停止平台并释放资源"""
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        self.started = False
        self._status = PlatformStatus.STOPPED
        await self.drain_message_archive()
        logger.info(f"[PlatformAdapter] 平台已停止: {self.config.id}")

    async def terminate(self) -> None:
        """终止平台 (stop + 清理错误记录)"""
        await self.stop()
        while not self._event_queue.empty():
            event = self._event_queue.get_nowait()
            cleanup = getattr(event, "cleanup_temporary_local_files", None)
            if callable(cleanup):
                cleanup()
            self._event_queue.task_done()
        self._errors.clear()

    # ---------- 事件队列 ----------

    def commit_event(self, event: MessageEvent) -> bool:
        """
        提交 MessageEvent 到事件队列

        参数:
        - event: 事件

        返回:
        - bool: 入队成功返回 True, 队列满时返回 False
        """
        event.queued_at = time.monotonic()
        try:
            self._event_queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self.dropped_events += 1
            event.cleanup_temporary_local_files()
            logger.warning(f"[PlatformAdapter] 入站队列已满, 平台={self.config.id}, 累计丢弃={self.dropped_events}")
            return False

    # ---------- 客户端访问 ----------

    def get_client(self) -> object:
        """
        获取平台客户端对象, 默认返回 None

        返回:
        - object: 平台客户端对象, 默认返回 None
        """
        return None

    async def check_connection(self) -> None:
        """发起只读平台请求并校验响应, 不支持时抛出 NotImplementedError"""
        raise NotImplementedError

    # ---------- Webhook 管理 ----------

    async def webhook_callback(self, request: Any) -> Any:
        """
        Webhook 回调处理, 默认无操作

        参数:
        - request: 请求对象

        返回:
        - Any: Webhook 回调处理, 默认无操作
        """
        return None

    def unified_webhook(self) -> bool:
        """
        是否统一 Webhook 模式, 默认 False

        返回:
        - bool: 是否统一 Webhook 模式, 默认 False
        """
        return False

    # ---------- 统计信息 ----------

    def get_stats(self) -> dict[str, Any]:
        """
        获取平台运行统计信息

        返回:
        - dict[str, Any]: 平台运行统计信息
        """
        return {
            "status": self._status.value,
            "started": self.started,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "event_queue": {
                "queued": self._event_queue.qsize(), "capacity": self._event_queue.maxsize,
                "active": self.active_events, "pending": self.pending_events,
                "dropped": self.dropped_events, "expired": self.expired_events,
            },
            "error_count": len(self._errors),
            "last_error": str(self._errors[-1]) if self._errors else None,
            "client_self_id": self.client_self_id,
            "config_id": self.config.id,
            "config_type": self.config.type,
            "session_provider": self.get_session_provider(),
            "session_type": self.get_session_type(),
            "session_bindings": self.config.session_bindings,
            "conversation_kinds": self.conversation_kinds,
            "group_chat_capabilities": self.group_chat_capabilities(),
        }

    # ---------- 消息发送 ----------

    async def send_by_session(self, session: MessageSession, message_chain: MessageChain) -> None:
        """
        通过会话对象发送消息, 无需 event 引用

        参数:
        - session: 会话对象
        - message_chain: 要发送的消息链
        """
        await self.send_message(session.session_id, message_chain)

    async def send_text(self, session_id: str, text: str) -> Any:
        """
        发送文本消息

        默认不支持, 具体平台可覆写

        参数:
        - session_id: 会话 ID
        - text: 要发送的文本消息

        返回:
        - Any: 发送文本消息
        """
        logger.error(
            f"[PlatformAdapter] {self.__class__.__name__} 未实现 send_text()"
        )

    async def send_message(
        self, session_id: str, message: MessageChain, *, request_id: str = "",
        purpose: str = "business", require_tracking: bool = False,
    ) -> Any:
        """
        发送完整的消息链, 默认实现: 提取 Plain 组件拼接文本后调用 send_text

        参数:
        - session_id: 会话 ID
        - message: 要发送的消息链
        - request_id: 可选的逻辑请求标识, 支持发送尝试记录的平台据此关联手动请求
        - purpose: business 业务输出或 error_feedback 错误反馈, 支持记录的平台据此区分送达证据
        - require_tracking: 为 True 时发送前的必要记录不可用则不发送, 避免已受理请求冒充可恢复

        返回:
        - Any: 发送完整的消息链, 默认实现: 提取 Plain 组件拼接文本后调用 send_text
        """
        parts: list[str] = []
        for c in message:
            t = safe_getattr(c, 'type')
            if t is not None and hasattr(t, 'value') and t.value.lower() == 'plain':
                parts.append(safe_getattr_str(c, 'text'))
        text = "".join(parts)
        if not text:
            text = str(message)
        return await self.send_text(session_id, text)

    async def send_stream(
        self,
        session_id: str,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> Any:
        """
        流式发送消息链

        默认实现: 迭代 generator 并逐条调用 send_message
        子类可覆写以支持真正的流式推送, 此时可借助 use_fallback 决定降级策略

        参数:
        - session_id: 会话 ID
        - generator: 消息链异步生成器
        - use_fallback: 是否使用降级策略

        返回:
        - Any: 流式发送消息链
        """
        async for chunk in generator:
            await self.send_message(session_id, chunk)

    async def send_typing(self, session_id: str) -> None:
        """
        发送"输入中"状态, 默认无操作

        参数:
        - session_id: 会话 ID
        """

    async def stop_typing(self, session_id: str) -> None:
        """
        停止"输入中"状态, 默认无操作

        参数:
        - session_id: 会话 ID
        """

    async def react(self, session_id: str, emoji: str) -> None:
        """
        对消息添加表情回应, 默认无操作

        参数:
        - session_id: 会话 ID
        - emoji: 表情
        """
        await self.send_text(session_id, emoji)

    async def fetch_quoted_message(self, message_id: str, session_id: str) -> dict[str, Any] | None:
        """
        回源一条被引用的消息, 默认不支持

        参数:
        - message_id: 平台消息 ID
        - session_id: 当前事件的平台会话 ID, 用于校验被引用消息属于同一会话

        返回:
        - dict[str, Any] | None: 含 components/message_str/sender_id/sender_nickname/time 的已核验结果, 不支持或失败时返回 None
        """
        return None

    async def fetch_forward_message(self, forward_id: str, session_id: str) -> list[Any] | None:
        """
        回源一条合并转发的节点列表, 默认不支持

        参数:
        - forward_id: 平台转发消息 ID
        - session_id: 当前事件的平台会话 ID, 用于校验转发所属会话仍在允许范围

        返回:
        - list[Any] | None: 已归一的 Node 组件列表, 不支持或失败时返回 None
        """
        return None

    def admin_capabilities(self) -> dict[str, str]:
        """
        查询平台管理能力矩阵, 默认没有任何已知管理能力

        返回:
        - dict[str, str]: 动作名到状态 (supported/unsupported/unavailable), 空表表示全部未知
        """
        return {}

    async def get_group(self, group_id: str | None = None) -> Group | None:
        """
        获取群聊信息, 默认返回 None

        参数:
        - group_id: 群组 ID

        返回:
        - Group | None: 群聊信息, 默认返回 None
        """
        return None


TAdapter = TypeVar("TAdapter", bound=PlatformAdapter)


class PlatformAdapterRegistry:
    """平台适配器注册表"""

    def __init__(self):
        """初始化 PlatformAdapterRegistry"""
        self._mapping: Dict[str, Type[PlatformAdapter]] = {}

    def register(self, adapter_type: str, adapter_cls: Type[TAdapter]):
        """
        注册适配器类型

        参数:
        - adapter_type: 适配器类型字符串
        - adapter_cls: 适配器类
        """
        key = (adapter_type or "").strip().lower()
        if not key:
            logger.error("[PlatformAdapterRegistry] 注册失败: adapter_type 不能为空")
            return
        self._mapping[key] = adapter_cls
        logger.info(f"[PlatformAdapterRegistry] 已注册平台适配器: {key} -> {adapter_cls.__name__}")

    def unregister(self, adapter_type: str):
        """
        注销适配器类型

        参数:
        - adapter_type: 适配器类型字符串
        """
        key = (adapter_type or "").strip().lower()
        self._mapping.pop(key, None)

    def get(self, adapter_type: str) -> Optional[Type[PlatformAdapter]]:
        """
        获取适配器类

        参数:
        - adapter_type: 适配器类型字符串

        返回:
        - Optional[Type[PlatformAdapter]]: 适配器类
        """
        key = (adapter_type or "").strip().lower()
        return self._mapping.get(key)

    def list_types(self) -> List[str]:
        """
        列出所有已注册适配器类型

        返回:
        - List[str]: 列出所有已注册适配器类型
        """
        return sorted(self._mapping.keys())

    def create(self, config: PlatformConfig, event_handler: EventHandler | None = None) -> Optional[PlatformAdapter]:
        """
        根据配置实例化适配器

        参数:
        - config: 平台配置
        - event_handler: 事件处理函数

        返回:
        - Optional[PlatformAdapter]: 根据配置实例化适配器
        """
        adapter_type = (config.type or "").strip().lower()
        adapter_cls = self.get(adapter_type)
        if adapter_cls is None:
            logger.error(
                f"[PlatformAdapterRegistry] 创建失败: 未注册的平台适配器类型: {config.type}"
            )
            return None
        return adapter_cls(config=config, event_handler=event_handler)


class PlatformAdapterManager:
    """
    平台适配器管理器

    管理多个平台实例的生命周期 (创建, 启动, 停止, 删除)
    """

    def __init__(self, registry: PlatformAdapterRegistry | None = None):
        """
        初始化 PlatformAdapterManager

        参数:
        - registry: 注册表实例
        """
        self.registry = registry or PlatformAdapterRegistry()
        self._adapters: Dict[str, PlatformAdapter] = {}

    def add_adapter(self, config: PlatformConfig, event_handler: EventHandler | None = None) -> Optional[PlatformAdapter]:
        """
        添加一个适配器实例 (按 config.id 唯一)

        参数:
        - config: 平台配置
        - event_handler: 事件处理函数

        返回:
        - Optional[PlatformAdapter]: 添加一个适配器实例 (按 config.id 唯一)
        """
        adapter_id = (config.id or "").strip()
        if not adapter_id:
            logger.error("[PlatformAdapterManager] 添加适配器失败: PlatformConfig.id 不能为空")
            return None
        if adapter_id in self._adapters:
            logger.error(
                f"[PlatformAdapterManager] 添加适配器失败: 适配器实例已存在: {adapter_id}"
            )
            return None

        adapter = self.registry.create(config=config, event_handler=event_handler)
        if adapter is None:
            return None
        self._adapters[adapter_id] = adapter
        return adapter

    def get_adapter(self, adapter_id: str) -> Optional[PlatformAdapter]:
        """
        获取适配器实例

        参数:
        - adapter_id: 适配器实例 id

        返回:
        - Optional[PlatformAdapter]: 适配器实例
        """
        return self._adapters.get((adapter_id or "").strip())

    def remove_adapter(self, adapter_id: str) -> Optional[PlatformAdapter]:
        """
        移除适配器实例 (仅移除, 不自动 stop)

        参数:
        - adapter_id: 适配器实例 id

        返回:
        - Optional[PlatformAdapter]: 移除适配器实例 (仅移除, 不自动 stop)
        """
        return self._adapters.pop((adapter_id or "").strip(), None)

    def list_adapters(self) -> List[str]:
        """
        列出当前适配器实例 id

        返回:
        - List[str]: 列出当前适配器实例 id
        """
        return sorted(self._adapters.keys())

    async def start_adapter(self, adapter_id: str):
        """
        启动指定适配器

        参数:
        - adapter_id: 适配器实例 id
        """
        adapter = self.get_adapter(adapter_id)
        if adapter is None:
            logger.error(
                f"[PlatformAdapterManager] 启动适配器失败: 未找到实例: {adapter_id}"
            )
            return
        if adapter.started:
            return
        await adapter.start()

    async def stop_adapter(self, adapter_id: str):
        """
        停止指定适配器

        参数:
        - adapter_id: 适配器实例 id
        """
        adapter = self.get_adapter(adapter_id)
        if adapter is None:
            return
        if not adapter.started:
            return
        await adapter.terminate()

    async def enable_adapter(self, adapter_id: str):
        """
        启用适配器 (start_adapter 的别名)

        参数:
        - adapter_id: 适配器 ID
        """
        await self.start_adapter(adapter_id)

    async def disable_adapter(self, adapter_id: str):
        """
        停用适配器 (stop_adapter 的别名)

        参数:
        - adapter_id: 适配器 ID
        """
        await self.stop_adapter(adapter_id)

    async def start_all(self):
        """启动所有启用的适配器, 单个失败不影响其余"""
        for adapter in self._adapters.values():
            if not adapter.started and adapter.config.enable:
                try:
                    await adapter.start()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    adapter.record_error(f"[PlatformAdapterManager] 启动失败: {type(error).__name__}: {error}")

    async def stop_all(self):
        """停止所有已启动适配器, 单个失败不影响其余"""
        for adapter in self._adapters.values():
            if adapter.started:
                try:
                    await adapter.terminate()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning(f"[PlatformAdapterManager] 停止 {adapter.config.id} 失败: {type(error).__name__}: {error}")


_current_adapter_manager: PlatformAdapterManager | None = None
"""进程级当前适配器管理器, 由后端装配时登记"""


def set_current_adapter_manager(manager: PlatformAdapterManager | None) -> None:
    """
    登记或清除进程级适配器管理器, 仅供后端启动/关闭调用

    参数:
    - manager: 当前管理器实例, None 表示清除
    """
    global _current_adapter_manager
    _current_adapter_manager = manager


def current_adapter_manager() -> PlatformAdapterManager | None:
    """
    读取进程级适配器管理器, 供平台工具在调用边界解析来源适配器

    返回:
    - PlatformAdapterManager | None: 未启动后端时为 None
    """
    return _current_adapter_manager


class EventDispatcher:
    """
    事件分发器

    轮询所有适配器的事件队列, 将 MessageEvent 分发给对应的处理器
    """

    def __init__(self, manager: PlatformAdapterManager, scheduler: PipelineScheduler | None = None):
        """
        初始化 EventDispatcher

        参数:
        - manager: 管理器实例
        - scheduler: 调度器
        """
        self.manager = manager
        self.scheduler = scheduler
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._changed = asyncio.Event()
        self._active = False

    async def attach_adapter(self, adapter: PlatformAdapter) -> None:
        """
        为新增或替换实例启动独立工作器

        参数:
        - adapter: 已就绪的目标适配器
        """
        if self._active and adapter.config.enable:
            if adapter.config.id in self._workers:
                raise ValueError("平台工作器已存在")
            self._workers[adapter.config.id] = asyncio.create_task(
                self._adapter_dispatch_loop(adapter), name=f"platform-dispatch-{adapter.config.id}",
            )
            self._changed.set()

    async def detach_adapter(self, platform_id: str) -> None:
        """
        取消单个实例的工作器并等待其资产清理

        参数:
        - platform_id: 目标实例 ID
        """
        worker = self._workers.pop(platform_id, None)
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self._changed.set()

    async def dispatch_loop(self) -> None:
        """监督动态实例工作器, 替换单个平台时保留其他平台任务"""
        self._active = True
        change_task: asyncio.Task[bool] | None = None
        try:
            for adapter in self.manager._adapters.values():
                await self.attach_adapter(adapter)
            while True:
                self._changed.clear()
                change_task = asyncio.create_task(self._changed.wait())
                await asyncio.wait({*self._workers.values(), change_task}, return_when=asyncio.FIRST_COMPLETED)
                change_task.cancel()
                await asyncio.gather(change_task, return_exceptions=True)
                change_task = None
                for worker in list(self._workers.values()):
                    if worker.done():
                        await worker
                        raise RuntimeError("平台工作器意外退出")
        finally:
            self._active = False
            if change_task is not None:
                change_task.cancel()
                await asyncio.gather(change_task, return_exceptions=True)
            workers = list(self._workers.values())
            self._workers.clear()
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _adapter_dispatch_loop(self, adapter: PlatformAdapter) -> None:
        """
        按来源会话保序执行, 使用有界等待区避免慢会话阻塞其他群

        参数:
        - adapter: 提供独立事件队列的平台适配器
        """
        concurrency = int(adapter.config.settings.get("event_concurrency", 8))
        capacity = int(adapter.config.settings.get("event_pending_capacity", 256))
        ttl = float(adapter.config.settings.get("event_queue_ttl", 120))
        if concurrency <= 0 or capacity <= 0 or ttl <= 0:
            raise ValueError("事件并发数, 等待容量和 TTL 必须大于 0")
        pending: list[MessageEvent] = []
        running: dict[asyncio.Task[None], MessageEvent] = {}
        receiver: asyncio.Task[MessageEvent] | None = None

        async def process(event: MessageEvent) -> None:
            """
            执行事件并隔离单个请求异常

            参数:
            - event: 已获得来源会话执行权的事件
            """
            try:
                await self._process_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(f"[EventDispatcher] 平台 {adapter.config.id} 处理失败: {type(error).__name__}")

        try:
            while True:
                # Step.1 回收已完成任务, 释放来源会话的执行位置
                for task in list(running):
                    if task.done():
                        await task
                        completed = running.pop(task)
                        completed.cleanup_temporary_local_files()
                        adapter._event_queue.task_done()
                active = {event.session_id for event in running.values()}
                remaining: list[MessageEvent] = []
                for event in pending:
                    # Step.2 排除过期请求, 只调度没有在执行的来源会话
                    if time.monotonic() - event.queued_at > ttl:
                        adapter.expired_events += 1
                        event.cleanup_temporary_local_files()
                        adapter._event_queue.task_done()
                    elif len(running) < concurrency and event.session_id not in active:
                        task = asyncio.create_task(process(event))
                        running[task] = event
                        active.add(event.session_id)
                    else:
                        remaining.append(event)
                pending = remaining
                adapter.active_events = len(running)
                adapter.pending_events = len(pending)
                if receiver is None and len(pending) < capacity:
                    receiver = asyncio.create_task(adapter._event_queue.get())
                waiting: set[asyncio.Task[Any]] = set(running)
                if receiver is not None:
                    waiting.add(receiver)
                await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if receiver is not None and receiver.done():
                    pending.append(receiver.result())
                    receiver = None
        finally:
            # Step.3 取消接收与执行任务, 清理所有尚未执行的事件资产
            if receiver is not None:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
                if not receiver.cancelled() and receiver.exception() is None:
                    pending.append(receiver.result())
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            adapter.active_events = 0
            adapter.pending_events = 0
            pending.extend(running.values())
            while not adapter._event_queue.empty():
                pending.append(adapter._event_queue.get_nowait())
            for event in pending:
                event.cleanup_temporary_local_files()
                adapter._event_queue.task_done()

    async def _process_event(self, event: MessageEvent) -> None:
        """
        处理单个 MessageEvent (委托给 PipelineScheduler)

        参数:
        - event: 要处理的事件
        """
        if self.scheduler:
            await self.scheduler.execute(event)
        else:
            logger.debug(
                f"[EventDispatcher] 未设置 PipelineScheduler，事件已忽略: "
                f"{event.unified_msg_origin}"
            )


registry = PlatformAdapterRegistry()
# 全局注册表与装饰器, 便于平台实现快速注册


def register_platform_adapter(adapter_type: str):
    """
    平台适配器注册装饰器

    参数:
    - adapter_type: adapter类型

    示例:
    ```python
    @register_platform_adapter("misskey")
    class MisskeyAdapter(PlatformAdapter):
        ...
    ```

    返回:
    - 平台适配器注册装饰器
    """

    def _decorator(cls: Type[TAdapter]) -> Type[TAdapter]:
        registry.register(adapter_type, cls)
        return cls

    return _decorator


__all__ = [
    "EventHandler",
    "PlatformConfig",
    "PlatformEvent",
    "PlatformAdapter",
    "PlatformAdapterRegistry",
    "PlatformAdapterManager",
    "EventDispatcher",
    "registry",
    "register_platform_adapter",
]
