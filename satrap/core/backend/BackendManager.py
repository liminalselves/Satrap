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
                record = await asyncio.to_thread(store.lookup_request, request_id, str(adapter_id))
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
                    await asyncio.to_thread(store.update_request, str(adapter_id), request_id, "failed", "queue_full")
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
        record = await asyncio.to_thread(store.lookup_request, request_id, adapter_id)
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
                    runtime_usable = not self._running or not adapter.config.enable or (adapter.started and adapter._run_task is not None and not adapter._run_task.done())
                    if runtime_usable and previous == proposed and (not changed or (candidate["type"] in {"onebot", "aiocqhttp"} and changed <= hot_keys)):
                        adapter.config = replace(adapter.config, settings=deepcopy(candidate.get("settings", {})))
                        if self._scheduler is not None and old_settings != new_settings:
                            self._scheduler.wake_window.clear_adapter(platform_id)
                            self._scheduler.wake_timers.clear_adapter(platform_id)
                            self._scheduler.clear_manual_wakes(platform_id)
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
            self._attach_send_attempt_recorder(replacement)
            self._attach_request_ledger(replacement)
        try:
            if old is not None:
                old.config = replace(old.config, enable=False)
            await dispatcher.detach_adapter(platform_id)
            if self._scheduler is not None:
                self._scheduler.wake_window.clear_adapter(platform_id)
                self._scheduler.wake_timers.clear_adapter(platform_id)
                self._scheduler.clear_manual_wakes(platform_id)
            if old is not None:
                await old.terminate()
            if replacement is not None:
                if replacement.config.enable:
                    await replacement.start()
                    await replacement.wait_ready()
                manager._adapters[platform_id] = replacement
                self._platform_runtimes[platform_id][0].plugin_environment = PluginEnvironment("platform", replacement.config.type)
                await dispatcher.attach_adapter(replacement)
            else:
                manager._adapters.pop(platform_id, None)
            if self._scheduler is not None:
                self._scheduler.set_platform_runtimes(self._platform_runtimes)
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
