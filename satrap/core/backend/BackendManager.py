"""
Satrap 后端服务组件的统一编排器

集中构建模型配置, 会话, 用户, 请求管线和平台适配器等管理组件,
负责后端的启动, 停止, 配置热重载与运行状态汇总
"""
from __future__ import annotations
from satrap.edictum.plugin_compatibility import PluginEnvironment

from dataclasses import dataclass, field
import asyncio
from pathlib import Path
import secrets
import signal
from typing import Any, Awaitable, Dict, List, Optional
import os

from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.UserManager import UserManager
from satrap.core.pipeline.rate_limiter import RateLimiter
from satrap.core.framework.providers import EdictumProvider, SESSION_CLASS_PROVIDER
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.edictum.registry import (
    EDICTUM_PROVIDER,
    EdictumTypeRegistry,
    create_default_edictum_type_registry,
)
from satrap.edictum.config import EdictumConfigManager
from satrap.core.platform import (
    EventDispatcher,
    PlatformAdapterManager,
    PlatformAdapterRegistry,
    PlatformConfig,
    registry as global_registry,
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
        self._adapter_mgr: PlatformAdapterManager | None = None
        self._dispatcher: EventDispatcher | None = None

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

    async def reload_config(self) -> dict[str, Any]:
        """
        热加载模型, 会话类和 Edictum 冷配置

        返回:
        - dict[str, Any]: Edictum 活跃会话插件同步结果
        """
        if self._model_cfg:
            self._model_cfg.reload()
        if self._session_cls_cfg:
            self._session_cls_cfg.reload()
        if self._edictum_cfg:
            self._edictum_cfg.reload()
        edictum_results = await self.reconcile_edictum_runtime_async()
        for session_manager, _ in self._platform_runtimes.values():
            await session_manager.reload_model_configs_async()
        logger.info("[BackendManager] 配置已重载")
        return {
            "ok": all(item.get("ok", False) for item in edictum_results),
            "edictum_sessions": edictum_results,
        }

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

        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except (asyncio.CancelledError, Exception):
                pass
        self._dispatch_state = "stopped"

        if self._adapter_mgr:
            try:
                await self._adapter_mgr.stop_all()
            except Exception as e:
                logger.warning(f"[BackendManager] 停止适配器失败: {e}")

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
        return {
            "running": self._running,
            "healthy": self._running and dispatch_healthy,
            "pid": os.getpid(),
            "runtime_id": self._runtime_id,
            "model_config": self._model_cfg is not None,
            "session_class_config": self._session_cls_cfg is not None,
            "edictum_config": self._edictum_cfg is not None,
            "session_manager": self._session_mgr is not None,
            "user_manager": self._user_mgr is not None,
            "pipeline": self._scheduler is not None,
            "adapters": adapters,
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
        )
        logger.info("[BackendManager] ModelConfigManager 就绪")

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
        self._scheduler.set_platform_runtimes(self._platform_runtimes)
        logger.info("[BackendManager] PipelineScheduler + RateLimiter + UserManager 就绪")

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

        for pcfg in self.config.platforms:
            pid = str(pcfg.get("id", ""))
            ptype = str(pcfg.get("type", ""))
            session_provider = str(
                pcfg.get("session_provider", SESSION_CLASS_PROVIDER)
            ).strip() or SESSION_CLASS_PROVIDER
            configured_session_type = str(pcfg.get("session_type", "")).strip()
            settings = dict(pcfg.get("settings", {}))
            if not pid or not ptype:
                logger.warning(f"[BackendManager] 跳过无效平台配置: {pcfg}")
                continue

            platform_session_manager, _ = self._ensure_platform_runtime(pid)
            platform_session_manager.plugin_environment = PluginEnvironment("platform", ptype)

            session_type = self._resolve_platform_session_type(
                ptype,
                configured_session_type,
                session_provider,
            )

            if platform_session_manager is not None:
                try:
                    resolved = platform_session_manager.provider_registry.resolve_definition(
                        session_type,
                        session_provider,
                    )
                except ValueError as error:
                    logger.error(
                        f"[BackendManager] 跳过平台配置 {pid}: {error}"
                    )
                    continue
                if resolved is None or not resolved[1].enabled:
                    logger.error(
                        f"[BackendManager] 跳过平台配置 {pid}: 会话定义不可用 "
                        f"provider={session_provider}, name={session_type}"
                    )
                    continue

            platform_config = PlatformConfig(
                id=pid,
                type=ptype,
                session_provider=session_provider,
                session_type=session_type,
                enable=True,
                settings=settings,
            )
            adapter = self._adapter_mgr.add_adapter(platform_config)
            if adapter:
                logger.info(f"[BackendManager] 已创建平台适配器: {pid} ({ptype})")
            else:
                logger.error(f"[BackendManager] 创建平台适配器失败: {pid} ({ptype})")

        if self._scheduler and self._adapter_mgr:
            self._scheduler.set_adapter_ids(set(self._adapter_mgr.list_adapters()))
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
