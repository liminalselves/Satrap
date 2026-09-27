"""
Satrap 后端服务组件的统一编排器

集中构建模型配置, 会话, 用户, 请求管线和平台适配器等管理组件,
负责后端的启动, 停止, 配置热重载与运行状态汇总
"""
from __future__ import annotations
from dataclasses import dataclass, field, replace
import hashlib
import asyncio
from pathlib import Path
import secrets
import signal
import time
from typing import (
    Any,
    Awaitable,
    Dict,
    List,
    Optional,
    cast,
)
from copy import deepcopy
import json
import os

from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.config.platform_policy import hot_reload_keys
from satrap.core.config.group_store import GroupConfigStore
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.config.group_actions import GroupActionStore
from satrap.core.config.group_action_origin import (
    ModelActionAuthorization, ModelActionAuthorizationError, current_model_action_authorization,
)
from satrap.core.config.group_events import GroupEventBuffer
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.core.framework.UserManager import UserManager
from satrap.core.pipeline.rate_limiter import RateLimiter
from satrap.core.platform.onebot.request_registry import RequestApprovalLedger
from satrap.core.pipeline.manual_wake_store import ManualWakeStore, ManualWakeStoreError
from satrap.core.pipeline.request_diagnostics import REJECTION_STAGES, parse_stages
from satrap.core.framework.providers import EdictumProvider, SESSION_CLASS_PROVIDER
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.edictum.registry import (
    EDICTUM_PROVIDER,
    EdictumTypeRegistry,
    create_default_edictum_type_registry,
)
from satrap.edictum.config import EdictumConfigManager
from satrap.core.pipeline.attachments import asr_resolver_from_manager
from satrap.core.platform.notices import PlatformEventHub, set_current_hub
from satrap.core.platform import (
    EventDispatcher,
    PlatformAdapter,
    PlatformAdapterManager,
    PlatformAdapterRegistry,
    PlatformConfig,
    PlatformStatus,
    registry as global_registry,
    set_current_adapter_manager,
)
from satrap.core.storage import LOCAL_PLATFORM_ID, StorageLayout, default_storage_layout
from satrap.core.type import safe_getattr, safe_getattr_bool, safe_getattr_str

from satrap.core.log import logger


@dataclass
class BackendConfig:
    """
    后端统一配置

    所有路径为 None 时使用对应 Manager 的默认路径
    """

    source_path: str | None = field(default=None, repr=False, kw_only=True)
    # 仅加载器设置的实际源文件, 不接受配置正文覆盖
    model_config_path: str | None = None
    # 存储路径
    data_root: str | None = None
    session_class_config_path: str | None = None
    edictum_config_path: str | None = None

    default_session_type: str = "default"
    # SessionManager 配置
    max_sessions: int = 1000
    idle_timeout: int = 3600
    session_checkpoint: bool = False
    """会话实例化时默认启用状态检查点 (类级/实例级显式配置优先覆盖)"""

    rate_limit: float = 1.0
    # 调度管线配置
    rate_burst: int = 5
    llm_timeout: float = 120.0
    error_feedback: bool = True

    session_classes: Dict[str, str] = field(default_factory=dict[str, str])
    # Session 类注册 (name -> class_path)
    session_scan_paths: List[str] = field(default_factory=lambda: [".satrap/session"])
    workspace_roots: List[str] = field(default_factory=lambda: ["."])
    # Chat 项目允许浏览和绑定的工作区根目录

    api_host: str = "127.0.0.1"
    # HTTP API 配置
    api_port: int = 19870

    platforms: List[Dict[str, Any]] = field(default_factory=list[Dict[str, Any]])
    # 平台适配器配置

    @staticmethod
    def _as_bool(value: Any) -> bool:
        """
        宽松布尔解析: 兼容 YAML 布尔与字符串形式的 true/false/1/0/yes/no

        参数:
        - value: 输入值

        返回:
        - bool: 宽松布尔解析: 兼容 YAML 布尔与字符串形式的 true/false/1/0/yes/no
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackendConfig:
        """
        从字典加载配置

        参数:
        - data: 输入数据

        返回:
        - BackendConfig: 从字典加载配置
        """
        removed_keys = {
            "session_db_path",
            "user_db_path",
            "session_checkpoint_db",
        }
        configured_removed = sorted(key for key in removed_keys if key in data)
        if configured_removed:
            raise ValueError(
                "v2 数据布局不再支持独立数据库路径: " + ", ".join(configured_removed)
            )
        return cls(
            model_config_path=data.get("model_config_path"),
            data_root=data.get("data_root"),
            session_class_config_path=data.get("session_class_config_path"),
            edictum_config_path=data.get("edictum_config_path"),
            default_session_type=data.get("default_session_type", "default"),
            max_sessions=int(data.get("max_sessions", 1000)),
            idle_timeout=int(data.get("idle_timeout", 3600)),
            session_checkpoint=cls._as_bool(data.get("session_checkpoint", False)),
            rate_limit=float(data.get("rate_limit", 1.0)),
            rate_burst=int(data.get("rate_burst", 5)),
            llm_timeout=float(data.get("llm_timeout", 120.0)),
            error_feedback=cls._as_bool(data.get("error_feedback", True)),
            session_classes=dict(data.get("session_classes", {})),
            session_scan_paths=list(data.get("session_scan_paths", [".satrap/session"])),
            workspace_roots=list(data.get("workspace_roots", ["."])),
            api_host=str(data.get("api", {}).get("host", data.get("api_host", "127.0.0.1"))),
            api_port=int(data.get("api", {}).get("port", data.get("api_port", 19870))),
            platforms=list(data.get("platforms", [])),
        )


class BackendManager:
    """
    后端统一编排层

    管理所有子组件的生命周期: 初始化 -> start -> stop
    依赖顺序:
      ModelConfigManager
        -> SessionClassConfigManager + EdictumConfigManager
          -> SessionManager -> UserManager
            -> PipelineScheduler -> RateLimiter
              -> PlatformAdapterManager -> EventDispatcher
    """

    def __init__(
        self,
        config: BackendConfig | None = None,
        edictum_type_registry: EdictumTypeRegistry | None = None,
    ):
        """
        初始化 BackendManager

        参数:
        - config: 配置信息
        - edictum_type_registry: 可选扩展类型注册表, 未提供时使用内置类型
        """
        self.config = config or BackendConfig()
        self._platform_active_configs: dict[str, dict[str, Any]] = {}
        self._platform_config_results: list[dict[str, Any]] = []
        self._platform_apply_lock = asyncio.Lock()
        self._group_sync_tasks: dict[tuple[str, str], tuple[str, asyncio.Task[None]]] = {}
        self._group_sync_lock = asyncio.Lock()
        self._group_action_stores: dict[str, GroupActionStore] = {}
        self._group_action_lock = asyncio.Lock()
        self._group_action_flags: dict[str, str] = {}
        self._group_action_authorizers: dict[str, ModelActionAuthorization] = {}
        self._group_events = GroupEventBuffer()

        self._model_cfg: ModelConfigManager | None = None
        # 子管理器 (按依赖顺序)
        self._session_cls_cfg: SessionClassConfigManager | None = None
        self._edictum_types = edictum_type_registry
        self._edictum_cfg: EdictumConfigManager | None = None
        self._session_mgr: SessionManager | None = None
        self._user_mgr: UserManager | None = None
        self._storage = StorageLayout(self.config.data_root) if self.config.data_root else default_storage_layout
        self._platform_runtimes: dict[str, tuple[SessionManager, UserManager]] = {}
        self._rate_limiter: RateLimiter | None = None
        self._scheduler: PipelineScheduler | None = None
        self._manual_wake_store: ManualWakeStore | None = None
        self._request_ledger: RequestApprovalLedger | None = None
        self._wake_accept_lock = asyncio.Lock()
        self._adapter_mgr: PlatformAdapterManager | None = None
        self._dispatcher: EventDispatcher | None = None
        self.platform_events = PlatformEventHub()

        self._http_server: BackendHTTPServer | None = None
        self._dispatch_task: asyncio.Task[Any] | None = None
        self._dispatch_state = "stopped"
        self._dispatch_last_error: str | None = None
        self._dispatch_restart_count = 0
        self._dispatch_restart_base_delay = 1.0
        self._dispatch_restart_max_delay = 30.0
        self._shutdown_event: asyncio.Event | None = None
        self._running = False
        self._runtime_id = os.environ.get("SATRAP_BACKEND_RUNTIME_ID") or secrets.token_urlsafe(24)

    # ---------- 属性访问 ----------

    @property
    def model_config_manager(self) -> ModelConfigManager | None:
        """
        获取 模型配置manager

        返回:
        - ModelConfigManager | None: 获取 模型配置manager
        """
        return self._model_cfg

    @property
    def session_class_mgr(self) -> SessionClassConfigManager | None:
        """
        获取 会话类mgr

        返回:
        - SessionClassConfigManager | None: 获取 会话类mgr
        """
        return self._session_cls_cfg

    @property
    def edictum_type_registry(self) -> EdictumTypeRegistry | None:
        """
        获取 Edictum 类型注册表

        返回:
        - EdictumTypeRegistry | None: 后端尚未初始化时返回 None
        """
        return self._edictum_types

    @property
    def edictum_config_manager(self) -> EdictumConfigManager | None:
        """
        获取 Edictum 冷配置管理器

        返回:
        - EdictumConfigManager | None: 后端尚未初始化时返回 None
        """
        return self._edictum_cfg

    @property
    def session_manager(self) -> SessionManager | None:
        """
        获取 会话manager

        返回:
        - SessionManager | None: 获取 会话manager
        """
        return self._session_mgr

    @property
    def user_manager(self) -> UserManager | None:
        """
        获取 用户manager

        返回:
        - UserManager | None: 获取 用户manager
        """
        return self._user_mgr

    @property
    def checkpoint_db_path(self) -> str:
        """
        检查点/上下文库路径 (管理 API 使用): 显式配置 > 会话库 > 默认路径

        返回:
        - str: 检查结果
        """
        return str(self._storage.platform_db(LOCAL_PLATFORM_ID))

    def platform_db_path(self, platform_id: str) -> str:
        """
        获取平台实例唯一数据库路径

        参数:
        - platform_id: 平台实例 ID

        返回:
        - str: 平台实例的 `platform.db` 路径
        """
        return str(self._storage.platform_db(platform_id))

    def _group_directory_store(self, adapter_id: str) -> GroupDirectoryStore:
        """
        为已配置的 OneBot 实例打开群目录数据库

        参数:
        - adapter_id: 平台实例 ID

        返回:
        - 与平台会话共用数据库的群目录存储
        """
        if not any(
            isinstance(item, dict) and item.get("id") == adapter_id
            and item.get("type") in {"onebot", "aiocqhttp"}
            for item in self.config.platforms
        ):
            raise ValueError("OneBot 平台不存在")
        return GroupDirectoryStore(self._storage.platform_db(adapter_id))

    async def group_accounts(self, adapter_id: str) -> dict[str, Any]:
        """
        列出当前平台的已确认账号和运行时绑定账号

        参数:
        - adapter_id: OneBot 平台实例 ID

        返回:
        - 历史账号摘要与当前可信账号
        """
        store = await asyncio.to_thread(self._group_directory_store, adapter_id)
        accounts = await asyncio.to_thread(store.list_accounts)
        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        current = str(getattr(adapter, "bot_self_id", "") or "")
        return {"items": accounts, "current_account": current, "waiting_for_account": not current}

    async def group_settings(self, adapter_id: str, self_id: str) -> dict[str, Any]:
        """
        读取当前或历史账号的群接入与审批默认设置

        参数:
        - adapter_id: OneBot 平台实例 ID
        - self_id: 固定的机器人账号

        返回:
        - 账号设置与是否为当前运行账号
        """
        store = await asyncio.to_thread(self._group_directory_store, adapter_id)
        account = await asyncio.to_thread(store.read_account, self_id)
        if account is None:
            raise LookupError("账号群配置不存在")
        from satrap.core.config.group_store import GROUP_APPROVAL_ACTIONS
        from satrap.core.platform.onebot.group_action_types import action_metadata

        counts = await asyncio.to_thread(store.approval_inheritance_counts, self_id)
        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        current = str(getattr(adapter, "bot_self_id", "") or "")
        return {**account, "current": current == self_id,
                "approval_actions": [action_metadata(action) for action in sorted(GROUP_APPROVAL_ACTIONS)],
                "approval_inheriting_counts": counts}

    async def patch_group_settings(
        self, adapter_id: str, self_id: str, expected_revision: int, mode: str,
        approval_defaults: dict[str, object],
    ) -> dict[str, Any]:
        """
        保存当前账号的接入模式和管理动作默认审批设置

        参数:
        - adapter_id: OneBot 平台实例 ID
        - self_id: 请求固定的机器人账号
        - expected_revision: 读取时的账号设置修订号
        - mode: selected 或 all
        - approval_defaults: 已由服务层校验的审批默认值

        返回:
        - 持久设置与运行时应用状态
        """
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        if not isinstance(adapter, OneBotAdapter) or adapter.bot_self_id != self_id:
            raise ValueError("机器人账号已变化")
        store = await asyncio.to_thread(self._group_directory_store, adapter_id)
        saved = await asyncio.to_thread(
            store.patch_account, self_id, expected_revision=expected_revision,
            mode=mode, approval_defaults=approval_defaults,
        )
        try:
            await adapter.refresh_group_access(self_id)
        except Exception as error:
            return {**await self.group_settings(adapter_id, self_id), **saved,
                    "apply_status": "failed", "apply_error": type(error).__name__}
        return {**await self.group_settings(adapter_id, self_id), **saved, "apply_status": "applied"}

    async def list_groups(
        self, adapter_id: str, self_id: str, *, query: str = "", membership: str = "joined",
        response: str = "all", page: int = 1, page_size: int = 25,
    ) -> dict[str, Any]:
        """
        读取固定账号的群目录快照和整体统计

        参数:
        - adapter_id: 已配置的 OneBot 实例 ID
        - self_id: 页面固定的机器人账号
        - query: 群名或群号筛选
        - membership: 成员关系筛选
        - response: 有效响应筛选
        - page: 页号
        - page_size: 每页条目数

        返回:
        - 分页结果、同步状态和当前账号代次
        """
        store = await asyncio.to_thread(self._group_directory_store, adapter_id)
        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        platform = next(item for item in self.config.platforms if item.get("id") == adapter_id)
        response_gate = bool(platform.get("enable", True)) and bool(platform.get("settings", {}).get("enable_group", True))
        listing, sync = await asyncio.gather(
            asyncio.to_thread(store.list_groups, self_id, query=query, membership=membership,
                              response=response, page=page, page_size=page_size,
                              response_gate=response_gate),
            asyncio.to_thread(store.sync_status, self_id),
        )
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        current = str(getattr(adapter, "bot_self_id", "") or "")
        generation = adapter.connection_generation() if isinstance(adapter, OneBotAdapter) and current == self_id else None
        return {**listing, "account": self_id, "current_account": current,
                "account_generation": generation, "sync": sync}

    async def trigger_group_sync(self, adapter_id: str, expected_self_id: str) -> dict[str, Any]:
        """
        启动或复用当前账号的单个群目录同步任务

        参数:
        - adapter_id: OneBot 平台实例 ID
        - expected_self_id: 调用方固定的机器人账号

        返回:
        - 任务 ID 与运行状态
        """
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        if not isinstance(adapter, OneBotAdapter):
            raise ValueError("OneBot 平台不存在")
        if not expected_self_id or adapter.bot_self_id != expected_self_id:
            raise ValueError("机器人账号已变化")
        if not adapter.config.enable or adapter._bot is None or (adapter._meta_hooked and not adapter._client_connected):
            raise RuntimeError("OneBot 平台未连接")
        await adapter._ensure_group_access(expected_self_id)
        key = (adapter_id, expected_self_id)
        async with self._group_sync_lock:
            existing = self._group_sync_tasks.get(key)
            if existing is not None and not existing[1].done():
                return {"sync_id": existing[0], "status": "running", "reused": True}
            store = await asyncio.to_thread(self._group_directory_store, adapter_id)
            token = secrets.token_urlsafe(24)
            generation = adapter.connection_generation()
            await asyncio.to_thread(store.begin_sync, expected_self_id, generation, token)
            task = asyncio.create_task(
                self._run_group_sync(adapter, store, expected_self_id, generation, token),
                name=f"group-sync:{adapter_id}:{expected_self_id}",
            )
            self._group_sync_tasks[key] = (token, task)
            return {"sync_id": token, "status": "running", "reused": False}

    async def _run_group_sync(
        self, adapter: Any, store: GroupDirectoryStore, self_id: str, generation: int, token: str,
    ) -> None:
        """
        执行一次有界同步并丢弃旧实例或旧连接的迟到结果

        参数:
        - adapter: 发起同步时的 OneBot 适配器对象
        - store: 当前平台的目录存储
        - self_id: 发起同步时确认的账号
        - generation: 发起同步时的连接代次
        - token: 本次同步的随机令牌
        """
        try:
            result = await adapter.admin.fetch_group_directory()
            current = self._adapter_mgr.get_adapter(adapter.config.id) if self._adapter_mgr else None
            if current is not adapter or adapter.bot_self_id != self_id or adapter.connection_generation() != generation:
                await asyncio.to_thread(store.fail_sync, self_id, generation, token, "stale_connection")
                return
            committed = await asyncio.to_thread(
                store.finish_sync, self_id, generation, token, result["items"],
                complete=result["complete"], truncated=result["truncated"], reason=result["reason"],
            )
            if committed:
                await adapter.refresh_management_membership(self_id)
        except asyncio.CancelledError:
            await asyncio.to_thread(store.fail_sync, self_id, generation, token, "interrupted")
            raise
        except Exception as error:
            logger.warning(f"[BackendManager] 群目录同步失败 adapter={adapter.config.id} self_id={self_id}: {type(error).__name__}: {error}")
            await asyncio.to_thread(store.fail_sync, self_id, generation, token, type(error).__name__)

    async def group_sync_status(self, adapter_id: str, self_id: str, sync_id: str) -> dict[str, Any]:
        """
        查询账号内固定同步任务的状态

        参数:
        - adapter_id: OneBot 平台实例 ID
        - self_id: 固定机器人账号
        - sync_id: 启动时返回的任务 ID

        返回:
        - 完整性, 截断原因和时间; 任务不属于此账号时抛出异常
        """
        store = await asyncio.to_thread(self._group_directory_store, adapter_id)
        result = await asyncio.to_thread(store.sync_status, self_id)
        if result["sync_id"] != sync_id:
            raise LookupError("同步任务不存在")
        key = (adapter_id, self_id)
        task = self._group_sync_tasks.get(key)
        if result["status"] == "running" and (task is None or task[0] != sync_id):
            await asyncio.to_thread(store.fail_sync, self_id, result["connection_generation"], sync_id, "interrupted_restart")
            result = await asyncio.to_thread(store.sync_status, self_id)
        return result

    def _group_platform_snapshot(self, adapter_id: str) -> dict[str, Any]:
        """取得当前运行平台策略快照供群策略合并和基础版本核验"""
        active = self._platform_active_configs.get(adapter_id)
        if active is not None:
            return active
        return next(item for item in self.config.platforms if item.get("id") == adapter_id)

    def _group_base_revision(
        self, adapter_id: str, platform: dict[str, Any], session: dict[str, object],
    ) -> str:
        """绑定的命名资源或模型目录变化时使群编辑基础版本失效"""
        from satrap.core.config.group_session import session_values

        binding = session_values(session).get("binding")
        if not isinstance(binding, dict):
            binding = {"provider": platform.get("session_provider") or "session_class",
                       "config_name": platform.get("session_type") or ""}
        provider = str(binding["provider"])
        name = str(binding["config_name"])
        named: object = None
        if provider == "edictum" and self._edictum_cfg is not None:
            named = self._edictum_cfg.get_config(name)
        elif provider == "session_class" and self._session_cls_cfg is not None:
            named = self._session_cls_cfg.get_config(name)
        model_configs = self._model_cfg.list_llm_configs(mask_api_key=False) if self._model_cfg else {}
        payload = {"platform": platform, "binding": binding, "named": named,
                   "models": model_configs, "adapter_id": adapter_id}
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def group_config(self, adapter_id: str, self_id: str, group_id: str) -> dict[str, Any]:
        """返回单群显式配置, 有效策略来源与已应用修订"""
        from satrap.core.config.group_approval import effective_approval
        from satrap.core.config.group_events import EVENT_KINDS, event_values
        from satrap.core.config.group_store import GROUP_APPROVAL_ACTIONS
        from satrap.core.config.group_policy import policy_values, resolve_group_policy
        from satrap.core.config.group_session import resolve_group_session, session_values
        from satrap.core.config.wake_overrides import GROUP_KEYS
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        store = await asyncio.to_thread(self._group_directory_store, adapter_id)
        account, record, config = await asyncio.gather(
            asyncio.to_thread(store.read_account, self_id),
            asyncio.to_thread(store.group_record, self_id, group_id),
            asyncio.to_thread(store.read_group, self_id, group_id),
        )
        if account is None or record is None:
            raise LookupError("群记录不存在")
        instance_summary = await asyncio.to_thread(
            store.scoped_session_summary, adapter_id, self_id, group_id, config["route_generation"],
        )
        platform = self._group_platform_snapshot(adapter_id)
        raw_policy = config["explicit"].get("policy", {})
        if not isinstance(raw_policy, dict):
            raise RuntimeError("群策略数据损坏")
        values = policy_values(raw_policy)
        resolved, origins = resolve_group_policy(platform.get("settings", {}), group_id, raw_policy)
        gate = bool(platform.get("enable", True)) and bool(platform.get("settings", {}).get("enable_group", True))
        enabled = gate and bool(values.get("enabled", account["mode"] == "all"))
        source = "platform" if not gate else "group" if "enabled" in values else "account"
        effective = {key: resolved.get(key) for key in sorted(GROUP_KEYS)}
        effective["enabled"] = enabled
        origins["enabled"] = {"source": source, "source_index": None,
                              "source_label": {"platform": "平台总开关", "group": "本群", "account": "账号接入模式"}[source]}
        raw_session = config["explicit"].get("session", {})
        if not isinstance(raw_session, dict):
            raise RuntimeError("群会话配置数据损坏")
        selected = session_values(raw_session).get("binding")
        if not isinstance(selected, dict):
            selected = {"provider": platform.get("session_provider") or "session_class",
                        "config_name": platform.get("session_type") or ""}
        defaults: dict[str, object] = {}
        session_fields = ["binding", "scope"]
        runtime = self.get_platform_runtime(adapter_id)
        binding_available: bool | None = None
        if runtime is not None and selected["config_name"]:
            resolved_definition = runtime[0].provider_registry.resolve_definition(
                str(selected["config_name"]), str(selected["provider"]),
            )
            binding_available = resolved_definition is not None and resolved_definition[1].enabled
            if resolved_definition is not None:
                definition = resolved_definition[1]
                model_name = definition.params.get(definition.model_key or "model_name")
                if isinstance(model_name, str):
                    defaults["model"] = model_name
                prompt = definition.params.get("system_prompt")
                if isinstance(prompt, str):
                    defaults["prompt"] = prompt
                if definition.provider_name == "edictum":
                    defaults["plugins"] = definition.metadata.get("plugins", [])
                    session_fields.extend(["model", "prompt"])
                    type_name = str(definition.metadata.get("edictum_type") or "")
                    type_definition = self._edictum_types.get(type_name) if self._edictum_types else None
                    if type_definition and type_definition.supports_plugins and type_definition.plugin_installer:
                        session_fields.append("plugins")
        elif runtime is not None:
            binding_available = False
        session_effective, session_sources = resolve_group_session(platform, raw_session, defaults)
        model_name = session_effective.get("model")
        model_available: bool | None = None
        if isinstance(model_name, str) and model_name and self._model_cfg is not None:
            model_config = self._model_cfg.get_llm_config(name=model_name)
            model_available = model_config is not None and bool(model_config.api_key)
        raw_approval = config["explicit"].get("approval", {})
        if not isinstance(raw_approval, dict):
            raise RuntimeError("群审批配置数据损坏")
        approval_pairs = {
            action: effective_approval(action, account["approval_defaults"], raw_approval)
            for action in sorted(GROUP_APPROVAL_ACTIONS)
        }
        raw_events = config["explicit"].get("events", {})
        if not isinstance(raw_events, dict):
            raise RuntimeError("群事件配置数据损坏")
        event_overrides = event_values(raw_events)
        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        active_revision = (
            adapter.group_active_revision(group_id)
            if isinstance(adapter, OneBotAdapter) and adapter.bot_self_id == self_id else None
        )
        return {
            "account": self_id, "current_account": adapter.bot_self_id if isinstance(adapter, OneBotAdapter) else "",
            "group": record, "explicit": config["explicit"],
            "revision": config["revision"], "saved_revision": config["revision"],
            "active_revision": active_revision,
            "apply_status": "applied" if active_revision == config["revision"] else "pending",
            "route_generation": config["route_generation"],
            "session_instances": instance_summary,
            "base_revision": self._group_base_revision(adapter_id, platform, raw_session),
            "effective": {"policy": effective, "session": session_effective,
                          "approval": {action: pair[0] for action, pair in approval_pairs.items()},
                          "events": {kind: event_overrides.get(kind, True) for kind in sorted(EVENT_KINDS)}},
            "sources": {"policy": {key: origins.get(key) for key in (*sorted(GROUP_KEYS), "enabled")},
                        "session": session_sources,
                        "approval": {action: pair[1] for action, pair in approval_pairs.items()},
                        "events": {kind: "group" if kind in event_overrides else "default" for kind in sorted(EVENT_KINDS)}},
            "capabilities": {"policy_fields": ["enabled", *sorted(GROUP_KEYS)],
                             "session_fields": session_fields,
                             "binding_available": binding_available,
                             "model_reference_available": model_available,
                             "approval_actions": sorted(GROUP_APPROVAL_ACTIONS),
                             "event_kinds": sorted(EVENT_KINDS)},
        }

    async def group_binding_options(self, adapter_id: str, self_id: str) -> dict[str, Any]:
        """列出当前运行时可解析的命名会话定义供群绑定选择"""
        store = await asyncio.to_thread(self._group_directory_store, adapter_id)
        if await asyncio.to_thread(store.read_account, self_id) is None:
            raise LookupError("账号群配置不存在")
        runtime = self.get_platform_runtime(adapter_id)
        if runtime is None:
            raise RuntimeError("平台会话运行时不可用")
        names: set[tuple[str, str]] = set()
        if self._session_cls_cfg is not None:
            classes = await asyncio.to_thread(self._session_cls_cfg.list_configs)
            names.update(("session_class", name) for name in classes)
        if self._edictum_cfg is not None:
            edictum = await asyncio.to_thread(self._edictum_cfg.list_configs)
            names.update(("edictum", name) for name in edictum)
        platform = self._group_platform_snapshot(adapter_id)
        if platform.get("session_type"):
            names.add((str(platform.get("session_provider") or "session_class"), str(platform["session_type"])))
        items: list[dict[str, Any]] = []
        for provider, name in sorted(names):
            resolved = await asyncio.to_thread(runtime[0].provider_registry.resolve_definition, name, provider)
            if resolved is None:
                items.append({"provider": provider, "config_name": name, "enabled": False,
                              "available": False, "description": "命名配置已删除"})
                continue
            definition = resolved[1]
            fields = ["binding", "scope"]
            if definition.provider_name == "edictum":
                fields.extend(["model", "prompt"])
                type_name = str(definition.metadata.get("edictum_type") or "")
                type_definition = self._edictum_types.get(type_name) if self._edictum_types else None
                if type_definition and type_definition.supports_plugins and type_definition.plugin_installer:
                    fields.append("plugins")
            items.append({"provider": provider, "config_name": name, "enabled": definition.enabled,
                          "available": True, "description": definition.description,
                          "session_fields": fields})
        models = []
        if self._model_cfg is not None:
            model_configs = await asyncio.to_thread(self._model_cfg.list_llm_configs, True)
            models = sorted(model_configs)
        plugins: list[dict[str, Any]] = []
        edictum_provider = runtime[0].provider_registry.get("edictum")
        if edictum_provider is not None:
            catalog = getattr(edictum_provider, "plugin_catalog", None)
            if catalog is not None:
                plugins = catalog.list_payloads()
        return {"items": items, "account": self_id, "models": models, "plugins": plugins}

    async def patch_group_config(
        self, adapter_id: str, self_id: str, group_id: str, *, expected_revision: int,
        base_revision: str, section: str, values: dict[str, object],
    ) -> dict[str, Any]:
        """核验当前账号与基础版本后按区域保存并刷新单群运行策略"""
        from satrap.core.config.group_store import GroupConfigConflict
        from satrap.core.config.group_session import session_values
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        if not isinstance(adapter, OneBotAdapter) or adapter.bot_self_id != self_id:
            raise ValueError("机器人账号已变化")
        if section not in {"policy", "session", "approval", "events"}:
            raise ValueError("未知群配置区域")
        if section == "session":
            session = session_values(values)
            binding = session.get("binding")
            platform_for_binding = self._group_platform_snapshot(adapter_id)
            if not isinstance(binding, dict):
                binding = {"provider": platform_for_binding.get("session_provider") or "session_class",
                           "config_name": platform_for_binding.get("session_type") or ""}
            runtime = self.get_platform_runtime(adapter_id)
            if runtime is None:
                raise RuntimeError("平台会话运行时不可用")
            resolved = await asyncio.to_thread(
                runtime[0].provider_registry.resolve_definition,
                str(binding["config_name"]), str(binding["provider"]),
            )
            if resolved is None or not resolved[1].enabled:
                raise ValueError("命名会话配置不存在或已禁用")
            definition = resolved[1]
            if set(session) & {"model", "prompt", "plugins"}:
                if definition.provider_name != "edictum":
                    raise ValueError("当前 SessionClass 不支持群级模型, 提示词或插件覆盖")
            if "model" in session:
                model_name = str(session["model"])
                model = self._model_cfg.get_llm_config(name=model_name) if self._model_cfg else None
                if model is None or not model.api_key:
                    raise ValueError("模型配置不存在或缺少必要凭据")
            if "plugins" in session:
                from satrap.edictum.plugin_config import validate_config_values
                from satrap.edictum.plugin_config import PluginConfigManager
                from satrap.edictum.plugin_settings import validate_plugin_settings
                from satrap.edictum.plugin_spec import parse_plugin_specs

                type_name = str(definition.metadata.get("edictum_type") or "")
                type_definition = self._edictum_types.get(type_name) if self._edictum_types else None
                if not type_definition or not type_definition.supports_plugins or not type_definition.plugin_installer:
                    raise ValueError("当前 Edictum 类型不支持插件覆盖")
                provider = resolved[0]
                catalog = getattr(provider, "plugin_catalog", None)
                if catalog is None:
                    raise ValueError("插件目录不可用")
                plugin_values = session["plugins"]
                if not isinstance(plugin_values, list):
                    raise ValueError("插件覆盖必须是数组")
                normalized = [{"name": item["name"], "enabled": item["mode"] == "enabled",
                               "config": item.get("config", {})} for item in plugin_values]
                parse_plugin_specs(normalized, catalog, require_available=True)
                for item in normalized:
                    entry = catalog.get(item["name"])
                    if entry is None:
                        raise ValueError("插件不存在")
                    cleaned = validate_config_values(entry.config_schema, item["config"], session_override=True)
                    inherited = PluginConfigManager().resolve(item["name"], entry.config_schema, cleaned)
                    validate_plugin_settings(
                        item["name"], entry.config_schema, inherited, models=self._model_cfg,
                    )
        platform = self._group_platform_snapshot(adapter_id)
        store = await asyncio.to_thread(self._group_directory_store, adapter_id)
        if not await asyncio.to_thread(store.group_exists, self_id, group_id):
            raise LookupError("群记录不存在")
        previous = await asyncio.to_thread(store.read_group, self_id, group_id)
        previous_session = previous["explicit"].get("session", {})
        if not isinstance(previous_session, dict) or base_revision != self._group_base_revision(
            adapter_id, platform, previous_session,
        ):
            raise GroupConfigConflict("平台或命名资源配置已变化, 请刷新后重试")
        await asyncio.to_thread(
            store.patch_group, self_id, group_id, section, values,
            expected_revision=expected_revision,
        )
        try:
            await adapter.refresh_group_access(self_id)
        except Exception as error:
            result = await self.group_config(adapter_id, self_id, group_id)
            return {**result, "apply_status": "failed", "apply_error": type(error).__name__}
        return await self.group_config(adapter_id, self_id, group_id)

    async def apply_group_config(
        self, adapter_id: str, self_id: str, group_id: str, saved_revision: int,
    ) -> dict[str, Any]:
        """只重试当前保存修订的运行时应用, 不重复写入配置"""
        from satrap.core.config.group_store import GroupConfigConflict
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        if not isinstance(adapter, OneBotAdapter) or adapter.bot_self_id != self_id:
            raise ValueError("机器人账号已变化")
        current = await self.group_config(adapter_id, self_id, group_id)
        if current["saved_revision"] != saved_revision:
            raise GroupConfigConflict("群配置已变化, 请刷新后重试")
        await adapter.refresh_group_access(self_id)
        return await self.group_config(adapter_id, self_id, group_id)

    async def dry_run_group_policy(
        self, adapter_id: str, self_id: str, group_id: str, *, expected_revision: int,
        base_revision: str, values: dict[str, object], scenario: dict[str, object],
    ) -> dict[str, Any]:
        """使用已保存平台设置和未保存群草稿进行无副作用唤醒试算"""
        from satrap.core.config.group_policy import policy_values
        from satrap.core.config.group_store import GroupConfigConflict
        from satrap.core.pipeline.wake_dry_run import dry_run_wake

        current = await self.group_config(adapter_id, self_id, group_id)
        if current["revision"] != expected_revision or current["base_revision"] != base_revision:
            raise GroupConfigConflict("试算基础配置已变化, 请刷新后重试")
        override = policy_values(values)
        platform = self._group_platform_snapshot(adapter_id)
        settings = dict(platform.get("settings", {}))
        settings["wake_group_overrides"] = {
            group_id: {key: value for key, value in override.items() if key != "enabled"},
        }
        data: dict[str, object] = {"settings": settings, "group_id": group_id}
        for key in ("local_time", "probe", "steps"):
            if key in scenario:
                data[key] = scenario[key]
        result = await dry_run_wake(data)
        gate = bool(platform.get("enable", True)) and bool(settings.get("enable_group", True))
        account_store = await asyncio.to_thread(self._group_directory_store, adapter_id)
        account = await asyncio.to_thread(account_store.read_account, self_id)
        if account is None:
            raise LookupError("账号群配置不存在")
        result["response_enabled"] = gate and bool(override.get("enabled", account["mode"] == "all"))
        result["draft"] = True
        result["saved_revision"] = expected_revision
        return result

    async def _group_action_store(self, adapter_id: str) -> GroupActionStore:
        """每个平台进程只恢复一次执行中动作, 避免重复提交网络动作"""
        async with self._group_action_lock:
            existing = self._group_action_stores.get(adapter_id)
            if existing is not None:
                return existing
            self._group_directory_store(adapter_id)
            store = await asyncio.to_thread(
                GroupActionStore, self._storage.platform_db(adapter_id), recover=True,
            )
            self._group_action_stores[adapter_id] = store
            return store

    async def _group_action_context(
        self, adapter_id: str, self_id: str, group_id: str, action_type: str,
    ) -> tuple[Any, GroupActionStore, str, int]:
        """确认群目标、账号代次和有效审批模式并生成策略指纹"""
        from satrap.core.config.group_approval import effective_approval
        from satrap.core.config.group_store import GROUP_APPROVAL_ACTIONS
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        if action_type not in GROUP_APPROVAL_ACTIONS:
            raise ValueError("动作不属于逐群审批范围")
        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        if not isinstance(adapter, OneBotAdapter) or adapter.bot_self_id != self_id:
            raise ValueError("机器人账号已变化")
        if adapter.get_client() is None:
            raise RuntimeError("OneBot 平台未连接")
        if action_type != "handle_group_request" and not adapter.allows_management_target(group_id):
            raise PermissionError("机器人当前成员关系未确认或已离开目标群")
        store = await self._group_action_store(adapter_id)
        account, group = await asyncio.gather(
            asyncio.to_thread(store.read_account, self_id),
            asyncio.to_thread(store.read_group, self_id, group_id),
        )
        if account is None:
            raise LookupError("账号群配置不存在")
        raw_approval = group["explicit"].get("approval", {})
        if not isinstance(raw_approval, dict):
            raise RuntimeError("群审批配置数据损坏")
        mode, _ = effective_approval(action_type, account["approval_defaults"], raw_approval)
        version_payload = json.dumps(
            [account["revision"], group["revision"], adapter.connection_generation(),
             adapter.bot_self_id, action_type, mode],
            ensure_ascii=True, separators=(",", ":"),
        )
        version = int(hashlib.sha256(version_payload.encode("utf-8")).hexdigest()[:15], 16)
        return adapter, store, mode, version

    async def group_action_types(self, adapter_id: str, self_id: str, group_id: str) -> dict[str, Any]:
        """返回当前群的动作参数结构、风险和有效审批模式"""
        from satrap.core.config.group_approval import effective_approval
        from satrap.core.platform.onebot.adapter import OneBotAdapter
        from satrap.core.platform.onebot.group_action_types import ACTION_FIELDS, action_metadata

        store = await asyncio.to_thread(self._group_directory_store, adapter_id)
        account, group, record = await asyncio.gather(
            asyncio.to_thread(store.read_account, self_id),
            asyncio.to_thread(store.read_group, self_id, group_id),
            asyncio.to_thread(store.group_record, self_id, group_id),
        )
        if account is None or record is None:
            raise LookupError("群记录不存在")
        approval = group["explicit"].get("approval", {})
        if not isinstance(approval, dict):
            raise RuntimeError("群审批配置数据损坏")
        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        current = isinstance(adapter, OneBotAdapter) and adapter.bot_self_id == self_id
        capabilities = adapter.admin_capabilities() if isinstance(adapter, OneBotAdapter) and current else {}
        items: list[dict[str, Any]] = []
        for action in ACTION_FIELDS:
            mode, source = effective_approval(action, account["approval_defaults"], approval)
            capability = capabilities.get(action, "unavailable")
            membership = record["membership"] == "joined" or action == "handle_group_request"
            items.append({**action_metadata(action), "approval_mode": mode, "approval_source": source,
                          "membership": record["membership"], "capability": capability,
                          "available": current and membership and capability not in {"unavailable", "unsupported"}})
        return {"items": items, "account": self_id, "group_id": group_id}

    async def group_members(
        self, adapter_id: str, self_id: str, group_id: str, *,
        query: str = "", page: int = 1, page_size: int = 25,
    ) -> dict[str, Any]:
        """按需从 OneBot 读取成员, 对已取集合分页并明确截断边界"""
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        if not isinstance(adapter, OneBotAdapter) or adapter.bot_self_id != self_id:
            raise ValueError("机器人账号已变化")
        if adapter.get_client() is None:
            raise RuntimeError("OneBot 平台未连接")
        if not adapter.allows_management_target(group_id):
            raise PermissionError("机器人当前成员关系未确认或已离开目标群")
        if page < 1 or page_size not in {25, 50, 100} or len(query) > 100:
            raise ValueError("成员查询或分页参数无效")
        members = await adapter.admin.get_group_member_list(group_id)
        truncated = len(members) >= 2048
        needle = query.strip().lower()
        filtered = [item for item in members if not needle or any(
            needle in str(item.get(key) or "").lower() for key in ("user_id", "nickname", "card")
        )]
        offset = (page - 1) * page_size
        return {"items": filtered[offset:offset + page_size], "total_loaded": len(filtered),
                "page": page, "page_size": page_size, "truncated": truncated,
                "complete": not truncated, "account": self_id, "group_id": group_id}

    async def group_events(self, adapter_id: str, self_id: str, group_id: str, *, limit: int = 50) -> dict[str, Any]:
        """按当前群的显式可见设置查询内存中的脱敏近期事件"""
        from satrap.core.config.group_events import event_values

        store = await asyncio.to_thread(self._group_directory_store, adapter_id)
        if not await asyncio.to_thread(store.group_exists, self_id, group_id):
            raise LookupError("群记录不存在")
        config = await asyncio.to_thread(store.read_group, self_id, group_id)
        explicit = config["explicit"].get("events", {})
        if not isinstance(explicit, dict):
            raise RuntimeError("群事件配置数据损坏")
        return self._group_events.list(adapter_id, self_id, group_id,
                                       visibility=event_values(explicit), limit=limit)

    def group_request_diagnostics(
        self, adapter_id: str, self_id: str, group_id: str, *,
        stage: str = "", request_id: str = "", limit: int = 50,
    ) -> dict[str, Any]:
        """在诊断存储内部按平台、账号、群和阶段过滤请求"""
        scheduler = self._scheduler
        if scheduler is None:
            return {"records": [], "available": False, "reason": "scheduler_unavailable"}
        return {
            "records": scheduler.request_diagnostics.list_requests(
                adapter_id, stages=parse_stages(stage), request_id=request_id,
                limit=limit, self_id=self_id, group_id=group_id,
            ),
            "available": True,
            **scheduler.request_diagnostics.stats(adapter_id),
        }

    def group_request_diagnostic_detail(
        self, adapter_id: str, self_id: str, group_id: str, request_id: str,
    ) -> dict[str, Any]:
        """只返回与指定群身份严格相符的阶段明细"""
        scheduler = self._scheduler
        if scheduler is None:
            return {"status": "unknown", "reason": "scheduler_unavailable"}
        detail = scheduler.request_diagnostics.get_request(request_id, adapter_id)
        if detail is None:
            raise LookupError("群请求诊断不存在")
        records = detail["records"]
        if not isinstance(records, list) or not records or any(
            not isinstance(row, dict) or row.get("self_id") != self_id
            or row.get("session_id") != f"group%{group_id}" for row in records
        ):
            raise LookupError("群请求诊断不存在")
        return detail

    async def group_info(self, adapter_id: str, self_id: str, group_id: str) -> dict[str, Any]:
        """按已确认成员关系读取指定群的 OneBot 群信息"""
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        if not isinstance(adapter, OneBotAdapter) or adapter.bot_self_id != self_id:
            raise ValueError("机器人账号已变化")
        if adapter.get_client() is None:
            raise RuntimeError("OneBot 平台未连接")
        return await adapter.admin.get_group_info(group_id)

    async def send_group_message(
        self, adapter_id: str, self_id: str, group_id: str, action_id: str, message: str,
    ) -> dict[str, Any]:
        """显式管理发送先持久占位, 使用同 ID 查询恢复未知回执"""
        from satrap.core.config.group_actions import action_fingerprint
        from satrap.core.config.group_store import GroupConfigConflict
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        if not isinstance(message, str) or not message.strip() or len(message) > 1000:
            raise ValueError("手动发送正文必须为 1 到 1000 字符")
        params: dict[str, object] = {
            "message_digest": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            "message_length": len(message),
        }
        store = await self._group_action_store(adapter_id)
        existing = await asyncio.to_thread(store.get, self_id, group_id, action_id)
        if existing is not None:
            if existing["fingerprint"] != action_fingerprint(self_id, group_id, "send_message", params, "panel"):
                raise GroupConfigConflict("相同 action_id 已用于不同动作")
            return existing
        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        if not isinstance(adapter, OneBotAdapter) or adapter.bot_self_id != self_id:
            raise ValueError("机器人账号已变化")
        if adapter.get_client() is None:
            raise RuntimeError("OneBot 平台未连接")
        if not adapter.allows_management_target(group_id):
            raise PermissionError("机器人当前成员关系未确认或已离开目标群")
        version = adapter.connection_generation()
        record, created = await asyncio.to_thread(
            store.submit, action_id, self_id, group_id, "send_message", params, "panel", version,
            approval_required=False,
        )
        if not created:
            return record
        try:
            if (self._adapter_mgr is None or self._adapter_mgr.get_adapter(adapter_id) is not adapter
                    or adapter.bot_self_id != self_id or adapter.connection_generation() != version):
                state, reason, message_ids = "failed", "adapter_changed", []
            else:
                receipt = await adapter.send_management_message(group_id, message)
                state = "succeeded" if receipt.status == "success" else "unknown" if receipt.status in {"partial", "unknown"} else "failed"
                reason, message_ids = receipt.reason or receipt.status, list(receipt.message_ids)
        except BaseException:
            await asyncio.to_thread(store.settle, self_id, group_id, action_id, "unknown", "interrupted")
            raise
        return await asyncio.to_thread(
            store.settle, self_id, group_id, action_id, state, reason, {"message_ids": message_ids},
        )

    async def wake_group(
        self, adapter_id: str, self_id: str, group_id: str, request_id: str, prompt: str,
    ) -> dict[str, Any]:
        """固定可信账号和群目标, 复用手动唤醒的持久受理链路"""
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        if not isinstance(adapter, OneBotAdapter) or adapter.bot_self_id != self_id:
            raise ValueError("机器人账号已变化")
        if not isinstance(prompt, str):
            raise ValueError("唤醒文本无效")
        return await self.wake_platform(
            {"adapter_id": adapter_id, "group_id": group_id, "user_id": self_id,
             "prompt": prompt, "request_id": request_id},
            operator="management",
        )

    async def group_wake_status(
        self, adapter_id: str, self_id: str, group_id: str, request_id: str,
    ) -> dict[str, Any]:
        """仅在账号和目标群都匹配时返回手动唤醒状态"""
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
        if not isinstance(adapter, OneBotAdapter) or adapter.bot_self_id != self_id:
            raise ValueError("机器人账号已变化")
        result = await self.manual_wake_status(request_id, adapter_id)
        if result.get("reason") in {"store_unavailable", "store_degraded"}:
            return result
        if result.get("target") != f"group:{group_id}":
            raise LookupError("群手动唤醒记录不存在")
        return result

    async def group_actions(
        self, adapter_id: str, self_id: str, group_id: str, *,
        state: str = "pending", page: int = 1, page_size: int = 25,
    ) -> dict[str, Any]:
        """读取固定群的有界审批和执行记录"""
        store = await self._group_action_store(adapter_id)
        return await asyncio.to_thread(store.list, self_id, group_id, state=state, page=page, page_size=page_size)

    async def group_action(self, adapter_id: str, self_id: str, group_id: str, action_id: str) -> dict[str, Any]:
        """按固定群身份查询单个动作, 供响应丢失后按原 ID 恢复"""
        store = await self._group_action_store(adapter_id)
        result = await asyncio.to_thread(store.get, self_id, group_id, action_id)
        if result is None:
            raise LookupError("管理动作不存在")
        return result

    async def submit_group_action(
        self, adapter_id: str, self_id: str, group_id: str, action_id: str,
        action_type: str, params: dict[str, object], *, actor_kind: str,
    ) -> dict[str, Any]:
        """校验目标和参数, 按策略持久登记审批或原子占用后执行"""
        from satrap.core.config.group_actions import action_fingerprint
        from satrap.core.config.group_store import GroupConfigConflict
        from satrap.core.platform.onebot.group_action_types import normalize_action_params

        normalized, secret_flag = normalize_action_params(action_type, params, self_id)
        authorization = current_model_action_authorization() if actor_kind == "model" else None
        if actor_kind == "model":
            if authorization is None or authorization.identity["adapter_id"] != adapter_id or authorization.identity["self_id"] != self_id:
                raise PermissionError("模型群动作缺少可信来源")
            authorization.verify(group_id)
        model_origin = authorization.identity if authorization is not None else None
        store = await self._group_action_store(adapter_id)
        existing = await asyncio.to_thread(store.get, self_id, group_id, action_id)
        if existing is not None:
            expected = action_fingerprint(self_id, group_id, action_type, normalized, actor_kind, model_origin)
            if existing["fingerprint"] != expected:
                raise GroupConfigConflict("相同 action_id 已用于不同动作")
            return existing
        adapter, store, mode, version = await self._group_action_context(
            adapter_id, self_id, group_id, action_type,
        )
        if action_type == "handle_group_request":
            if secret_flag is None:
                raise ValueError("群请求缺少 flag")
            entry = adapter.request_flags.ledger.lookup(adapter_id, self_id, "group", secret_flag)
            if (entry is None or entry["state"] != "available" or entry["group_id"] != group_id
                    or entry["sub_type"] != normalized["sub_type"]
                    or time.time() - entry["received_at"] >= adapter.request_flags.ledger.ttl):
                raise PermissionError("群请求 flag 未登记、已过期或归属不符")
        record, created = await asyncio.to_thread(
            store.submit, action_id, self_id, group_id, action_type, normalized,
            actor_kind, version, approval_required=mode == "approval_required", model_origin=model_origin,
        )
        if not created:
            return record
        if authorization is not None:
            self._group_action_authorizers[action_id] = authorization
        if secret_flag is not None:
            self._group_action_flags[action_id] = secret_flag
        if mode == "approval_required":
            return record
        return await self._execute_group_action(adapter_id, self_id, group_id, action_id, record)

    async def decide_group_action(
        self, adapter_id: str, self_id: str, group_id: str, action_id: str,
        *, approve: bool,
    ) -> dict[str, Any]:
        """人工批准或拒绝固定群的待审批动作, 并发只占用一次"""
        current = await self.group_action(adapter_id, self_id, group_id, action_id)
        if approve:
            _, store, _, version = await self._group_action_context(
                adapter_id, self_id, group_id, current["action_type"],
            )
        else:
            from satrap.core.platform.onebot.adapter import OneBotAdapter

            adapter = self._adapter_mgr.get_adapter(adapter_id) if self._adapter_mgr else None
            if not isinstance(adapter, OneBotAdapter) or adapter.bot_self_id != self_id:
                raise ValueError("机器人账号已变化")
            store = await self._group_action_store(adapter_id)
            version = int(current["policy_revision"])
        decided = await asyncio.to_thread(
            store.decide, self_id, group_id, action_id, approve=approve, policy_revision=version,
        )
        if not approve:
            self._group_action_flags.pop(action_id, None)
            self._group_action_authorizers.pop(action_id, None)
            return decided
        return await self._execute_group_action(adapter_id, self_id, group_id, action_id, decided)

    async def _execute_group_action(
        self, adapter_id: str, self_id: str, group_id: str, action_id: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        """占用后再次核验策略与成员关系, 按 OneBot 结果保守结算"""
        from satrap.core.platform.onebot.admin import AdminActionRejected, AdminActionUnconfirmed, UnsupportedAdminAction

        store = await self._group_action_store(adapter_id)
        action = str(record["action_type"])
        started = False
        try:
            adapter, _, _, version = await self._group_action_context(adapter_id, self_id, group_id, action)
            if version != record["policy_revision"]:
                raise AdminActionRejected("审批策略或连接代次已变化")
            if record["actor_kind"] == "model":
                persisted_origin = await asyncio.to_thread(store.model_origin, self_id, group_id, action_id)
                authorization = self._group_action_authorizers.get(action_id)
                if authorization is None or persisted_origin is None or dict(authorization.identity) != persisted_origin:
                    raise ModelActionAuthorizationError("model_source_unavailable")
                try:
                    authorization.verify(group_id)
                except Exception as error:
                    raise ModelActionAuthorizationError("model_permission_revoked") from error
            params = dict(record["params"])
            flag = self._group_action_flags.get(action_id)
            if "flag_digest" in params:
                params.pop("flag_digest")
                if flag is None:
                    raise AdminActionRejected("请求 flag 已失效, 需要重新提交")
                params["flag"] = flag
            method = getattr(adapter.admin, action)
            started = True
            await method(group_id, **params)
            state, reason = "succeeded", "platform_confirmed"
        except ModelActionAuthorizationError as error:
            state, reason = "failed", str(error)
        except (AdminActionRejected, UnsupportedAdminAction, PermissionError, ValueError) as error:
            state, reason = "failed", type(error).__name__
        except (AdminActionUnconfirmed, asyncio.TimeoutError) as error:
            state, reason = "unknown", type(error).__name__
        except Exception as error:
            state, reason = ("unknown" if started else "failed"), type(error).__name__
        except BaseException:
            await asyncio.to_thread(store.settle, self_id, group_id, action_id, "unknown", "interrupted")
            raise
        finally:
            self._group_action_flags.pop(action_id, None)
            self._group_action_authorizers.pop(action_id, None)
        try:
            return await asyncio.to_thread(store.settle, self_id, group_id, action_id, state, reason)
        except Exception as error:
            logger.error(f"[BackendManager] 群动作结果落库失败 action_id={action_id} state={state}: {type(error).__name__}")
            raise RuntimeError("群动作已发出但结果持久化失败, 平台结果未知") from error

    def get_platform_runtime(self, platform_id: str) -> tuple[SessionManager, UserManager] | None:
        """
        获取平台实例运行时管理器

        参数:
        - platform_id: 平台实例 ID

        返回:
        - tuple[SessionManager, UserManager] | None: 会话和用户管理器
        """
        return self._platform_runtimes.get(platform_id)

    def list_platform_runtimes(self) -> dict[str, tuple[SessionManager, UserManager]]:
        """返回平台实例运行时映射的副本"""
        return dict(self._platform_runtimes)

    @property
    def storage_layout(self) -> StorageLayout:
        """返回后端统一使用的 v2 数据布局"""
        return self._storage

    def list_edictum_config_references(self, config_name: str) -> list[dict[str, str]]:
        """
        列出全部平台中引用指定 Edictum 配置的会话实例

        参数:
        - config_name: Edictum 配置名称

        返回:
        - list[dict[str, str]]: 平台和会话引用
        """
        references: list[dict[str, str]] = []
        for platform_id, (session_manager, _) in self._platform_runtimes.items():
            references.extend(
                {
                    "platform_id": platform_id,
                    "session_id": session_id,
                }
                for session_id in session_manager.store.list_definition_references(
                    EDICTUM_PROVIDER,
                    config_name,
                )
            )
        return references

    def rename_edictum_config_references(
        self,
        old_name: str,
        new_name: str,
    ) -> list[dict[str, str]]:
        """
        迁移全部平台中的 Edictum 配置引用

        参数:
        - old_name: 原配置名称
        - new_name: 新配置名称

        返回:
        - list[dict[str, str]]: 已迁移的平台和会话引用
        """
        migrated: list[dict[str, str]] = []
        completed: list[SessionManager] = []
        try:
            for platform_id, (session_manager, _) in self._platform_runtimes.items():
                session_ids = session_manager.store.rename_definition_references(
                    EDICTUM_PROVIDER,
                    old_name,
                    new_name,
                )
                completed.append(session_manager)
                migrated.extend(
                    {"platform_id": platform_id, "session_id": session_id}
                    for session_id in session_ids
                )
        except Exception:
            for session_manager in reversed(completed):
                session_manager.store.rename_definition_references(
                    EDICTUM_PROVIDER,
                    new_name,
                    old_name,
                )
            raise
        return migrated

    @property
    def scheduler(self) -> PipelineScheduler | None:
        """
        获取 scheduler

        返回:
        - PipelineScheduler | None: 获取 scheduler
        """
        return self._scheduler

    @property
    def adapter_manager(self) -> PlatformAdapterManager | None:
        """
        获取 adapter_manager

        返回:
        - PlatformAdapterManager | None: 获取 adapter_manager
        """
        return self._adapter_mgr

    def set_shutdown_event(self, event: asyncio.Event | None):
        """
        设置外部关闭事件, 供 HTTP 管理接口触发

        参数:
        - event: 事件
        """
        self._shutdown_event = event

    def request_shutdown(self):
        """请求后端主循环退出"""
        if self._shutdown_event:
            self._shutdown_event.set()

    # ---------- 启动 ----------

    async def start(self):
        """完整启动流程"""
        try:
            self._init_model_config()
            self._init_session_class_config()
            self._init_edictum_config()
            self._init_session_manager()
            self._init_user_manager()
            self._init_pipeline()
            await self._init_platforms()
            await self._init_http_api()
            logger.info("[BackendManager] 所有组件初始化完成")
        except Exception as e:
            logger.error(f"[BackendManager] 启动失败: {e}")
            await self.stop()
            raise

    async def wake_platform(self, payload: dict[str, Any], *, operator: str) -> dict[str, Any]:
        """
        向平台队列提交手动唤醒, 操作者必须来自可信应用调用边界

        参数:
        - payload: 目标平台, 群, 路由用户, prompt 和 request_id
        - operator: 已认证的管理主体, 禁止从 payload 读取

        返回:
        - dict[str, Any]: accepted, already_pending, no_pending 或 rejected
        """
        from dataclasses import replace
        from satrap.core.pipeline.manual_wake import ManualWakeTicket
        from satrap.core.platform.onebot.adapter import OneBotAdapter
        from satrap.core.platform.event import MessageEvent

        allowed = {"adapter_id", "group_id", "user_id", "prompt", "message_id", "request_id", "reason"}
        if set(payload) - allowed or not operator:
            return {"status": "rejected", "reason": "invalid_fields_or_operator"}
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128 or "\n" in request_id:
            return {"status": "rejected", "reason": "invalid_request_id"}
        if not self._running or self._scheduler is None or self._adapter_mgr is None:
            return {"status": "rejected", "request_id": request_id, "reason": "backend_unavailable"}
        adapter_id = payload.get("adapter_id")
        adapter = self._adapter_mgr.get_adapter(adapter_id) if isinstance(adapter_id, str) else None
        if not isinstance(adapter, OneBotAdapter) or not adapter.config.enable or not adapter.started:
            return {"status": "rejected", "request_id": request_id, "reason": "adapter_unavailable"}
        group_id, user_id = payload.get("group_id"), payload.get("user_id")
        if not all(isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0 for value in (group_id, user_id)):
            return {"status": "rejected", "request_id": request_id, "reason": "explicit_group_and_route_user_required"}
        if not adapter.allows_group(str(group_id)) or not adapter.bot_self_id:
            return {"status": "rejected", "request_id": request_id, "reason": "source_unavailable"}
        prompt = payload.get("prompt", "")
        if not isinstance(prompt, str) or len(prompt) > 8192:
            return {"status": "rejected", "request_id": request_id, "reason": "invalid_prompt"}
        message_id = payload.get("message_id", "")
        if not isinstance(message_id, str) or (message_id and (len(message_id) > 24 or not message_id.lstrip("-").isascii() or not message_id.lstrip("-").isdecimal())) or (message_id and prompt.strip()):
            return {"status": "rejected", "request_id": request_id, "reason": "invalid_message_id_or_conflicting_prompt"}
        requests = self._scheduler.manual_wakes
        fingerprint = requests.fingerprint(payload, operator)
        duplicate = requests.check(request_id, fingerprint)
        if duplicate is not None:
            return duplicate
        raw_message: dict[str, Any] = {"self_id": adapter.bot_self_id, "group_id": group_id,
            "user_id": user_id, "message_id": "", "message_type": "group", "post_type": "message",
            "message": [{"type": "text", "data": {"text": prompt}}]}
        if message_id:
            try:
                raw_message = await adapter.fetch_group_message(message_id, str(group_id), str(user_id))
            except Exception as error:
                logger.warning(f"[BackendManager] 手动唤醒回源失败 request_id={request_id} adapter={adapter_id} operator={operator}: {type(error).__name__}")
                return {"status": "rejected", "request_id": request_id, "reason": "message_lookup_failed_or_scope_mismatch"}
        try:
            message = await adapter.convert_message(raw_message)
        except Exception as error:
            logger.warning(f"[BackendManager] 手动唤醒消息转换失败 request_id={request_id} adapter={adapter_id}: {type(error).__name__}")
            return {"status": "rejected", "request_id": request_id, "reason": "message_convert_failed"}
        if message_id:
            prompt = message.message_str
            if not prompt.strip():
                return {"status": "no_pending", "request_id": request_id, "reason": "message_has_no_text"}
        event = MessageEvent(prompt, message, adapter.meta(), message.session_id, adapter,
                             adapter.config.session_provider, adapter.get_session_type())
        snapshot = self._scheduler.wake_window.peek(event) if not prompt.strip() and not message_id else ()
        if not prompt.strip() and not snapshot and not message_id:
            return {"status": "no_pending", "request_id": request_id}
        if snapshot:
            event.message_str = "\n".join(item.text for item in snapshot)
        event._call_origin = replace(event.call_origin, actor_id=operator, actor_kind="management", route_user_id=str(user_id), request_id=request_id)
        store = self._manual_wake_store
        # 接受事务: 查重 + 持久占位 + 入队由同一把异步锁保护, 成功落盘后才返回 accepted
        async with self._wake_accept_lock:
            duplicate = requests.check(request_id, fingerprint)
            if duplicate is not None:
                return duplicate
            if store is not None:
                if store.degraded:
                    return {"status": "rejected", "request_id": request_id, "reason": "store_unavailable"}
                try:
                    record = await asyncio.to_thread(store.lookup_request, request_id, str(adapter_id))
                except ManualWakeStoreError as error:
                    logger.warning(f"[BackendManager] 手动唤醒查重失败 request_id={request_id} adapter={adapter_id}: {error}")
                    return {"status": "rejected", "request_id": request_id, "reason": "store_unavailable"}
                if record is not None:
                    if record["fingerprint"] == fingerprint:
                        return {"status": "already_pending", "request_id": request_id, "state": record["status"], "reason": "duplicate"}
                    return {"status": "rejected", "request_id": request_id, "reason": "request_id_conflict"}
            if self._adapter_mgr.get_adapter(str(adapter_id)) is not adapter or not adapter.started or not adapter.allows_group(str(group_id)):
                return {"status": "rejected", "request_id": request_id, "reason": "adapter_changed"}
            if adapter._event_queue.full():
                return {"status": "rejected", "request_id": request_id, "reason": "queue_full"}
            if store is not None:
                try:
                    await asyncio.to_thread(store.accept_request, str(adapter_id), request_id, fingerprint, f"group:{group_id}", operator)
                except ManualWakeStoreError as error:
                    reason = "request_capacity" if error.reason == "capacity" else "store_unavailable"
                    logger.warning(f"[BackendManager] 手动唤醒占位落盘失败 request_id={request_id} adapter={adapter_id}: {error}")
                    return {"status": "rejected", "request_id": request_id, "reason": reason}
            ticket = ManualWakeTicket(request_id, snapshot)
            requests.register(event, fingerprint, ticket)
            if not adapter.commit_event(event):
                requests.records.pop(request_id, None)
                if store is not None:
                    try:
                        outcome = await asyncio.to_thread(
                            store.update_request, str(adapter_id), request_id, "failed", "queue_full",
                        )
                    except ManualWakeStoreError as error:
                        logger.warning(f"[BackendManager] 手动唤醒回滚写入失败 request_id={request_id} adapter={adapter_id}: {error}")
                    else:
                        if outcome not in {"persisted", "no_op", "degraded"}:
                            logger.warning(
                                f"[BackendManager] 手动唤醒回滚未落盘 request_id={request_id} "
                                f"adapter={adapter_id} outcome={outcome}",
                            )
                logger.warning(f"[BackendManager] 手动唤醒入队失败 request_id={request_id} adapter={adapter_id}")
                return {"status": "rejected", "request_id": request_id, "reason": "queue_full"}
        logger.info(f"[BackendManager] 手动唤醒已接受 request_id={request_id} adapter={adapter_id} group={group_id} operator={operator} pending={len(snapshot)}")
        return {"status": "accepted", "request_id": request_id}

    async def manual_wake_status(self, request_id: str, adapter_id: str | None = None) -> dict[str, Any]:
        """
        查询手动唤醒请求的持久化状态, 重启后可查

        参数:
        - request_id: 客户端幂等标识
        - adapter_id: 可选适配器实例 ID, 缺省跨实例按 request_id 查最新记录

        返回:
        - dict[str, Any]: 记录状态与明细; 未找到/存储不可用时给出明确原因
        """
        store = self._manual_wake_store
        if store is None:
            return {"status": "unknown", "request_id": request_id, "reason": "store_unavailable"}
        if store.degraded:
            return {"status": "unknown", "request_id": request_id, "reason": "store_degraded", "detail": store.degraded_reason}
        try:
            record = await asyncio.to_thread(store.lookup_request, request_id, adapter_id)
        except ManualWakeStoreError as error:
            logger.warning(f"[BackendManager] 手动唤醒状态查询失败 request_id={request_id} adapter={adapter_id}: {error}")
            return {"status": "unknown", "request_id": request_id, "reason": "store_unavailable"}
        if store.degraded:
            # 查询期间归档读取失败会进入降级, 此时不能把结果报成未找到
            return {"status": "unknown", "request_id": request_id, "reason": "store_degraded", "detail": store.degraded_reason}
        if record is None:
            return {"status": "unknown", "request_id": request_id, "reason": "not_found"}
        return {
            "status": record["status"], "request_id": record["request_id"], "adapter_id": record["adapter_id"],
            "target": record["target"], "operator": record["operator"], "detail": record["detail"],
            "created_at": record["created_at"], "updated_at": record["updated_at"],
        }

    def wake_rejections(self, adapter_id: str | None = None, limit: int = 50) -> list[dict[str, object]]:
        """
        查询唤醒决策点与限流点的有界拒绝记录, 最新在前

        参数:
        - adapter_id: 可选适配器实例 ID, 缺省跨实例按时间合并
        - limit: 返回条数上限

        返回:
        - list[dict[str, object]]: 拒绝记录 (只含决策点与限流点阶段); 调度器未装配时为空列表
        """
        scheduler = self._scheduler
        if scheduler is None:
            return []
        return scheduler.request_diagnostics.list(adapter_id, limit, stages=REJECTION_STAGES)

    def request_diagnostics(
        self, adapter_id: str | None = None, *, stage: str = "", request_id: str = "", limit: int = 50,
    ) -> dict[str, Any]:
        """
        查询按请求关联的有界诊断摘要, 最新在前

        参数:
        - adapter_id: 可选适配器实例 ID, 缺省跨实例按时间合并
        - stage: 阶段过滤, 多个阶段用逗号分隔, 命中任一阶段即保留该请求; 空串不过滤
        - request_id: 只保留该请求
        - limit: 返回请求数上限

        返回:
        - dict[str, Any]: 摘要列表与容量信息; 调度器未装配时显式标记不可用

        异常:
        - ValueError: stage 含空项或未知阶段
        """
        scheduler = self._scheduler
        if scheduler is None:
            return {"records": [], "available": False, "reason": "scheduler_unavailable"}
        return {
            "records": scheduler.request_diagnostics.list_requests(
                adapter_id, stages=parse_stages(stage), request_id=request_id, limit=limit,
            ),
            "available": True,
            **scheduler.request_diagnostics.stats(adapter_id),
        }

    def request_diagnostic_detail(self, request_id: str, adapter_id: str | None = None) -> dict[str, Any]:
        """
        单个请求的完整阶段诊断

        参数:
        - request_id: 逻辑请求标识
        - adapter_id: 可选适配器实例 ID

        返回:
        - dict[str, Any]: 阶段记录明细; 未采集到或调度器未装配时给出明确原因
        """
        scheduler = self._scheduler
        if scheduler is None:
            return {"status": "unknown", "request_id": request_id, "reason": "scheduler_unavailable"}
        detail = scheduler.request_diagnostics.get_request(request_id, adapter_id)
        if detail is None:
            return {"status": "unknown", "request_id": request_id, "reason": "not_found"}
        return detail

    async def reload_config(self, expected_config_revision: str | None = None) -> dict[str, Any]:
        """
        重载模型与会话定义, 并应用可在线更新的平台策略

        参数:
        - expected_config_revision: 控制端保存的文档修订, 默认 None 不核对来源内容

        返回:
        - dict[str, Any]: 平台配置应用状态与 Edictum 活跃会话同步结果
        """
        if self._model_cfg:
            self._model_cfg.reload()
        if self._session_cls_cfg:
            self._session_cls_cfg.reload()
        if self._edictum_cfg:
            self._edictum_cfg.reload()
        platform_results = await self.reload_platform_policies(expected_config_revision)
        edictum_results = await self.reconcile_edictum_runtime_async()
        for session_manager, _ in self._platform_runtimes.values():
            await session_manager.reload_model_configs_async()
        failed_platforms = [str(item.get("id") or "配置文件") for item in platform_results if item.get("status") == "failed"]
        if failed_platforms:
            logger.warning(f"[BackendManager] 配置已重载, 但 {len(failed_platforms)} 个平台应用失败: {', '.join(failed_platforms)}")
        else:
            logger.info("[BackendManager] 配置已重载")
        return {
            "ok": all(item.get("ok", False) for item in edictum_results) and all(item["status"] == "applied" for item in platform_results),
            "platforms": platform_results,
            "edictum_sessions": edictum_results,
        }

    @staticmethod
    def _normalized_platform_snapshot(config: dict[str, Any]) -> dict[str, Any]:
        """
        生成与保存路径同口径的平台配置快照, 启动配置未经校验时按原样保留

        参数:
        - config: 启动或热更新使用的平台配置

        返回:
        - 归一化后的独立副本, 校验失败时为原配置的深拷贝
        """
        from satrap.core.config.document import validate_platforms
        # 延迟导入配置边界, 避免配置文档依赖 BackendConfig 形成循环
        try:
            return validate_platforms([deepcopy(config)])[0]
        except Exception as error:
            logger.warning(f"[BackendManager] 平台配置 {config.get('id', '')} 未通过校验, 生效指纹按原样计算: {type(error).__name__}")
            return deepcopy(config)

    @staticmethod
    def _platform_revision(config: dict[str, Any] | None) -> str | None:
        """
        计算配置指纹, 不向调用方返回配置正文或密钥

        参数:
        - config: 平台配置快照, None 表示不存在

        返回:
        - 配置摘要或 None
        """
        if config is None:
            return None
        payload = json.dumps(config, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def reload_platform_policies(self, expected_config_revision: str | None = None) -> list[dict[str, Any]]:
        """
        从实际启动文件读取平台配置, 在线应用策略或定向协调实例

        参数:
        - expected_config_revision: 控制端保存的文档修订, 默认 None 不核对来源内容

        返回:
        - 各实例保存/生效指纹与应用状态, 后端未运行时标为 pending_restart
        """
        from satrap.core.config.document import config_document_revision, load_config_document, validate_platforms
        # 延迟导入配置边界, 避免配置文档依赖 BackendConfig 形成循环

        async with self._platform_apply_lock:
            source_revision = None
            try:
                if self.config.source_path:
                    source = Path(self.config.source_path)
                    if not source.is_file():
                        raise FileNotFoundError("启动配置文件不存在")
                    source_document = await asyncio.to_thread(load_config_document, source, locked=True)
                    # 持锁读取可能自旋等待, 移出事件循环避免冻结
                    source_revision = config_document_revision(source_document)
                    candidates = validate_platforms(source_document.get("platforms", []))
                else:
                    candidates = validate_platforms(deepcopy(self.config.platforms))
            except Exception as error:
                logger.warning(f"[BackendManager] 平台配置读取或校验失败: {type(error).__name__}: {error}")
                self._platform_config_results = [{
                    "id": platform_id, "saved_revision": None,
                    "active_revision": self._platform_revision(self._normalized_platform_snapshot(active)), "status": "failed",
                    "error": f"平台配置读取或校验失败: {type(error).__name__}",
                } for platform_id, active in self._platform_active_configs.items()]
                if not self._platform_config_results:
                    self._platform_config_results = [{"id": "", "saved_revision": None, "active_revision": None,
                                                      "status": "failed", "error": "平台配置读取或校验失败"}]
                return deepcopy(self._platform_config_results)
            if expected_config_revision is not None and source_revision != expected_config_revision:
                logger.warning("[BackendManager] 平台配置修订与本次保存不一致, 未应用平台变更")
                candidate_map = {str(item["id"]): item for item in candidates}
                self._platform_config_results = [{
                    "id": platform_id, "status": "failed", "reason": "source_revision_mismatch",
                    "saved_revision": self._platform_revision(candidate_map.get(platform_id)),
                    "active_revision": self._platform_revision(self._normalized_platform_snapshot(self._platform_active_configs[platform_id]))
                    if platform_id in self._platform_active_configs else None,
                    "error": "后端实际配置与本次保存的修订不一致, 未应用平台变更",
                } for platform_id in sorted(set(candidate_map) | set(self._platform_active_configs) or {""})]
                return deepcopy(self._platform_config_results)
            self.config.platforms = deepcopy(candidates)
            desired = {str(item["id"]): item for item in candidates}
            results: list[dict[str, Any]] = []
            # 逐事件生效的字段由策略字段契约派生: 变更只替换配置与策略快照, 不重建实例或连接; 已冻结事件保留其原快照
            hot_keys = hot_reload_keys()
            for platform_id in sorted(set(desired) | set(self._platform_active_configs)):
                candidate = desired.get(platform_id)
                active = self._platform_active_configs.get(platform_id)
                normalized_active = self._normalized_platform_snapshot(active) if active is not None else None
                saved_revision = self._platform_revision(candidate)
                active_revision = self._platform_revision(normalized_active)
                result: dict[str, Any] = {"id": platform_id, "saved_revision": saved_revision,
                                          "active_revision": active_revision, "status": "pending_restart"}
                adapter = self._adapter_mgr.get_adapter(platform_id) if self._adapter_mgr else None
                if candidate is not None and normalized_active is not None and adapter is not None:
                    previous = deepcopy(normalized_active)
                    proposed = deepcopy(candidate)
                    old_settings = previous.pop("settings", {})
                    new_settings = proposed.pop("settings", {})
                    try:
                        if candidate["type"] in {"onebot", "aiocqhttp"}:
                            for settings in (old_settings, new_settings):
                                settings["host"] = str(settings.get("host") or settings.get("listen_host") or "127.0.0.1")
                                settings["port"] = int(settings.get("port") or settings.get("listen_port") or 8080)
                                settings.pop("listen_host", None)
                                settings.pop("listen_port", None)
                                for name in ("access_token", "secret", "self_id"):
                                    settings[name] = str(settings.get(name) or "")
                    except (TypeError, ValueError):
                        result["status"] = "failed"
                        result["error"] = "连接参数校验失败, 保留旧配置"
                        results.append(result)
                        continue
                    changed = {key for key in set(old_settings) | set(new_settings) if old_settings.get(key) != new_settings.get(key)}
                    if candidate["type"] in {"onebot", "aiocqhttp"}:
                        from satrap.core.platform.onebot.adapter import OneBotAdapter

                        if isinstance(adapter, OneBotAdapter) and adapter.bot_self_id:
                            try:
                                store = GroupConfigStore(self._storage.platform_db(platform_id))
                                await asyncio.to_thread(store.check_legacy_source, adapter.bot_self_id, new_settings)
                            except Exception as error:
                                result.update(status="failed", reason="legacy_group_config_conflict", error=str(error))
                                results.append(result)
                                continue
                    runtime_usable = not self._running or not adapter.config.enable or (adapter.started and adapter._run_task is not None and not adapter._run_task.done())
                    if runtime_usable and previous == proposed and (not changed or (candidate["type"] in {"onebot", "aiocqhttp"} and changed <= hot_keys)):
                        adapter.config = replace(adapter.config, settings=deepcopy(candidate.get("settings", {})))
                        if self._scheduler is not None and old_settings != new_settings:
                            self._scheduler.wake_window.clear_adapter(platform_id)
                            self._scheduler.wake_timers.clear_adapter(platform_id)
                            await self._scheduler.clear_manual_wakes(platform_id)
                        self._platform_active_configs[platform_id] = deepcopy(candidate)
                        result["active_revision"] = saved_revision
                        result["status"] = "applied"
                    else:
                        result["reason"] = "连接, 执行容量或会话绑定变更需要重建实例"
                else:
                    result["reason"] = "新增, 删除或未启动的平台需要协调生命周期"
                if result["status"] == "pending_restart" and self._running and self._dispatcher is not None:
                    try:
                        await self._replace_platform_instance(platform_id, candidate)
                        if candidate is None:
                            self._platform_active_configs.pop(platform_id, None)
                        else:
                            self._platform_active_configs[platform_id] = deepcopy(candidate)
                        result.update(status="applied", active_revision=saved_revision)
                        result.pop("reason", None)
                    except Exception as error:
                        logger.warning(f"[BackendManager] 平台 {platform_id} 应用失败: {type(error).__name__}: {error}")
                        restored = self._adapter_mgr.get_adapter(platform_id) if self._adapter_mgr else None
                        preserved = restored is adapter and restored is not None and (
                            not restored.config.enable or (restored.started and restored._run_task is not None and not restored._run_task.done())
                        )
                        result.update(status="failed", error=f"平台应用失败: {type(error).__name__}", old_runtime_preserved=preserved)
                        if not preserved:
                            result["active_revision"] = None
                        result.pop("reason", None)
                results.append(result)
            self._platform_config_results = results
            return deepcopy(results)

    async def _replace_platform_instance(self, platform_id: str, candidate: dict[str, Any] | None) -> None:
        """
        定向替换或移除平台, 新实例启动失败时恢复旧实例

        参数:
        - platform_id: 目标平台 ID
        - candidate: 已校验的目标配置, None 表示删除
        """
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        manager = self._adapter_mgr
        dispatcher = self._dispatcher
        if manager is None or dispatcher is None:
            raise RuntimeError("平台运行时未初始化")
        old = manager.get_adapter(platform_id)
        old_config = old.config if old else None
        old_started = bool(old and old.started)
        replacement: PlatformAdapter | None = None
        logger.info(f"[BackendManager] 开始{'删除' if candidate is None else '替换'}平台实例: {platform_id}")
        runtime = self._platform_runtimes.get(platform_id)
        previous_environment = runtime[0].plugin_environment if runtime else None
        if candidate is not None:
            provider = str(candidate.get("session_provider", SESSION_CLASS_PROVIDER))
            platform_type = str(candidate["type"])
            session_type = self._resolve_platform_session_type(platform_type, str(candidate.get("session_type", "")), provider)
            session_manager, _ = self._ensure_platform_runtime(platform_id)
            definition = session_manager.provider_registry.resolve_definition(session_type, provider)
            if definition is None or not definition[1].enabled:
                raise ValueError("平台绑定的会话定义不可用")
            replacement = manager.registry.create(PlatformConfig(
                id=platform_id, type=platform_type, session_provider=provider, session_type=session_type,
                enable=bool(candidate.get("enable", True)), settings=deepcopy(candidate.get("settings", {})),
            ), event_handler=old.event_handler if old else self.platform_events)
            if replacement is None:
                raise ValueError("平台类型不可用")
            if isinstance(replacement, OneBotAdapter):
                replacement.set_group_access_store(GroupDirectoryStore(self._storage.platform_db(platform_id)))
                replacement.set_group_sync_handler(lambda: self.trigger_group_sync(platform_id, replacement.bot_self_id))
                replacement.group_action_handler = lambda group_id, action_type, params: self.submit_group_action(
                    platform_id, replacement.bot_self_id, group_id, secrets.token_hex(16),
                    action_type, params, actor_kind="model",
                )
            self._attach_send_attempt_recorder(replacement)
            self._attach_request_ledger(replacement)
        try:
            if old is not None:
                old.config = replace(old.config, enable=False)
            await dispatcher.detach_adapter(platform_id)
            if self._scheduler is not None:
                self._scheduler.wake_window.clear_adapter(platform_id)
                self._scheduler.wake_timers.clear_adapter(platform_id)
                await self._scheduler.clear_manual_wakes(platform_id)
            if old is not None:
                await old.terminate()
            if replacement is not None:
                if replacement.config.enable:
                    await replacement.start()
                    await replacement.wait_ready()
                manager._adapters[platform_id] = replacement
                self._platform_runtimes[platform_id][0].plugin_environment = PluginEnvironment("platform", replacement.config.type)
                await dispatcher.attach_adapter(replacement)
                if isinstance(replacement, OneBotAdapter) and replacement._client_connected and replacement.bot_self_id:
                    await self.trigger_group_sync(platform_id, replacement.bot_self_id)
            else:
                manager._adapters.pop(platform_id, None)
            if self._scheduler is not None:
                self._scheduler.set_platform_runtimes(self._platform_runtimes)
            if isinstance(old, OneBotAdapter) and old.bot_self_id and (
                candidate is None or isinstance(replacement, OneBotAdapter)
                and replacement.bot_self_id and replacement.bot_self_id != old.bot_self_id
            ):
                store = await self._group_action_store(platform_id)
                await asyncio.to_thread(
                    store.expire_account, old.bot_self_id,
                    "platform_deleted" if candidate is None else "account_changed",
                )
        except BaseException as error:
            logger.warning(f"[BackendManager] 平台 {platform_id} 替换失败, 回滚旧实例: {type(error).__name__}: {error}")
            try:
                if replacement is not None:
                    await dispatcher.detach_adapter(platform_id)
                    await replacement.terminate()
                if old is not None and old_config is not None:
                    old.config = old_config
                    manager._adapters[platform_id] = old
                    if old_started:
                        await old.start()
                        await old.wait_ready()
                    await dispatcher.attach_adapter(old)
                else:
                    manager._adapters.pop(platform_id, None)
                if runtime is not None and previous_environment is not None:
                    runtime[0].plugin_environment = previous_environment
            except Exception as rollback_error:
                logger.error(f"[BackendManager] 平台 {platform_id} 回滚失败, 平台可能处于停机状态: {type(rollback_error).__name__}: {rollback_error}")
            raise
        logger.info(f"[BackendManager] 平台实例已{'删除' if candidate is None else '替换'}: {platform_id}")

    def preview_edictum_runtime_changes(
        self,
        *,
        config_name: str | None = None,
        session_refs: list[dict[str, str]] | None = None,
        desired_config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        预览全部或指定平台会话的完整 Edictum 配置变更

        参数:
        - config_name: 可选 Edictum 配置名称过滤
        - session_refs: 可选平台和会话引用
        - desired_config: 可选的尚未保存目标配置

        返回:
        - list[dict[str, Any]]: 逐会话影响预览
        """
        refs_by_platform: dict[str, set[str]] | None = None
        if session_refs is not None:
            refs_by_platform = {}
            for ref in session_refs:
                platform_id = str(ref.get("platform_id", "")).strip()
                session_id = str(ref.get("session_id", "")).strip()
                if platform_id and session_id:
                    refs_by_platform.setdefault(platform_id, set()).add(session_id)
        results: list[dict[str, Any]] = []
        for platform_id, (session_manager, _) in self._platform_runtimes.items():
            if refs_by_platform is not None and platform_id not in refs_by_platform:
                continue
            results.extend(session_manager.preview_edictum_runtime(
                config_name=config_name,
                session_ids=(refs_by_platform or {}).get(platform_id),
                desired_config=desired_config,
            ))
        return results

    async def reconcile_edictum_runtime_async(
        self,
        *,
        config_name: str | None = None,
        session_refs: list[dict[str, str]] | None = None,
        desired_config: dict[str, Any] | None = None,
        concurrency: int = 4,
    ) -> list[dict[str, Any]]:
        """
        有界并发协调完整 Edictum 运行时配置

        参数:
        - config_name: 可选 Edictum 配置名称过滤
        - session_refs: 可选平台和会话引用
        - desired_config: 可选目标配置覆盖
        - concurrency: 每个平台最大并发协调数

        返回:
        - list[dict[str, Any]]: 逐会话协调结果
        """
        refs_by_platform: dict[str, set[str]] | None = None
        if session_refs is not None:
            refs_by_platform = {}
            for ref in session_refs:
                platform_id = str(ref.get("platform_id", "")).strip()
                session_id = str(ref.get("session_id", "")).strip()
                if platform_id and session_id:
                    refs_by_platform.setdefault(platform_id, set()).add(session_id)
        pending: list[Awaitable[list[dict[str, Any]]]] = []
        for platform_id, (session_manager, _) in self._platform_runtimes.items():
            if refs_by_platform is not None and platform_id not in refs_by_platform:
                continue
            pending.append(session_manager.reconcile_edictum_runtime_async(
                config_name=config_name,
                session_ids=(refs_by_platform or {}).get(platform_id),
                desired_config=desired_config,
                concurrency=concurrency,
            ))
        grouped = await asyncio.gather(*pending) if pending else []
        return [item for group in grouped for item in group]

    def preview_edictum_plugin_changes(
        self,
        *,
        config_name: str | None = None,
        session_refs: list[dict[str, str]] | None = None,
        desired_plugins: object | None = None,
    ) -> list[dict[str, Any]]:
        """
        预览全部或指定平台会话的 Edictum 插件变更

        参数:
        - config_name: 可选 Edictum 配置名称过滤
        - session_refs: 可选平台和会话引用
        - desired_plugins: 可选目标插件配置覆盖

        返回:
        - list[dict[str, Any]]: 逐会话影响预览
        """
        refs_by_platform: dict[str, set[str]] | None = None
        if session_refs is not None:
            refs_by_platform = {}
            for ref in session_refs:
                platform_id = str(ref.get("platform_id", "")).strip()
                session_id = str(ref.get("session_id", "")).strip()
                if platform_id and session_id:
                    refs_by_platform.setdefault(platform_id, set()).add(session_id)
        results: list[dict[str, Any]] = []
        for platform_id, (session_manager, _) in self._platform_runtimes.items():
            if refs_by_platform is not None and platform_id not in refs_by_platform:
                continue
            results.extend(session_manager.preview_edictum_plugins(
                config_name=config_name,
                session_ids=(refs_by_platform or {}).get(platform_id),
                desired_plugins=desired_plugins,
            ))
        return results

    async def reconcile_edictum_plugins_async(
        self,
        *,
        config_name: str | None = None,
        session_refs: list[dict[str, str]] | None = None,
        desired_plugins: object | None = None,
        concurrency: int = 4,
    ) -> list[dict[str, Any]]:
        """
        有界并发协调全部或指定平台会话的 Edictum 插件

        参数:
        - config_name: 可选 Edictum 配置名称过滤
        - session_refs: 可选平台和会话引用
        - desired_plugins: 可选目标插件配置覆盖
        - concurrency: 每个平台最大并发协调数

        返回:
        - list[dict[str, Any]]: 逐会话协调结果
        """
        refs_by_platform: dict[str, set[str]] | None = None
        if session_refs is not None:
            refs_by_platform = {}
            for ref in session_refs:
                platform_id = str(ref.get("platform_id", "")).strip()
                session_id = str(ref.get("session_id", "")).strip()
                if platform_id and session_id:
                    refs_by_platform.setdefault(platform_id, set()).add(session_id)
        pending: list[Awaitable[list[dict[str, Any]]]] = []
        for platform_id, (session_manager, _) in self._platform_runtimes.items():
            if refs_by_platform is not None and platform_id not in refs_by_platform:
                continue
            pending.append(session_manager.reconcile_edictum_plugins_async(
                config_name=config_name,
                session_ids=(refs_by_platform or {}).get(platform_id),
                desired_plugins=desired_plugins,
                concurrency=concurrency,
            ))
        grouped = await asyncio.gather(*pending) if pending else []
        return [item for group in grouped for item in group]

    async def stop(self):
        """优雅关闭: 逆序停止"""
        self._running = False
        self._dispatch_state = "stopping"

        if self._http_server:
            await self._http_server.stop()

        async with self._platform_apply_lock:
            if self._dispatch_task and not self._dispatch_task.done():
                self._dispatch_task.cancel()
                try:
                    await self._dispatch_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._dispatch_state = "stopped"
            if self._scheduler is not None:
                await self._scheduler.wake_timers.close()

            if self._adapter_mgr:
                try:
                    await self._adapter_mgr.stop_all()
                    await self.platform_events.close()
                except Exception as e:
                    logger.warning(f"[BackendManager] 停止适配器失败: {e}")
                finally:
                    set_current_hub(None)
                    set_current_adapter_manager(None)

            for platform_id, (session_manager, _) in self._platform_runtimes.items():
                try:
                    session_manager.cleanup_idle_sessions(max_idle_seconds=0)
                except Exception as e:
                    logger.warning(f"[BackendManager] 清理会话失败: platform={platform_id}, 错误={e}")

            logger.info("[BackendManager] 已关闭")

    async def health(self) -> Dict[str, Any]:
        """
        健康检查

        返回:
        - Dict[str, Any]: 健康检查
        """
        adapters = {}
        if self._adapter_mgr:
            for aid, adapter in self._adapter_mgr._adapters.items():
                try:
                    adapters[aid] = adapter.get_stats()
                except Exception as e:
                    adapters[aid] = {
                        "status": "unknown",
                        "started": safe_getattr_bool(adapter, "started"),
                        "started_at": None,
                        "error_count": 1,
                        "last_error": str(e),
                        "config_id": aid,
                        "config_type": safe_getattr_str(safe_getattr(adapter, "config"), "type"),
                        "session_provider": safe_getattr_str(
                            safe_getattr(adapter, "config"),
                            "session_provider",
                            SESSION_CLASS_PROVIDER,
                        ),
                        "session_type": safe_getattr_str(
                            safe_getattr(adapter, "config"),
                            "session_type",
                        ),
                    }

        dispatch_task_running = self._dispatch_task is not None and not self._dispatch_task.done()
        dispatch_healthy = self._dispatcher is None or (
            self._dispatch_state == "running" and dispatch_task_running
        )
        adapters_errored = sorted(
            aid for aid, adapter in (self._adapter_mgr._adapters.items() if self._adapter_mgr else ())
            if isinstance(adapter, PlatformAdapter) and adapter.config.enable and adapter.status is PlatformStatus.ERROR
        )
        return {
            "running": self._running,
            "healthy": self._running and dispatch_healthy and not adapters_errored,
            "adapters_errored": adapters_errored,
            "pid": os.getpid(),
            "runtime_id": self._runtime_id,
            "model_config": self._model_cfg is not None,
            "session_class_config": self._session_cls_cfg is not None,
            "edictum_config": self._edictum_cfg is not None,
            "session_manager": self._session_mgr is not None,
            "user_manager": self._user_mgr is not None,
            "pipeline": self._scheduler is not None,
            "adapters": adapters,
            "platform_config": deepcopy(self._platform_config_results),
            "platform_events": dict(self.platform_events.stats),
            "platform_count": len(self._adapter_mgr.list_adapters()) if self._adapter_mgr else 0,
            "dispatch": {
                "status": self._dispatch_state,
                "healthy": dispatch_healthy,
                "restart_count": self._dispatch_restart_count,
                "last_error": self._dispatch_last_error,
            },
        }

    # ---------- 内部初始化 ----------

    def _init_model_config(self):
        self._model_cfg = ModelConfigManager(
            storage_path=self.config.model_config_path,
            asr_in_use_checker=self._check_asr_in_use,
            llm_in_use_checker=self._check_llm_in_use,
        )
        logger.info("[BackendManager] ModelConfigManager 就绪")

    def _check_asr_in_use(self, config_name: str) -> list[dict[str, str]]:
        """后端运行时的 ASR 引用扫描, 平台绑定取当前已加载配置"""
        from satrap.core.config.asr_references import list_asr_config_references

        return list_asr_config_references(
            config_name,
            platforms=list(self.config.platforms),
            layout=self._storage,
        )

    def _check_llm_in_use(self, config_name: str) -> list[dict[str, str]]:
        """模型删除或重命名前扫描全部当前平台的群覆盖"""
        from satrap.core.config.group_references import list_group_references

        return list_group_references(
            "llm", config_name, layout=self._storage,
            platform_ids=[str(item.get("id") or "") for item in self.config.platforms],
        )

    def group_resource_references(self, target: str, name: str) -> list[dict[str, str]]:
        """命名会话配置变更前扫描群绑定"""
        from satrap.core.config.group_references import list_group_references

        return list_group_references(
            target, name, layout=self._storage,
            platform_ids=[str(item.get("id") or "") for item in self.config.platforms],
        )

    def _init_session_class_config(self):
        self._session_cls_cfg = SessionClassConfigManager(
            storage_path=self.config.session_class_config_path,
            session_scan_paths=self.config.session_scan_paths,
        )
        # 注册配置中声明的 session 类
        for name, class_path in self.config.session_classes.items():
            try:
                self._session_cls_cfg.register_by_class_path(name, class_path)
            except Exception as e:
                logger.error(f"[BackendManager] 注册 session 类失败: {name}={class_path}, 错误: {e}")
        logger.info("[BackendManager] SessionClassConfigManager 就绪")

    def _init_edictum_config(self) -> None:
        """初始化 Edictum 类型注册表和冷配置管理器"""
        if self._edictum_types is None:
            self._edictum_types = create_default_edictum_type_registry()
        self._edictum_cfg = EdictumConfigManager(
            self._edictum_types,
            storage_path=self.config.edictum_config_path,
        )
        logger.info("[BackendManager] EdictumConfigManager 就绪")

    def _init_session_manager(self):
        self._session_mgr = self._create_session_manager(LOCAL_PLATFORM_ID)
        logger.info("[BackendManager] local SessionManager 就绪")

    def _create_session_manager(self, platform_id: str) -> SessionManager:
        """
        创建绑定到单个平台数据库的 SessionManager

        参数:
        - platform_id: 平台实例 ID

        返回:
        - SessionManager: 已完成 Provider 和会话类注册的管理器
        """
        database = str(self._storage.platform_db(platform_id))
        self._storage.ensure_platform(platform_id)
        manager = SessionManager(
            default_session_type=self.config.default_session_type,
            max_size=self.config.max_sessions,
            idle_timeout=self.config.idle_timeout,
            db_path=database,
            default_checkpoint=self.config.session_checkpoint,
            default_checkpoint_db=database,
            platform_id=platform_id,
            storage_layout=self._storage,
        )
        # 关联 SessionClassConfigManager 用于启用检查
        manager.class_cfg_mgr = self._session_cls_cfg
        # 关联 ModelConfigManager 用于会话内按名称查找 LLM 配置
        manager.model_config_manager = self._model_cfg
        if self._edictum_cfg is not None and self._edictum_types is not None:
            manager.register_provider(
                EdictumProvider(
                    self._edictum_cfg,
                    self._edictum_types,
                    default_checkpoint=self.config.session_checkpoint,
                    default_checkpoint_db=database,
                )
            )
        # 将 SessionClassConfigManager 中所有已注册的 class 同步到 SessionRegistry
        if self._session_cls_cfg:
            for name in self._session_cls_cfg.list_configs():
                try:
                    cls = self._session_cls_cfg.get_class(name)
                    manager.registry.register(name, cls)
                except Exception as e:
                    logger.warning(f"[BackendManager] 同步 session 类到注册表失败: {name}, 错误={e}")
        return manager

    def _init_user_manager(self):
        if self._session_mgr is None:
            raise RuntimeError("SessionManager 未初始化")
        self._user_mgr = UserManager(
            session_manager=self._session_mgr,
            db_path=self._storage.platform_db(LOCAL_PLATFORM_ID),
            platform_id=LOCAL_PLATFORM_ID,
            storage_layout=self._storage,
        )
        self._session_mgr.user_manager = self._user_mgr
        self._platform_runtimes[LOCAL_PLATFORM_ID] = (self._session_mgr, self._user_mgr)
        logger.info("[BackendManager] local UserManager 就绪")

    def _ensure_platform_runtime(self, platform_id: str) -> tuple[SessionManager, UserManager]:
        """
        获取或创建平台实例专属运行时

        参数:
        - platform_id: 平台实例 ID

        返回:
        - tuple[SessionManager, UserManager]: 会话和用户管理器
        """
        existed = self._platform_runtimes.get(platform_id)
        if existed is not None:
            return existed
        session_manager = self._create_session_manager(platform_id)
        user_manager = UserManager(
            session_manager=session_manager,
            db_path=self._storage.platform_db(platform_id),
            platform_id=platform_id,
            storage_layout=self._storage,
        )
        session_manager.user_manager = user_manager
        runtime = (session_manager, user_manager)
        self._platform_runtimes[platform_id] = runtime
        logger.info(f"[BackendManager] 平台数据运行时就绪: {platform_id}")
        return runtime

    def _init_pipeline(self):
        if self._session_mgr is None:
            raise RuntimeError("SessionManager 未初始化")

        self._rate_limiter = RateLimiter(
            rate=self.config.rate_limit,
            burst=self.config.rate_burst,
        )
        self._scheduler = PipelineScheduler(
            session_manager=self._session_mgr,
            rate_limiter=self._rate_limiter,
            llm_timeout=self.config.llm_timeout,
            error_feedback=self.config.error_feedback,
            user_manager=self._user_mgr,
        )
        self._manual_wake_store = ManualWakeStore(self._storage.root / "manual_wake_store.json")
        self._scheduler.manual_wake_store = self._manual_wake_store
        if self._manual_wake_store.degraded:
            logger.warning("[BackendManager] 手动唤醒状态存储已降级, 新手动请求将被拒绝, 其余功能照常")
        self._scheduler.set_platform_runtimes(self._platform_runtimes)
        self._scheduler.asr_resolver = asr_resolver_from_manager(self._model_cfg)
        logger.info("[BackendManager] PipelineScheduler + RateLimiter + UserManager 就绪")

    def _attach_send_attempt_recorder(self, adapter: PlatformAdapter) -> None:
        """
        给支持发送尝试记录的适配器注入状态存储

        参数:
        - adapter: 平台适配器实例
        """
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        if isinstance(adapter, OneBotAdapter) and self._manual_wake_store is not None:
            adapter.set_send_attempt_recorder(self._manual_wake_store)

    def _attach_request_ledger(self, adapter: PlatformAdapter) -> None:
        """
        给支持审批动作的适配器注入跨重启的审批身份账本

        参数:
        - adapter: 平台适配器实例

        账本按数据目录共用一份, 以适配器 ID 与已绑定账号区分条目; 注入失败时保持
        适配器自带的仅进程内账本, 审批不会因此放宽
        """
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        if not isinstance(adapter, OneBotAdapter):
            return
        if self._request_ledger is None:
            self._request_ledger = RequestApprovalLedger(self._storage.root / "request_ledger.json")
            if self._request_ledger.degraded:
                logger.warning(
                    f"[BackendManager] 审批账本已降级, request 审批将拒绝执行: {self._request_ledger.degraded_reason}"
                )
        adapter.set_request_ledger(self._request_ledger)

    def _resolve_platform_session_type(
        self,
        platform_type: str,
        configured_session_type: str,
        session_provider: str = SESSION_CLASS_PROVIDER,
    ) -> str:
        """
        解析平台实例应使用的会话类配置名称

        参数:
        - platform_type: 平台适配器类型
        - configured_session_type: 平台实例显式绑定的会话类名称
        - session_provider: 平台实例绑定的会话 Provider 名称

        返回:
        - str: 显式绑定, 同名会话类或全局默认会话类
        """
        explicit = configured_session_type.strip()
        if explicit:
            return explicit
        if (
            session_provider == SESSION_CLASS_PROVIDER
            and self._session_cls_cfg
            and self._session_cls_cfg.has_config(platform_type)
        ):
            return platform_type
        return self.config.default_session_type

    def _init_platform(self, pid: str, ptype: str, pcfg: dict[str, Any]) -> None:
        """
        创建单个平台适配器, 配置或构造异常抛给调用方隔离处理

        参数:
        - pid: 平台 ID
        - ptype: 平台类型
        - pcfg: 原始平台配置
        """
        from satrap.core.platform.onebot.adapter import OneBotAdapter

        if self._adapter_mgr is None:
            raise RuntimeError("平台运行时未初始化")
        session_provider = str(pcfg.get("session_provider", SESSION_CLASS_PROVIDER)).strip() or SESSION_CLASS_PROVIDER
        configured_session_type = str(pcfg.get("session_type", "")).strip()
        raw_settings = pcfg.get("settings", {})
        if not isinstance(raw_settings, dict):
            raise ValueError("settings 必须是对象")
        settings = dict(cast(dict[str, Any], raw_settings))
        platform_session_manager, _ = self._ensure_platform_runtime(pid)
        platform_session_manager.plugin_environment = PluginEnvironment("platform", ptype)
        session_type = self._resolve_platform_session_type(ptype, configured_session_type, session_provider)
        resolved = platform_session_manager.provider_registry.resolve_definition(session_type, session_provider)
        if resolved is None or not resolved[1].enabled:
            raise ValueError(f"会话定义不可用 provider={session_provider}, name={session_type}")
        platform_config = PlatformConfig(
            id=pid, type=ptype, session_provider=session_provider, session_type=session_type,
            enable=bool(pcfg.get("enable", True)), settings=settings,
        )
        adapter = self._adapter_mgr.add_adapter(platform_config, event_handler=self.platform_events)
        if adapter is None:
            raise ValueError("平台类型不可用")
        if isinstance(adapter, OneBotAdapter):
            adapter.set_group_access_store(GroupDirectoryStore(self._storage.platform_db(pid)))
            adapter.set_group_sync_handler(lambda: self.trigger_group_sync(pid, adapter.bot_self_id))
            adapter.group_action_handler = lambda group_id, action_type, params: self.submit_group_action(
                pid, adapter.bot_self_id, group_id, secrets.token_hex(16),
                action_type, params, actor_kind="model",
            )
        self._attach_send_attempt_recorder(adapter)
        self._attach_request_ledger(adapter)
        self._platform_active_configs[pid] = self._normalized_platform_snapshot(pcfg)
        logger.info(f"[BackendManager] 已创建平台适配器: {pid} ({ptype})")

    async def _init_platforms(self) -> None:
        """创建并启动配置中的平台适配器"""
        try:
            import satrap.core.platform.misskey.adapter   # noqa: F401
            # 按配置启用时导入适配器, 触发注册装饰器
        except ImportError:
            pass
        # 导入已有平台适配器模块, 触发 @register_platform_adapter 装饰器
        try:
            import satrap.core.platform.onebot.adapter   # noqa: F401
            # 按配置启用时导入适配器, 触发注册装饰器
        except ImportError:
            pass

        self._adapter_mgr = PlatformAdapterManager(registry=global_registry)
        if self.platform_events.closed:
            self.platform_events = PlatformEventHub()
        self._group_events = GroupEventBuffer()
        self.platform_events.subscribe("*", self._group_events.append)
        set_current_hub(self.platform_events)
        set_current_adapter_manager(self._adapter_mgr)

        init_failures: list[dict[str, Any]] = []
        for pcfg in self.config.platforms:
            if not isinstance(pcfg, dict):
                logger.warning(f"[BackendManager] 跳过无效平台配置: 条目类型 {type(pcfg).__name__}")
                continue
            pid = str(pcfg.get("id", ""))
            ptype = str(pcfg.get("type", ""))
            if not pid or not ptype:
                logger.warning(f"[BackendManager] 跳过无效平台配置: id={pid!r} type={ptype!r}")
                continue
            try:
                self._init_platform(pid, ptype, pcfg)
            except Exception as error:
                logger.error(f"[BackendManager] 平台 {pid} ({ptype}) 初始化失败: {type(error).__name__}: {error}")
                init_failures.append({
                    "id": pid, "saved_revision": self._platform_revision(self._normalized_platform_snapshot(pcfg)),
                    "active_revision": None, "status": "failed", "error": f"平台初始化失败: {type(error).__name__}: {error}",
                })
        if init_failures:
            self._platform_config_results = init_failures

        if self._scheduler and self._adapter_mgr:
            self._scheduler.set_platform_runtimes(self._platform_runtimes)

        if self._adapter_mgr:
            await self._adapter_mgr.start_all()
            # 等待全部已启用适配器完成启动

        if self._adapter_mgr and self._scheduler:
            self._dispatcher = EventDispatcher(
                manager=self._adapter_mgr,
                scheduler=self._scheduler,
            )
            self._running = True
            self._dispatch_state = "running"
            self._dispatch_task = asyncio.create_task(self._dispatch_loop())
            logger.info("[BackendManager] EventDispatcher 已启动")
        # 启动事件分发

    async def _init_http_api(self):
        """启动内嵌 HTTP API 服务器"""
        self._http_server = BackendHTTPServer(
            self,
            host=self.config.api_host,
            port=self.config.api_port,
        )
        await self._http_server.start()
        logger.info(f"[BackendManager] HTTP API 服务: http://{self.config.api_host}:{self.config.api_port}")

    async def _dispatch_loop(self) -> None:
        """监督事件分发循环并在异常退出后自动重启"""
        restart_delay = self._dispatch_restart_base_delay
        while self._running and self._dispatcher is not None:
            self._dispatch_state = "running"
            try:
                await self._dispatcher.dispatch_loop()
                if not self._running:
                    break
                raise RuntimeError("事件分发循环意外退出")
            except asyncio.CancelledError:
                self._dispatch_state = "stopped"
                raise
            except Exception as error:
                self._dispatch_last_error = str(error)
                self._dispatch_restart_count += 1
                self._dispatch_state = "restarting"
                logger.error(
                    f"[BackendManager] 事件分发循环异常, 将在 {restart_delay:.1f} 秒后重启: {error}"
                )
                await asyncio.sleep(restart_delay)
                restart_delay = min(
                    restart_delay * 2,
                    self._dispatch_restart_max_delay,
                )
        self._dispatch_state = "stopped"
