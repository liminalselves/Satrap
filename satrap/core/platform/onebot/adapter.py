"""
OneBot 平台事件与消息收发适配器

负责账号绑定, 有界消息去重和入站转换,
将发送结果归一为明确回执并协调反向 WebSocket 生命周期
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from collections import OrderedDict
from dataclasses import dataclass, field, replace
import asyncio
import hashlib
import inspect
import traceback
import sqlite3
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
from satrap.core.config.platform_policy import validate_wake_policy, validate_context_scope, normalize_group_whitelist, normalize_wake_words, policy_default
from satrap.core.config.group_store import GroupConfigStore, GroupLegacyConflict, GroupRuntimeSnapshot
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.platform.onebot.outbound import OutboundTurns, flatten_forward_nodes, split_components, split_forward_turns
from satrap.core.platform.onebot.admin import ADMIN_CAPABILITIES, _CAPABILITY_ACTIONS, OneBotAdmin, is_missing_action_error
from satrap.core.platform.onebot.request_registry import RequestApprovalLedger, RequestFlagRegistry
from satrap.core.platform.onebot.self_identity import OneBotSelfIdentity
from satrap.core.platform.identity import BotIdentity
from satrap.core.platform.notices import build_onebot_notice, notice_attachment
from satrap.core.platform.receipt import SendAttemptRecorder, SendReceipt, combine_receipts
from satrap.core.platform.scheduled import ScheduledTarget, ScheduledRecorder, execute_scheduled_segments
from satrap.core.platform.connection import ConnectionProbeError
from satrap.core.utils.paths import MediaSourcePermissionError, normalize_media_source
from satrap.core.server_auth import is_loopback_host
from satrap.core.components import At, BaseMessageComponent, File, Node, Plain, Reply
from satrap.core.platform.event import MessageChain, MessageEvent, PlatformMetadata
from satrap.core.platform import EventHandler, PlatformAdapter, PlatformConfig, PlatformEvent, register_platform_adapter
from satrap.core.type import PlatformMessage, safe_getattr_callable

from satrap.core.log import logger
from satrap.core.call_context import CallOrigin
from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore
from satrap.core.platform.message_archive import archive_snapshot
from satrap.core.group_chat.types import GroupChatError, MemberSnapshot, VerifiedMember, VerifiedMessage, GroupSnapshot, VerifiedGroup


class _MissingCQHttp:
    """缺少 aiocqhttp 时的占位类型, 用于给出清晰错误"""


_action_failures: tuple[type[Exception], ...] = ()
try:
    from aiocqhttp.exceptions import ActionFailed
    from aiocqhttp import CQHttp
    _action_failures = (ActionFailed,)   # 仅平台明确拒绝属于失败, 客户端通信不可用按未确认处理
except ImportError:   # pragma: no cover - 在安装依赖后走真实分支
    CQHttp = _MissingCQHttp


_RECEIPT_TO_SEGMENT_STATUS = {"success": "sent", "partial": "partial", "failed": "failed", "unknown": "unknown"}
"""回执状态到发送尝试段状态的映射"""

_ATTEMPT_FINALIZE_TIMEOUT = 2.0
"""发送尝试收尾的有界等待秒数, 超时保持未确认而不是谎报终态"""


@dataclass
class GroupSessionApplyState:
    """记录当前群会话覆盖在活跃实例上的应用结果"""

    session_revision: int
    route_generation: int
    previous_active_revision: int | None
    observed_sessions: set[tuple[str, str]] = field(default_factory=set)
    applied_sessions: set[tuple[str, str]] = field(default_factory=set)
    failed_sessions: dict[tuple[str, str], str] = field(default_factory=dict)


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

    @staticmethod
    def normalize_user_identifier(value: str) -> str:
        """
        将 QQ 用户 ID 规范为与入站事件一致的十进制身份

        参数:
        - value: 用户填写的 QQ 号

        返回:
        - 正整数的十进制文字, 格式错误抛出 ValueError
        """
        if not isinstance(value, str) or not value.strip().isascii() or not value.strip().isdigit() or len(value.strip()) > 20 or int(value.strip()) <= 0:
            raise ValueError("此平台的用户识别号必须是有效 QQ 号")
        return str(int(value.strip()))
    display_name = "OneBot"
    supports_scheduled_group_send = True

    @classmethod
    def conversation_catalog_metadata(cls, connection: sqlite3.Connection, route: dict[str, str]) -> dict[str, str]:
        """
        使用本地群目录补充群名, 群身份包含机器人账号

        参数:
        - connection: 平台数据库只读连接
        - route: 通用目录解码的完整路由

        返回:
        - 目标群展示标签, 无群目录或无匹配记录时返回空映射
        """
        if not route.get("group_id") or not connection.execute("SELECT 1 FROM sqlite_master WHERE name='group_directory'").fetchone():
            return {}
        row = connection.execute("SELECT group_name FROM group_directory WHERE self_id=? AND group_id=?", (route.get("self_id", ""), route["group_id"])).fetchone()
        return {"target": str(row[0]) or route["group_id"]} if row else {}

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
        self._self_identity = OneBotSelfIdentity(self)
        self._client_connected = False
        self._meta_hooked = False
        self._heartbeats = 0
        self._last_heartbeat_at = 0.0
        self._send_attempt_recorder: SendAttemptRecorder | None = None
        self._group_access_store: GroupConfigStore | None = None
        self._group_access_snapshot: GroupRuntimeSnapshot | None = None
        self._group_session_apply: dict[str, GroupSessionApplyState] = {}
        self._paused_groups: set[str] = set()
        self._account_mode_restrictions: dict[str, tuple[int, frozenset[str]]] = {}
        self._account_apply_errors: dict[int, str] = {}
        self._group_access_lock = asyncio.Lock()
        self._group_sync_handler: Callable[[], Awaitable[object]] | None = None
        self.group_action_handler: Callable[[str, str, dict[str, object]], Awaitable[dict[str, Any]]] | None = None

    def set_group_access_store(self, store: GroupConfigStore) -> None:
        """
        装配平台数据库中的逐群接入配置

        参数:
        - store: 已完成结构迁移的群配置存储
        """
        self._group_access_store = store

    def set_group_sync_handler(self, handler: Callable[[], Awaitable[object]]) -> None:
        """
        装配连接确认后触发群目录同步的回调

        参数:
        - handler: 返回同步任务标识的异步回调
        """
        self._group_sync_handler = handler

    async def _ensure_group_access(self, incoming_self: str, *, force: bool = False) -> bool:
        """
        采用可信账号的旧配置并装载接入快照, 异账号保持拒绝

        参数:
        - incoming_self: 入站事件或连接确认的机器人账号
        - force: 重连时重新核验旧配置源并刷新快照

        返回:
        - 账号有效且快照可用时返回 True
        """
        store = self._group_access_store
        if not incoming_self or (self.bot_self_id and incoming_self != self.bot_self_id):
            return False
        if store is None:
            return True
        snapshot = self._group_access_snapshot
        if snapshot is not None and snapshot.self_id == incoming_self and not force:
            return True
        async with self._group_access_lock:
            snapshot = self._group_access_snapshot
            if snapshot is not None and snapshot.self_id == incoming_self and not force:
                return True
            try:
                await asyncio.to_thread(store.adopt_legacy, incoming_self, self.config.settings)
            except GroupLegacyConflict:
                self._group_access_snapshot = None
                self._group_session_apply.clear()
                self._account_mode_restrictions.clear()
                raise
            prepared = await asyncio.to_thread(store.runtime_snapshot, incoming_self)
            if snapshot is None or snapshot.self_id != incoming_self:
                self._group_session_apply.clear()
                for group_id, route in prepared.routes.items():
                    if route[0]:
                        self._group_session_apply[group_id] = GroupSessionApplyState(
                            prepared.revisions.get(group_id, 0), route[1], None,
                        )
            else:
                for group_id, route in prepared.routes.items():
                    prior_route = snapshot.routes.get(group_id, ({}, 0))
                    if (route[0] != prior_route[0]
                            or prepared.revisions.get(group_id, 0) != snapshot.revisions.get(group_id, 0)):
                        previous_state = self._group_session_apply.get(group_id)
                        previous_active = (
                            snapshot.revisions.get(group_id, 0)
                            if previous_state is None or (previous_state.applied_sessions and not previous_state.failed_sessions)
                            else previous_state.previous_active_revision
                        )
                        self._group_session_apply[group_id] = GroupSessionApplyState(
                            prepared.revisions.get(group_id, 0), route[1], previous_active,
                            observed_sessions=(set(previous_state.observed_sessions)
                                               if previous_state is not None and route[1] == prior_route[1] else set()),
                        )
            self._group_access_snapshot = prepared
            self._account_mode_restrictions = {
                token: restriction for token, restriction in self._account_mode_restrictions.items()
                if restriction[0] > prepared.account_revision
            }
            self._account_apply_errors = {
                revision: reason for revision, reason in self._account_apply_errors.items()
                if revision > prepared.account_revision
            }
            return True

    def begin_account_mode_restriction(self, self_id: str, mode: str, revision: int) -> str | None:
        """账号收紧写入前限制将失去响应资格的群"""
        snapshot = self._group_access_snapshot
        if snapshot is None or snapshot.self_id != self_id or snapshot.mode != "all" or mode != "selected":
            return None
        token = secrets.token_urlsafe(16)
        allowed = frozenset(group_id for group_id, enabled in snapshot.exceptions.items() if enabled)
        self._account_mode_restrictions[token] = (revision, allowed)
        return token

    def withdraw_account_mode_restriction(self, token: str | None) -> None:
        """只撤销本次未提交写入所新增的限制"""
        if token is not None:
            self._account_mode_restrictions.pop(token, None)

    def account_apply_status(self, self_id: str, revision: int) -> tuple[str, int | None, str | None]:
        """区分账号设置的保存修订与运行时快照修订"""
        snapshot = self._group_access_snapshot
        if snapshot is None or snapshot.self_id != self_id:
            return "pending", None, None
        active = snapshot.account_revision
        if active == revision:
            return "applied", active, None
        error = self._account_apply_errors.get(revision)
        return ("failed" if error else "pending"), active, error

    def record_account_apply_failure(self, revision: int, reason: str) -> None:
        """保存账号快照刷新失败原因, 供查询和重试展示"""
        self._account_apply_errors[revision] = reason

    def _account_group_restricted(self, group_id: str) -> bool:
        """按所有尚未确认应用的收紧请求取允许范围交集"""
        return any(group_id not in allowed for _, allowed in self._account_mode_restrictions.values())

    def group_session_apply_status(
        self, group_id: str, saved_revision: int, active_instances: dict[str, str],
    ) -> tuple[str, int | None, str | None]:
        """汇总策略快照与会话实例的应用状态"""
        snapshot = self._group_access_snapshot
        if snapshot is None or snapshot.self_id != self.bot_self_id:
            return "pending", None, None
        active_revision = snapshot.revisions.get(group_id, 0)
        if active_revision != saved_revision:
            return "pending", active_revision, None
        state = self._group_session_apply.get(group_id)
        if state is None:
            return "applied", active_revision, None
        current = {(session_id, generation) for session_id, generation in active_instances.items()}
        state.observed_sessions.intersection_update(current)
        state.applied_sessions.intersection_update(current)
        state.failed_sessions = {key: reason for key, reason in state.failed_sessions.items() if key in current}
        if not current:
            return "applied", active_revision, None
        failed = current & state.failed_sessions.keys()
        if failed:
            return "failed", state.previous_active_revision, state.failed_sessions[next(iter(failed))]
        if not current.issubset(state.applied_sessions):
            return "pending", state.previous_active_revision, None
        return "applied", active_revision, None

    def report_group_session_apply(
        self, self_id: str, group_id: str, revision: int, route_generation: int,
        session_id: str, instance_generation: str, error: str | None,
    ) -> None:
        """只接受当前账号和当前修订的安全轮次应用结果"""
        snapshot = self._group_access_snapshot
        state = self._group_session_apply.get(group_id)
        if (snapshot is None or snapshot.self_id != self_id or state is None
                or snapshot.revisions.get(group_id, 0) != revision
                or state.route_generation != route_generation
                or snapshot.routes.get(group_id, ({}, 0))[1] != route_generation):
            return
        key = (session_id, instance_generation)
        state.observed_sessions.add(key)
        if error is None:
            state.failed_sessions.pop(key, None)
            state.applied_sessions.add(key)
        else:
            state.applied_sessions.discard(key)
            state.failed_sessions[key] = error

    def observe_group_session(
        self, self_id: str, group_id: str, revision: int, route_generation: int,
        session_id: str, instance_generation: str,
    ) -> None:
        """记录可信群调用当前使用的会话实例, 支持旧版共享路由"""
        snapshot = self._group_access_snapshot
        if (snapshot is None or snapshot.self_id != self_id
                or snapshot.revisions.get(group_id, 0) != revision
                or snapshot.routes.get(group_id, ({}, 0))[1] != route_generation):
            return
        state = self._group_session_apply.get(group_id)
        if state is None:
            state = GroupSessionApplyState(revision, route_generation, None)
            self._group_session_apply[group_id] = state
        if state.session_revision == revision and state.route_generation == route_generation:
            state.observed_sessions.add((session_id, instance_generation))

    def observed_group_session_ids(
        self, group_id: str, revision: int, route_generation: int,
    ) -> tuple[tuple[str, str], ...]:
        """返回本群当前修订和路由代次的可信实例关联"""
        state = self._group_session_apply.get(group_id)
        if state is None or state.session_revision != revision or state.route_generation != route_generation:
            return ()
        return tuple(state.observed_sessions)

    def failed_group_session_ids(self, group_id: str, active_instances: dict[str, str]) -> tuple[tuple[str, str], ...]:
        """返回当前群覆盖应用失败的会话实例标识"""
        state = self._group_session_apply.get(group_id)
        if state is None:
            return ()
        return tuple(key for key in state.failed_sessions if active_instances.get(key[0]) == key[1])

    def group_route(self, group_id: str) -> tuple[dict[str, object], int]:
        """返回已应用的群会话配置和路由代次"""
        settings, generation, _ = self.group_route_revision(group_id)
        return settings, generation

    def group_route_revision(self, group_id: str) -> tuple[dict[str, object], int, int | None]:
        """从单一快照取得群会话配置, 路由代次和修订号"""
        if group_id in self._paused_groups or self._account_group_restricted(group_id):
            return {}, -1, None
        if self._group_access_store is None:
            return {}, 0, None
        snapshot = self._group_access_snapshot
        if snapshot is None or snapshot.self_id != self.bot_self_id:
            return {}, -1, None
        settings, generation = snapshot.routes.get(group_id, ({}, 0))
        return settings, generation, snapshot.revisions.get(group_id, 0)

    def allows_management_target(self, group_id: str) -> bool:
        """校验已确认成员关系, 不借用聊天响应启停判断管理资格"""
        if group_id in self._paused_groups:
            return False
        if self._group_access_store is None:
            return self.allows_group(group_id)
        snapshot = self._group_access_snapshot
        return bool(snapshot is not None and self.bot_self_id == snapshot.self_id
                    and snapshot.membership.get(group_id) == "joined")

    async def refresh_management_membership(self, expected_self_id: str) -> None:
        """目录同步后刷新当前账号的管理目标成员关系快照"""
        if expected_self_id != self.bot_self_id or not isinstance(self._group_access_store, GroupDirectoryStore):
            raise ValueError("机器人账号已变化或目录存储不可用")
        async with self._group_access_lock:
            members = await asyncio.to_thread(self._group_access_store.membership_snapshot, expected_self_id)
            snapshot = self._group_access_snapshot
            if snapshot is not None and snapshot.self_id == expected_self_id:
                self._group_access_snapshot = replace(snapshot, membership=members)

    def group_active_revision(self, group_id: str) -> int | None:
        """返回当前账号已装载的群配置修订号"""
        snapshot = self._group_access_snapshot
        if snapshot is None or snapshot.self_id != self.bot_self_id:
            return None
        return snapshot.revisions.get(group_id, 0)

    def resolve_policy_settings(self, group_id: str) -> dict[str, Any]:
        """取得与入站群消息同一快照的有效策略"""
        from satrap.core.config.group_policy import resolve_group_policy
        from satrap.core.config.wake_overrides import resolve_wake_settings

        if self._group_access_store is None:
            return resolve_wake_settings(self.config.settings, group_id)
        snapshot = self._group_access_snapshot
        resolved, _ = resolve_group_policy(
            self.config.settings, group_id, snapshot.policies.get(group_id, {}) if snapshot is not None else {},
        )
        return resolved

    async def refresh_group_access(self, expected_self_id: str) -> None:
        """
        配置提交后刷新当前账号的响应快照

        参数:
        - expected_self_id: 提交请求固定的机器人账号
        """
        if expected_self_id != self.bot_self_id or self._group_access_store is None:
            raise ValueError("机器人账号已变化或群配置存储不可用")
        await self._ensure_group_access(expected_self_id, force=True)

    def pause_group_access(self, group_id: str) -> bool:
        """配置提交期间暂停目标群的新业务受理"""
        already_paused = group_id in self._paused_groups
        self._paused_groups.add(group_id)
        return already_paused

    def resume_group_access(self, group_id: str) -> None:
        """目标群配置成功刷新后恢复按新快照判定"""
        self._paused_groups.discard(group_id)

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
        if not self.access_token:
            if is_loopback_host(self.host):
                logger.warning(
                    "[OneBotAdapter] 未配置 access_token, 反向 WebSocket 端口本机任意进程可伪造事件; "
                    "建议在平台 settings 中设置 access_token"
                )
            else:
                self.record_error(
                    f"[OneBotAdapter] 非回环地址 {self.host} 监听必须配置 access_token, 已拒绝启动"
                )
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
        if not await self._ensure_group_access(incoming_self, force=meta_type == "lifecycle" and event.get("sub_type") == "connect"):
            return
        if meta_type == "lifecycle":
            sub_type = str(event.get("sub_type") or "")
            if sub_type == "connect":
                self._connection_generation += 1
                self._self_identity.clear()
                self._client_connected = True
                self._heartbeats = 0
                self._last_heartbeat_at = 0.0
                self._capability_states.clear()
                # 新代次被动重新学习, 不沿用上一连接的任何结论
                logger.info(f"[OneBotAdapter] OneBot 客户端已连接 adapter={self.config.id} self_id={incoming_self} generation={self._connection_generation}")
                if self._group_sync_handler is not None:
                    try:
                        await self._group_sync_handler()
                    except Exception as error:
                        logger.warning(f"[OneBotAdapter] 连接后群目录同步未启动: {type(error).__name__}: {error}")
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
        if not await self._ensure_group_access(str(event.get("self_id") or "")):
            self._ingress_rejections["account"] += 1
            return
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
        if group_id in self._paused_groups or self._account_group_restricted(group_id):
            return False
        if self._group_access_store is not None:
            snapshot = self._group_access_snapshot
            if snapshot is None or snapshot.self_id != self.bot_self_id:
                return False
            enabled = snapshot.exceptions.get(group_id, snapshot.mode == "all")
            return self.config.enable and bool(settings.get("enable_group", True)) and enabled
        if settings.get("group_management_version") == 1:
            return False
        groups = normalize_group_whitelist(settings.get("group_whitelist", []))
        return self.config.enable and bool(settings.get("enable_group", True)) and (not groups or group_id in groups)

    async def group_chat_scope(self, origin: CallOrigin) -> MessageScope:
        """
        核验当前群聊仍在启用范围且未确认离开

        参数:
        - origin: 宿主固定的轮次来源

        返回:
        - 当前可信群身份, 群停用或账号切换时拒绝
        """
        scope = await super().group_chat_scope(origin)
        snapshot = self._group_access_snapshot
        if (not self.allows_group(scope.chat_id)
                or (snapshot is not None and snapshot.membership.get(scope.chat_id) == "left")):
            raise GroupChatError("stale_call", "来源群已停用或机器人已离开")
        return scope

    def group_chat_media_formats(self) -> tuple[str, ...]:
        """OneBot 图片段支持的宿主已验证格式"""
        return ("image/png", "image/jpeg", "image/webp", "image/gif")

    async def group_chat_refresh_image(self, scope: MessageScope, reference: dict[str, Any]) -> str | None:
        """
        根据当前群档案的原生图片标识刷新地址

        参数:
        - scope: 已核验群身份
        - reference: 已核验档案引用

        返回:
        - 实现返回的可下载地址, 不使用实现返回的本地路径
        """
        if scope.adapter_id != self.config.id or scope.self_id != self.client_self_id:
            raise GroupChatError("stale_call", "图片来源账号已失效")
        source = reference.get("native_id") or reference.get("url")
        if not isinstance(source, str) or not source:
            return None
        return (await self.admin.get_image(source)).get("url")

    @classmethod
    def group_chat_native_sticker_catalog(cls, archive: PlatformMessageStore | None) -> list[dict[str, str]]:
        """
        只提供已采集消息里实际出现的表情, 不猜测平台完整目录

        参数:
        - archive: 可信平台档案, 可为尚未采集的空存储

        返回:
        - 有限的已观测原生表情目录
        """
        if archive is None:
            return []
        connection = archive._connect()
        if connection is None:
            return []
        try:
            rows = connection.execute(
                "SELECT DISTINCT json_extract(j.value,'$.id') FROM platform_messages m,json_each(m.components_json) j "
                "WHERE m.status='active' AND m.verified=1 AND json_extract(j.value,'$.type')='Face' AND m.message_time>=? LIMIT 200",
                (archive._clock() - archive.retention_days * 86400,),
            ).fetchall()
            return [{"key": "face:" + str(row[0]), "name": "平台表情 " + str(row[0])} for row in rows
                    if str(row[0]).isascii() and str(row[0]).isdigit() and len(str(row[0])) <= 5]
        finally:
            connection.close()

    def group_chat_native_sticker_component(self, key: str) -> BaseMessageComponent:
        """
        将已经由本适配器目录确认的表情映射为 OneBot face 段

        参数:
        - key: 表情库保存的目录键

        返回:
        - 原生表情组件, 禁止任意模型编号
        """
        from satrap.core.components import Face

        value = key.removeprefix("face:")
        if not key.startswith("face:") or not value.isascii() or not value.isdigit() or len(value) > 5:
            raise GroupChatError("asset_unavailable", "原生表情目录键无效")
        return Face(id=value)

    def friend_account(self) -> str:
        """返回协议确认的机器人账号"""
        return self.bot_self_id

    def friend_generation(self) -> object:
        """返回当前平台连接代次"""
        return self.connection_generation()

    def friend_capabilities(self) -> dict[str, dict[str, str]]:
        """返回好友接口实际能力, 扩展接口保持未知直到真实调用确认"""
        states = self.admin_capabilities()
        result = {}
        for name, action in {"list_friends": "get_friend_list", "list_requests": "handle_friend_request",
                             "handle_request": "handle_friend_request", "delete_friend": "delete_friend"}.items():
            state = states.get(action, "unknown")
            if state == "unknown" and name != "delete_friend":
                state = "supported"
            result[name] = {"state": state, "reason": {"unsupported": "当前实现不支持此接口", "unavailable": "平台未连接",
                            "unknown": "扩展接口尚未验证, 可在确认目标后尝试", "supported": "适配器已实现"}[state]}
        return result

    async def friend_list(self, account: str) -> dict[str, Any]:
        """
        获取账号好友目录

        参数:
        - account: 固定账号

        返回:
        - 目录及覆盖证据
        """
        from satrap.core.platform.onebot.friends import OneBotFriends
        return await OneBotFriends(self).list(account)

    async def friend_requests(self, account: str, limit: int, cursor: str | None) -> dict[str, Any]:
        """
        查询好友申请

        参数:
        - account: 固定账号
        - limit: 返回数量
        - cursor: 分页位置

        返回:
        - 不含原始 flag 的申请记录
        """
        return await self.request_flags.list_requests("friend", self_id=account, limit=limit, cursor=cursor)

    async def friend_handle(self, account: str, request_id: str, approve: bool, remark: str) -> None:
        """
        处理账号好友申请

        参数:
        - account: 固定账号
        - request_id: 申请 ID
        - approve: 是否同意
        - remark: 同意后的备注
        """
        from satrap.core.platform.onebot.friends import OneBotFriends
        await OneBotFriends(self).handle(account, request_id, approve, remark)

    async def friend_delete(self, account: str, user_id: str) -> None:
        """
        删除当前账号的单个好友

        参数:
        - account: 固定账号
        - user_id: 好友 ID
        """
        from satrap.core.platform.onebot.friends import OneBotFriends
        await OneBotFriends(self).delete(account, user_id)

    def group_chat_capabilities(self) -> dict[str, dict[str, str]]:
        """
        声明 OneBot 已实现的群聊能力, 按连接与被动学习结果更新状态

        返回:
        - 通用能力状态及原因, 未学习的只读动作允许调用并核验实际支持情况
        """
        capabilities = super().group_chat_capabilities()
        states = self.admin_capabilities()
        for name, action in {"member_list": "get_group_member_list", "member_info": "get_group_member_info",
                             "group_list": "get_group_list", "group_info": "get_group_info",
                             "self_nickname": "set_group_card", "message_lookup": "get_msg"}.items():
            learned = self._capability_states.get(action)
            if learned is not None and learned[0] == self.connection_generation() and learned[1] == "unsupported":
                state, reason = "unsupported", "platform_action_not_supported"
            elif states.get("get_message" if action == "get_msg" else action) == "unavailable":
                state, reason = "unavailable", "platform_disconnected"
            else:
                state, reason = "supported", "adapter_implemented"
            capabilities[name] = {"state": state, "reason": reason}
        for name in ("text", "quote", "mention", "image", "sticker"):
            disconnected = states.get("get_group_list") == "unavailable"
            capabilities[name] = {"state": "unavailable" if disconnected else "supported",
                                  "reason": "platform_disconnected" if disconnected else "adapter_implemented"}
        return capabilities

    def group_chat_self_id(self) -> str:
        """
        返回连接确认的机器人账号

        返回:
        - 当前机器人账号 ID
        """
        return self.bot_self_id

    async def group_chat_private_scope(self, origin: CallOrigin) -> MessageScope:
        """
        按平台私聊开关核验群列表查询来源

        参数:
        - origin: 宿主固定的调用来源

        返回:
        - 当前私聊身份
        """
        if not self.config.settings.get("enable_private", True):
            raise GroupChatError("wrong_conversation", "平台私聊已停用")
        return await super().group_chat_private_scope(origin)

    async def group_chat_groups(self, scope: MessageScope) -> GroupSnapshot:
        """
        读取账号群列表

        参数:
        - scope: 管理者私聊身份

        返回:
        - 群列表快照
        """
        from satrap.core.platform.onebot.group_chat import OneBotGroupChatReader
        return await OneBotGroupChatReader(self).groups(scope)

    async def group_chat_group(self, scope: MessageScope) -> VerifiedGroup:
        """
        读取当前群资料

        参数:
        - scope: 当前群身份

        返回:
        - 已核验群资料
        """
        from satrap.core.platform.onebot.group_chat import OneBotGroupChatReader
        return await OneBotGroupChatReader(self).group(scope)

    def group_chat_group_visible(self, group_id: str) -> bool:
        """
        群列表沿用平台当前允许的群范围

        参数:
        - group_id: 真实群 ID

        返回:
        - 当前群未停用且在平台范围内时为 True
        """
        return self.allows_group(group_id)

    async def group_chat_set_nickname(self, scope: MessageScope, nickname: str) -> dict[str, Any]:
        """
        固定机器人自身为目标, 交给宿主审批服务

        参数:
        - scope: 当前群身份
        - nickname: 新群昵称

        返回:
        - 审批或执行结果, 宿主未装配时拒绝执行
        """
        from satrap.core.config.group_action_origin import current_model_action_authorization
        source = current_model_action_authorization()
        if (scope.adapter_id != self.config.id or scope.self_id != self.bot_self_id
                or scope.conversation_kind != "group" or not self.allows_group(scope.chat_id) or source is None):
            raise GroupChatError("stale_call", "机器人自身修改的来源已失效")
        source.verify(scope.chat_id)
        if not callable(self.group_action_handler):
            raise GroupChatError("unavailable", "群昵称审批服务尚未装配")
        return await self.group_action_handler(scope.chat_id, "set_group_card", {"user_id": scope.self_id, "card": nickname})

    async def group_chat_members(self, scope: MessageScope) -> MemberSnapshot:
        """
        读取当前群成员及完整性状态

        参数:
        - scope: 宿主核验的当前群身份

        返回:
        - 已核验的成员快照
        """
        from satrap.core.platform.onebot.group_chat import OneBotGroupChatReader

        return await OneBotGroupChatReader(self).members(scope)

    async def group_chat_member(self, scope: MessageScope, user_id: str) -> VerifiedMember:
        """
        核验当前群内的一个成员

        参数:
        - scope: 宿主核验的当前群身份
        - user_id: 待核验的成员 ID

        返回:
        - 带当前群归属的成员资料
        """
        from satrap.core.platform.onebot.group_chat import OneBotGroupChatReader

        return await OneBotGroupChatReader(self).member(scope, user_id)

    async def group_chat_message(self, scope: MessageScope, message_id: str) -> VerifiedMessage:
        """
        回源读取并核验当前群的一条消息

        参数:
        - scope: 宿主核验的当前群身份
        - message_id: 待读取的消息 ID

        返回:
        - 已核验的原始消息快照
        """
        from satrap.core.platform.onebot.group_chat import OneBotGroupChatReader

        return await OneBotGroupChatReader(self).message(scope, message_id)

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
            if self.message_archive is not None:
                try:
                    echo = await self.convert_message(event)
                    if echo.group_id:
                        await self.archive_message(echo, direction="outbound")
                except Exception as exc:
                    logger.error(f"[消息档案] 自身回显转换失败, 平台={self.config.id}, 原因={type(exc).__name__}: {exc}")
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
            await self.archive_message(message)
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
        incoming_self = str(event.get("self_id") or "")
        if await self._ensure_group_access(incoming_self):
            notice_type = str(event.get("notice_type") or "")
            if notice_type in {"group_recall", "friend_recall"} and self.config.enable:
                from satrap.core.config.platform_messages import MessageScope

                group_id = str(event.get("group_id") or "")
                peer_id = str(event.get("user_id") or "")
                raw_id = event.get("message_id")
                message_id = str(raw_id) if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else ""
                group_allowed = notice_type == "group_recall" and self.allows_group(group_id)
                private_allowed = (notice_type == "friend_recall" and bool(self.config.settings.get("enable_private", True))
                                   and bool(peer_id) and peer_id != incoming_self)
                if message_id and (group_allowed or private_allowed):
                    scope = MessageScope(self.config.id, incoming_self, "group" if group_allowed else "private",
                                         group_id if group_allowed else peer_id)
                    await self.archive_recall(scope, message_id)
            if notice_type in {"group_increase", "group_decrease"} and str(event.get("user_id") or "") == incoming_self:
                group_id = str(event.get("group_id") or "")
                if group_id.isascii() and group_id.isdecimal() and int(group_id) > 0:
                    store = self._group_access_store
                    if isinstance(store, GroupDirectoryStore):
                        await asyncio.to_thread(
                            store.confirm_membership, incoming_self, group_id, notice_type == "group_increase",
                        )
                        async with self._group_access_lock:
                            snapshot = self._group_access_snapshot
                            if snapshot is not None and snapshot.self_id == incoming_self:
                                membership = {**snapshot.membership,
                                              group_id: "joined" if notice_type == "group_increase" else "left"}
                                self._group_access_snapshot = replace(snapshot, membership=membership)
                elif self._group_sync_handler is not None:
                    await self._group_sync_handler()
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
                    comment=str(event.get("comment") or "")[:2000],
                )
            elif request_type == "friend":
                await self.request_flags.register("friend", flag, self_id=incoming_self, user_id=user_id,
                                                  comment=str(event.get("comment") or "")[:2000])
        except Exception:
            # 登记失败的 flag 无法被审批, 保守行为是拒绝执行而不是放行
            logger.error(f"[OneBotAdapter] request 登记失败: {traceback.format_exc()}")

    async def _emit_notice(self, raw: dict[str, Any]) -> None:
        """
        按订阅类型与群范围过滤后提交通知事件

        参数:
        - raw: notice 或 request 原始事件
        """
        if not await self._ensure_group_access(str(raw.get("self_id") or "")):
            self._ingress_rejections["account"] += 1
            return
        payload = build_onebot_notice(raw, self.bot_self_id)
        if payload is None:
            self._ingress_rejections["account"] += 1
            return
        event_type = f"{payload.category}.{payload.kind}"
        enabled = self.config.settings.get("notice_types")
        if isinstance(enabled, list) and event_type not in cast(list[object], enabled) and payload.category not in cast(list[object], enabled):
            return
        if payload.group_id:
            if self._group_access_store is None and not self.allows_group(payload.group_id):
                return
            event_kind = "group_request" if payload.category == "request" and payload.kind == "group" else payload.kind
            snapshot = self._group_access_snapshot
            if self._group_access_store is not None and snapshot is not None and not snapshot.events.get(payload.group_id, {}).get(event_kind, True):
                return
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
        from satrap.core.group_chat.assets import fork_message_leases

        leases = fork_message_leases(message)
        def release_leases() -> None:
            """发送子任务实际结束后释放文件引用"""
            for lease in leases:
                lease.release()
        try:
            async def operation() -> SendReceipt:
                """在队列取得执行权后发送同一条逻辑回复"""
                return await self._send_message(
                    session_id, message, request_id=request_id, purpose=purpose, require_tracking=require_tracking,
                )

            if leases:
                return await self._outbound.run(session_id, operation, on_settle=release_leases)
            return await self._outbound.run(session_id, operation)
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
        limit = int(self.config.settings.get("message_text_limit", policy_default("message_text_limit")))
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
                    except MediaSourcePermissionError as error:
                        result = self._failed_receipt(session_id, "forward", "media_source_denied", error)
                    except PermissionError:
                        result = SendReceipt("failed", reason="target_unavailable")
                    except Exception as error:
                        result = self._failed_receipt(session_id, "forward", "message_conversion_failed", error)
                elif kind == "file":
                    try:
                        result = await self._send_file(session_id, cast(File, payload[0]))
                    except MediaSourcePermissionError as error:
                        result = self._failed_receipt(session_id, "file", "media_source_denied", error)
                    except PermissionError:
                        result = SendReceipt("failed", reason="target_unavailable")
                    except Exception as error:
                        result = self._failed_receipt(session_id, "file", "message_conversion_failed", error)
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
        if recorder is None or recorder.degraded:
            # 降级期存储拒绝一切写入, 记录保持未确认; 与发送前的记录跳过保持一致
            return
        worker = asyncio.ensure_future(
            asyncio.to_thread(recorder.complete_send_attempt, turn_id, status, detail, untracked or []),
        )
        try:
            completed = await asyncio.wait_for(asyncio.shield(worker), _ATTEMPT_FINALIZE_TIMEOUT)
        except asyncio.CancelledError:
            logger.warning(f"[OneBotAdapter] 发送收尾被取消, 记录保持未确认 turn={turn_id}")
        except asyncio.TimeoutError:
            logger.warning(f"[OneBotAdapter] 发送收尾超时, 记录保持未确认 turn={turn_id}")
        except Exception as error:
            logger.warning(f"[OneBotAdapter] 发送收尾失败 turn={turn_id}: {type(error).__name__}: {error}")
        else:
            # 只有调用正常返回 False 才是"未落盘": 异常与超时已在各自分支记录, 不重复告警
            if not completed:
                logger.warning(f"[OneBotAdapter] 发送收尾未落盘 turn={turn_id} status={status}")

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
        except MediaSourcePermissionError as error:
            # 媒体白名单拒绝与目标范围拒绝同属 PermissionError, 优先细分以保留真实原因码
            return self._failed_receipt(session_id, "message", "media_source_denied", error)
        except PermissionError:
            return SendReceipt("failed", reason="target_unavailable")
        except Exception as error:
            # 转换阶段的异常只留下固定原因码会丢掉排查线索, 复用发送失败回执记录类型
            return self._failed_receipt(session_id, "message", "message_conversion_failed", error)

    async def send_management_message(self, group_id: str, text: str) -> SendReceipt:
        """经管理服务授权后向已确认加入的群发送单条纯文本消息"""
        from satrap.core.platform.onebot.onebot_utils import group_session_id

        if not self.allows_management_target(group_id):
            return SendReceipt("failed", reason="membership_unconfirmed")
        session_id = group_session_id(group_id)
        try:
            return await self._outbound.run(
                session_id,
                lambda: self._send_chunk(session_id, MessageChain.from_text(text), management=True),
            )
        except (RuntimeError, asyncio.TimeoutError):
            return SendReceipt("failed", reason="send_queue_unavailable")

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
        sent_self_id = self.bot_self_id
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
        receipt = self._receipt_from_result(result)
        if receipt.status == "success":
            forward_id = result.get("forward_id") or result.get("res_id") or ""
            self._archive_sent_segments(session_id, sent_self_id, receipt.message_ids[0],
                                        [{"type": "forward", "data": {"id": forward_id}}])
        return receipt

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
        raw_source = component.file_ or component.url or ""
        if not raw_source:
            return SendReceipt("failed", reason="empty_file")
        try:
            source = normalize_media_source(raw_source)
        except MediaSourcePermissionError as error:
            return self._failed_receipt(session_id, "file", "media_source_denied", error)
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

    async def group_chat_send_scheduled(self, target: ScheduledTarget, chain: MessageChain, recorder: ScheduledRecorder) -> SendReceipt:
        """
        在现有出站队列中发送固定提醒, 不替换普通回复的共享记录器

        参数:
        - target: 同一平台实例, 账号, 群和连接代次的冻结目标
        - chain: 仅含文字和成员提及的固定消息
        - recorder: 本次提醒独享的发送记录器

        返回:
        - 实际回执, 必要证据未落盘时停止网络发送
        """
        scope = target.scope
        if (scope.adapter_id != self.config.id or scope.conversation_kind != "group"
                or not chain.components or any(not isinstance(item, (Plain, At)) for item in chain.components)
                or any(isinstance(item, At) and str(item.qq) == "all" for item in chain.components)):
            return SendReceipt("failed", reason="invalid_scheduled_target")
        session_id = group_session_id(scope.chat_id)

        async def operation() -> SendReceipt:
            """
            在取得队列锁后按实际文字限制构建并发送分段

            返回:
            - 实际发送汇总
            """
            limit = int(self.config.settings.get("message_text_limit", policy_default("message_text_limit")))
            steps = _plan_send_steps(split_forward_turns(chain.components), limit)
            plan = _plan_send_segments(split_forward_turns(chain.components), limit)
            senders: list[Callable[[], Awaitable[SendReceipt]]] = []
            for _, payload in steps:
                async def send_chunk(components: list[BaseMessageComponent] = payload) -> SendReceipt:
                    """
                    转换后紧邻原生网络请求复核冻结目标

                    参数:
                    - components: 当前实际分段组件

                    返回:
                    - 当前段的原生平台回执
                    """
                    return await self._send_chunk(session_id, MessageChain(components), scheduled_target=target)

                senders.append(send_chunk)
            return await execute_scheduled_segments(target, recorder, plan, senders)

        try:
            return await self._outbound.run(session_id, operation)
        except (RuntimeError, asyncio.TimeoutError):
            logger.warning(f"[提醒发送] OneBot 发送队列不可用, 任务={target.reminder_id}")
            return SendReceipt("failed", reason="send_queue_unavailable")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(f"[提醒发送] OneBot 后台发送失败, 任务={target.reminder_id}" + "\n" + traceback.format_exc())
            return SendReceipt("unknown", reason="scheduled_send_exception")

    async def _send_chunk(self, session_id: str, message: MessageChain, *, management: bool = False,
                          scheduled_target: ScheduledTarget | None = None) -> SendReceipt:
        """
        按 OneBot 会话 ID 发送完整消息链

        参数:
        - session_id: 会话 ID
        - message: 消息内容
        - management: 是否采用已授权的管理目标范围
        - scheduled_target: 提醒冻结目标, 转换后再次复核连接和策略

        返回:
        - SendReceipt: 平台确认或失败状态, 无回包不视为成功
        """
        if not self._bot:
            self._warn_limited("client_unavailable", "[OneBotAdapter] 客户端未初始化, 无法发送消息")
            return SendReceipt("failed", reason="client_unavailable")

        if is_group_session(session_id) and not (
            self.allows_management_target(extract_group_id(session_id)) if management
            else self.allows_group(extract_group_id(session_id))
        ):
            raise PermissionError("目标群不在当前适配器允许范围内")

        segments = await message_chain_to_onebot_segments(message.components)
        if not segments:
            logger.warning("[OneBotAdapter] 消息为空, 跳过发送")
            return SendReceipt("failed", reason="empty_message")

        if scheduled_target is not None:
            if (not await scheduled_target.guard() or scheduled_target.scope.self_id != self.bot_self_id
                    or scheduled_target.connection_token != self.group_chat_connection_token()
                    or not self.config.enable or not self.allows_group(scheduled_target.scope.chat_id)):
                return SendReceipt("failed", reason="scheduled_target_changed")

        sent_self_id = self.bot_self_id
        try:
            result = await self._dispatch_action(session_id, "send_private_msg", "send_group_msg", message=segments)
        except ValueError:
            return self._failed_receipt(session_id, "message", "invalid_session")
        except _action_failures as error:
            return self._failed_receipt(session_id, "message", "action_rejected", error)
        except Exception as error:
            return self._failed_receipt(session_id, "message", "action_unconfirmed", error, status="unknown")
        receipt = self._receipt_from_result(result)
        if receipt.status == "success":
            self._archive_sent_segments(session_id, sent_self_id, receipt.message_ids[0], segments)
        return receipt

    def _archive_sent_segments(self, session_id: str, self_id: str, message_id: str,
                               segments: list[dict[str, Any]]) -> None:
        """
        只采集原生消息动作确认的实际分段, 上传文件 ID 不经过此入口

        参数:
        - session_id: 实际提交的目标会话
        - self_id: 提交动作之前冻结的机器人账号
        - message_id: 平台成功回包中的消息 ID
        - segments: 实际提交的原生组件, 合并转发仅保存根引用
        """
        if self.message_archive is None:
            return
        try:
            kind = "group" if is_group_session(session_id) else "private"
            chat_id = extract_group_id(session_id) if kind == "group" else extract_private_user_id(session_id)
            scope = MessageScope(self.config.id, self_id, kind, chat_id)
            frame = {"message_type": kind, "self_id": self_id, "user_id": self_id,
                     "group_id": chat_id, "message_id": message_id, "message": segments,
                     "sender": {"user_id": self_id}}
            message = create_platform_message(frame, self_id)
            snapshot = replace(archive_snapshot(message, direction="outbound"), source="confirmed_send")
            self.queue_confirmed_message(scope, snapshot)
        except Exception as exc:
            logger.error(f"[消息档案] OneBot 发送确认转换失败, 平台={self.config.id}, 原因={type(exc).__name__}: {exc}")

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
            except MediaSourcePermissionError as error:
                receipts.append(self._failed_receipt(session_id, "message", "media_source_denied", error))
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
        self._self_identity.clear()

    async def resolve_self_identity(self, self_id: str, group_id: str = "") -> BotIdentity | None:
        """
        返回当前事件可用的机器人自身昵称和群名片

        参数:
        - self_id: 事件固定的机器人账号
        - group_id: 事件所在群, 私聊为空

        返回:
        - BotIdentity | None: 同一账号和连接下的确认资料, 查询失败时为 None
        """
        return await self._self_identity.resolve(self_id, group_id)

    async def check_connection(self) -> None:
        """通过当前 OneBot 连接读取版本信息, 断线或响应异常时抛出错误"""
        bot = self.get_client()
        account = self.bot_self_id
        generation = self._connection_generation
        if bot is None:
            raise ConnectionProbeError("OneBot 客户端尚未连接")
        method = getattr(bot, "get_version_info", None)
        if not callable(method):
            raise ConnectionProbeError("当前 OneBot 实现不支持版本信息请求")
        call = cast(Callable[..., Awaitable[object]], method)
        result = await call(self_id=account)
        if (self.get_client() is not bot or self.bot_self_id != account
                or self._connection_generation != generation):
            raise ConnectionProbeError("检查期间 OneBot 连接已变化, 请重试")
        if not isinstance(result, dict) or not isinstance(result.get("app_name"), str) or not result["app_name"]:
            raise ConnectionProbeError("OneBot 返回了无效的版本信息")

    def get_client(self) -> Any:
        """
        返回底层 aiocqhttp 客户端

        返回:
        - Any: 底层 aiocqhttp 客户端
        """
        return self._bot
