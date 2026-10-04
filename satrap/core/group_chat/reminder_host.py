"""从宿主当前平台与 Agent 配置重建后台提醒授权, 不保存旧执行上下文"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import hashlib
import json
import asyncio
import time
import uuid
import traceback

from satrap.core.components import At, Plain
from satrap.core.config.agent_routing import resolve_agent_binding
from satrap.core.config.group_session import resolve_group_session
from satrap.core.config.platform_messages import MessageScope
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.group_chat.reminders import ReminderStore
from satrap.core.group_chat.reminder_scheduler import ReminderDelivery
from satrap.core.group_chat.types import GroupChatError
from satrap.core.platform import PlatformAdapter
from satrap.core.platform.event import MessageChain
from satrap.core.platform.scheduled import ScheduledTarget
from satrap.core.log import logger
from satrap.edictum.plugin_config import PluginConfigManager, validate_config_values
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.edictum.plugin_spec import parse_plugin_specs


@dataclass(frozen=True)
class ReminderPolicy:
    """当前路由的有效配置和授权指纹, 不保存模型或主会话对象"""

    state: str
    reason: str = ""
    revision: str = ""
    config: dict[str, Any] | None = None


class ReminderHost:
    """公共提醒宿主, 调度器和后续工具/管理入口共用当前权限解析"""

    def __init__(self, backend: Any) -> None:
        """
        绑定当前后端生命周期

        参数:
        - backend: 拥有存储布局, 平台注册表与 Provider 注册表的 BackendManager
        """
        self.backend = backend
        self._stores: dict[Path, ReminderStore] = {}

    def stores(self) -> Iterable[ReminderStore]:
        """
        发现真实平台数据库, 包括已移除平台留下的任务

        返回:
        - 不越过数据目录或跟随符号链接的任务存储
        """
        root = self.backend.storage_layout.platforms_root.resolve()
        if not root.is_dir():
            return []
        discovered = []
        for directory in root.iterdir():
            database = directory / "platform.db"
            if directory.is_symlink() or database.is_symlink() or not database.is_file() or not database.resolve().is_relative_to(root):
                continue
            path = database.resolve()
            try:
                store = self._stores.get(path)
                if store is None:
                    store = ReminderStore(path)
                    self._stores[path] = store
                discovered.append(store)
            except Exception:
                logger.error(f"[提醒宿主] 平台存储无法加载, 数据库={path}" + "\n" + traceback.format_exc())
        return discovered

    def adapter(self, scope: MessageScope) -> PlatformAdapter | None:
        """
        按完整平台实例身份取当前适配器

        参数:
        - scope: 持久任务范围

        返回:
        - 当前真实适配器, 实例已移除时为 None
        """
        manager = self.backend.adapter_manager
        return manager.get_adapter(scope.adapter_id) if manager else None

    def policy(self, reminder: dict[str, Any]) -> ReminderPolicy:
        """
        从最新群绑定, 命名配置及仍属于该路由的来源会话合成授权

        参数:
        - reminder: 持久任务或新建任务的可信元数据

        返回:
        - ready, waiting 或 paused 与相关配置指纹
        """
        scope = MessageScope(**reminder["scope"])
        adapter = self.adapter(scope)
        if adapter is None:
            return ReminderPolicy("paused", "platform_removed")
        if not adapter.config.enable:
            return ReminderPolicy("paused", "platform_disabled")
        if not adapter.supports_scheduled_group_send:
            return ReminderPolicy("paused", "scheduled_send_unsupported")
        platform = {"session_provider": adapter.get_session_provider(), "session_type": adapter.get_session_type(),
                    "session_bindings": adapter.config.session_bindings, "settings": adapter.config.settings}
        explicit: dict[str, object] = {}
        group_generation = 0
        group_route = getattr(adapter, "group_route", None)
        if callable(group_route):
            route = group_route(scope.chat_id)
            if not isinstance(route, tuple) or len(route) != 2 or not isinstance(route[0], dict) or type(route[1]) is not int:
                raise ValueError("当前平台群路由快照无效")
            explicit, group_generation = route
        effective, _ = resolve_group_session(platform, explicit)
        binding = effective.get("binding", resolve_agent_binding(platform, "group")[0])
        runtime = self.backend.get_platform_runtime(scope.adapter_id)
        if runtime is None or not isinstance(binding, dict) or binding.get("provider") != "edictum":
            return ReminderPolicy("paused", "agent_without_reminders")
        manager = runtime[0]
        provider = manager.provider_registry.get("edictum")
        if provider is None:
            return ReminderPolicy("paused", "agent_unavailable")
        provider.config_manager.reload()
        resolved = manager.provider_registry.resolve_definition(str(binding.get("config_name", "")), "edictum")
        if resolved is None or not resolved[1].enabled:
            return ReminderPolicy("paused", "agent_disabled")
        definition = resolved[1]
        effective, _ = resolve_group_session(platform, explicit, {"plugins": definition.metadata.get("plugins", [])})
        plugins = effective.get("plugins", [])
        source = reminder.get("source_agent", {})
        session_id = source.get("session_id", "")
        current_generation = (adapter.agent_route_store.current_revision(scope.self_id, "group", scope.chat_id)
                              if adapter.agent_route_store else adapter._agent_route_memory.get((scope.self_id, "group", scope.chat_id), ((), 0))[1])
        same_source = (bool(session_id) and source.get("config_name") == binding["config_name"]
                       and source.get("route_generation") == current_generation)
        session_cfg = manager.store.get(session_id) if same_source else None
        if (session_cfg is not None and session_cfg.provider_name == "edictum"
                and session_cfg.session_type_name == binding["config_name"] and "plugins" in (session_cfg.session_config or {})):
            plugins = session_cfg.session_config["plugins"]
        if not isinstance(plugins, list):
            raise ValueError("当前 Agent 的插件配置不是列表")
        spec = next((item for item in parse_plugin_specs(plugins, provider.plugin_catalog) if item.name == "group_chat"), None)
        if spec is None or not spec.enabled:
            return ReminderPolicy("paused", "group_chat_disabled")
        if not spec.capabilities.get("tools", {}).get("group_chat_create_reminder", False):
            return ReminderPolicy("paused", "reminder_capability_disabled")
        entry = provider.plugin_catalog.get("group_chat")
        if entry is None or not entry.check_environment(PluginEnvironment("platform", adapter.config.type)).allowed:
            return ReminderPolicy("paused", "group_chat_unavailable")
        config = PluginConfigManager().resolve("group_chat", entry.config_schema, spec.config)
        if same_source and session_cfg is not None:
            overrides = SessionOverrideStore(self.backend.storage_layout.platform_db(scope.adapter_id)).read(session_id, "plugins.group_chat")["overrides"]
            config.update(validate_config_values(entry.config_schema, overrides, session_override=True))
        if config.get("reminders_enabled") is not True:
            return ReminderPolicy("paused", "reminders_disabled", config=config)
        revision = hashlib.sha256(json.dumps([scope.key, binding, group_generation, current_generation, config,
                                               spec.capabilities], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        account = adapter.group_chat_self_id()
        if not account or adapter.get_client() is None or not adapter.started:
            return ReminderPolicy("waiting", "platform_offline", revision, config)
        if account != scope.self_id:
            return ReminderPolicy("paused", "account_changed", revision, config)
        if not adapter.group_chat_group_visible(scope.chat_id):
            return ReminderPolicy("paused", "group_disabled", revision, config)
        return ReminderPolicy("ready", revision=revision, config=config)

    async def resolve(self, reminder: dict[str, Any], prepare: bool) -> ReminderDelivery:
        """
        复核当前配置与成员, 到期时重建固定消息发送目标

        参数:
        - reminder: 持久任务
        - prepare: True 表示到期准备发送, False 仅做生命周期配置检查

        返回:
        - 当前暂停, 离线或可发送结论, 可发送目标每次重新生成
        """
        policy = await asyncio.to_thread(self.policy, reminder)
        config = policy.config or {}
        grace = int(config.get("reminder_catchup_seconds", 600))
        if policy.state != "ready" or not prepare:
            return ReminderDelivery("paused" if policy.state == "paused" else "waiting" if policy.state == "waiting" else "ready", policy.reason, grace)
        scope = MessageScope(**reminder["scope"])
        adapter = self.adapter(scope)
        if adapter is None:
            return ReminderDelivery("paused", "platform_removed", grace)
        connection = adapter.group_chat_connection_token()

        async def guard() -> bool:
            """
            每段网络 I/O 前复核当前连接, 配置, 成员和原截止时间

            返回:
            - 所有身份与权限仍有效时为 True
            """
            try:
                scheduler = getattr(self.backend, "_reminder_scheduler", None)
                if (not self.backend._running or scheduler is None or scheduler._stopping or scheduler._clock_unstable
                        or self.adapter(scope) is not adapter or adapter.group_chat_connection_token() != connection
                        or not reminder["due_timestamp"] <= time.time() <= reminder["due_timestamp"] + grace):
                    return False
                latest = await asyncio.to_thread(self.policy, reminder)
                if latest.state != "ready" or latest.revision != policy.revision:
                    return False
                members = list(reminder["mention_user_ids"])
                if reminder["creator_kind"] == "model":
                    members.append(reminder["creator_user_id"])
                for identity in dict.fromkeys(members):
                    verified = await adapter.group_chat_member(scope, identity)
                    if verified.scope != scope or verified.member.user_id != identity:
                        return False
                latest = await asyncio.to_thread(self.policy, reminder)
                return (latest.state == "ready" and latest.revision == policy.revision and self.adapter(scope) is adapter
                        and adapter.group_chat_connection_token() == connection and adapter.group_chat_self_id() == scope.self_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"[提醒宿主] 发送前权限复核失败, 任务={reminder['reminder_id']}" + "\n" + traceback.format_exc())
                return False

        try:
            ids = [*reminder["mention_user_ids"], *([reminder["creator_user_id"]] if reminder["creator_kind"] == "model" else [])]
            for identity in dict.fromkeys(ids):
                member = await adapter.group_chat_member(scope, identity)
                if member.scope != scope or member.member.user_id != identity:
                    return ReminderDelivery("paused", "member_unverified", grace)
        except GroupChatError as exc:
            logger.warning(f"[提醒宿主] 当前成员核验失败, 任务={reminder['reminder_id']}, 原因={exc.code}")
            return ReminderDelivery("waiting" if exc.retryable else "paused", "member_unavailable" if exc.retryable else "member_unverified", grace)
        target = ScheduledTarget(scope, connection, policy.revision, reminder["reminder_id"], "attempt_" + uuid.uuid4().hex, guard)
        chain = MessageChain([*[At(qq=identity) for identity in reminder["mention_user_ids"]], Plain(reminder["text"])])
        return ReminderDelivery("ready", grace=grace, target=target, chain=chain, send=adapter.group_chat_send_scheduled)
