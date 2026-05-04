from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.UserManager import UserManager
from satrap.core.log import logger
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.pipeline.rate_limiter import RateLimiter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform import (
    EventDispatcher,
    PlatformAdapterManager,
    PlatformAdapterRegistry,
    PlatformConfig,
)


@dataclass
class BackendConfig:
    """后端统一配置

    所有路径为 None 时使用对应 Manager 的默认路径。
    """

    # 存储路径
    model_config_path: str | None = None
    session_class_config_path: str | None = None
    session_db_path: str | None = None
    user_db_path: str | None = None

    # SessionManager
    default_session_type: str = "default"
    max_sessions: int = 1000
    idle_timeout: int = 3600

    # Pipeline
    rate_limit: float = 1.0
    rate_burst: int = 5
    llm_timeout: float = 120.0
    error_feedback: bool = True

    # Session 类注册 (name -> class_path)
    session_classes: Dict[str, str] = field(default_factory=dict)

    # HTTP API
    api_host: str = "127.0.0.1"
    api_port: int = 19870

    # 平台适配器配置
    platforms: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> BackendConfig:
        """从字典加载配置"""
        return cls(
            model_config_path=data.get("model_config_path"),
            session_class_config_path=data.get("session_class_config_path"),
            session_db_path=data.get("session_db_path"),
            user_db_path=data.get("user_db_path"),
            default_session_type=data.get("default_session_type", "default"),
            max_sessions=int(data.get("max_sessions", 1000)),
            idle_timeout=int(data.get("idle_timeout", 3600)),
            rate_limit=float(data.get("rate_limit", 1.0)),
            rate_burst=int(data.get("rate_burst", 5)),
            llm_timeout=float(data.get("llm_timeout", 120.0)),
            error_feedback=bool(data.get("error_feedback", True)),
            session_classes=dict(data.get("session_classes", {})),
            api_host=str(data.get("api", {}).get("host", data.get("api_host", "127.0.0.1"))),
            api_port=int(data.get("api", {}).get("port", data.get("api_port", 19870))),
            platforms=list(data.get("platforms", [])),
        )


class BackendManager:
    """后端统一编排层

    管理所有子组件的生命周期: 初始化 -> start -> stop.
    依赖顺序:
      ModelConfigManager
        -> SessionClassConfigManager
          -> SessionManager -> UserManager
            -> PipelineScheduler -> RateLimiter
              -> PlatformAdapterManager -> EventDispatcher
    """

    def __init__(self, config: BackendConfig | None = None):
        self.config = config or BackendConfig()

        # 子管理器 (按依赖顺序)
        self._model_cfg: ModelConfigManager | None = None
        self._session_cls_cfg: SessionClassConfigManager | None = None
        self._session_mgr: SessionManager | None = None
        self._user_mgr: UserManager | None = None
        self._rate_limiter: RateLimiter | None = None
        self._scheduler: PipelineScheduler | None = None
        self._adapter_mgr: PlatformAdapterManager | None = None
        self._dispatcher: EventDispatcher | None = None

        self._http_server: BackendHTTPServer | None = None
        self._dispatch_task: asyncio.Task | None = None
        self._running = False

    # ── 属性访问 ──

    @property
    def model_config_manager(self) -> ModelConfigManager | None:
        return self._model_cfg

    @property
    def session_class_mgr(self) -> SessionClassConfigManager | None:
        return self._session_cls_cfg

    @property
    def session_manager(self) -> SessionManager | None:
        return self._session_mgr

    @property
    def user_manager(self) -> UserManager | None:
        return self._user_mgr

    @property
    def scheduler(self) -> PipelineScheduler | None:
        return self._scheduler

    @property
    def adapter_manager(self) -> PlatformAdapterManager | None:
        return self._adapter_mgr

    # ── 启动 ──

    async def start(self):
        """完整启动流程"""
        try:
            self._init_model_config()
            self._init_session_class_config()
            self._init_session_manager()
            self._init_user_manager()
            self._init_pipeline()
            self._init_platforms()
            self._init_http_api()
            logger.info("[BackendManager] 所有组件初始化完成")
        except Exception as e:
            logger.error(f"[BackendManager] 启动失败: {e}")
            await self.stop()
            raise

    async def reload_config(self):
        """热加载配置: ModelConfigManager + SessionClassConfigManager 重新读盘"""
        if self._model_cfg:
            self._model_cfg.reload()
        if self._session_cls_cfg:
            self._session_cls_cfg.reload()
        logger.info("[BackendManager] 配置已重载")

    async def stop(self):
        """优雅关闭: 逆序停止"""
        self._running = False

        if self._http_server:
            await self._http_server.stop()

        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._adapter_mgr:
            try:
                await self._adapter_mgr.stop_all()
            except Exception as e:
                logger.warning(f"[BackendManager] 停止适配器失败: {e}")

        if self._session_mgr:
            try:
                self._session_mgr.cleanup_idle_sessions(max_idle_seconds=0)
            except Exception as e:
                logger.warning(f"[BackendManager] 清理会话失败: {e}")

        logger.info("[BackendManager] 已关闭")

    async def health(self) -> Dict[str, Any]:
        """健康检查"""
        return {
            "running": self._running,
            "model_config": self._model_cfg is not None,
            "session_class_config": self._session_cls_cfg is not None,
            "session_manager": self._session_mgr is not None,
            "user_manager": self._user_mgr is not None,
            "pipeline": self._scheduler is not None,
            "adapters": {
                aid: {
                    "status": a.status.value if hasattr(a, 'status') else 'unknown',
                    "started": a.started,
                }
                for aid, a in (self._adapter_mgr._adapters.items() if self._adapter_mgr else {}).items()
            },
            "platform_count": len(self._adapter_mgr.list_adapters()) if self._adapter_mgr else 0,
        }

    # ── 内部初始化 ──

    def _init_model_config(self):
        self._model_cfg = ModelConfigManager(
            storage_path=self.config.model_config_path,
        )
        logger.info("[BackendManager] ModelConfigManager 就绪")

    def _init_session_class_config(self):
        from satrap.core.framework.SessionClassManager import SessionClassConfigManager as _SCCM

        self._session_cls_cfg = _SCCM(
            storage_path=self.config.session_class_config_path,
        )
        # 注册配置中声明的 session 类
        for name, class_path in self.config.session_classes.items():
            try:
                cls = _SCCM._load_class(class_path)
                self._session_cls_cfg.register(name, cls)
            except Exception as e:
                logger.error(f"[BackendManager] 注册 session 类失败: {name}={class_path}, 错误: {e}")
        logger.info("[BackendManager] SessionClassConfigManager 就绪")

    def _init_session_manager(self):
        self._session_mgr = SessionManager(
            default_session_type=self.config.default_session_type,
            max_size=self.config.max_sessions,
            idle_timeout=self.config.idle_timeout,
            db_path=self.config.session_db_path,
        )
        # 关联 SessionClassConfigManager 用于启用检查
        self._session_mgr.class_cfg_mgr = self._session_cls_cfg
        # 关联 ModelConfigManager 用于会话内按名称查找 LLM 配置
        self._session_mgr.model_config_manager = self._model_cfg
        # 将 SessionClassConfigManager 中所有已注册的 class 同步到 SessionRegistry
        if self._session_cls_cfg:
            for name in self._session_cls_cfg.list_configs():
                try:
                    cls = self._session_cls_cfg.get_class(name)
                    self._session_mgr.registry.register(name, cls)
                except Exception as e:
                    logger.warning(f"[BackendManager] 同步 session 类到注册表失败: {name}, 错误={e}")
        logger.info("[BackendManager] SessionManager 就绪")

    def _init_user_manager(self):
        if self._session_mgr is None:
            raise RuntimeError("SessionManager 未初始化")
        self._user_mgr = UserManager(
            session_manager=self._session_mgr,
            db_path=self.config.user_db_path,
        )
        self._session_mgr.user_manager = self._user_mgr
        logger.info("[BackendManager] UserManager 就绪")

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
        logger.info("[BackendManager] PipelineScheduler + RateLimiter + UserManager 就绪")

    def _init_platforms(self):
        # 导入已有平台适配器模块, 触发 @register_platform_adapter 装饰器
        try:
            import satrap.core.platform.misskey.adapter  # noqa: F401
        except ImportError:
            pass

        from satrap.core.platform import registry as global_registry

        self._adapter_mgr = PlatformAdapterManager(registry=global_registry)

        for pcfg in self.config.platforms:
            pid = str(pcfg.get("id", ""))
            ptype = str(pcfg.get("type", ""))
            settings = dict(pcfg.get("settings", {}))
            if not pid or not ptype:
                logger.warning(f"[BackendManager] 跳过无效平台配置: {pcfg}")
                continue

            platform_config = PlatformConfig(
                id=pid,
                type=ptype,
                enable=True,
                settings=settings,
            )
            adapter = self._adapter_mgr.add_adapter(platform_config)
            if adapter:
                logger.info(f"[BackendManager] 已创建平台适配器: {pid} ({ptype})")
            else:
                logger.error(f"[BackendManager] 创建平台适配器失败: {pid} ({ptype})")

        # 启动适配器
        loop = asyncio.get_event_loop()
        if self._adapter_mgr:
            try:
                loop.run_until_complete(self._adapter_mgr.start_all())
            except RuntimeError:
                # 已在运行中的 loop 中, 创建 task
                asyncio.ensure_future(self._adapter_mgr.start_all())

        # 启动事件分发
        if self._adapter_mgr and self._scheduler:
            self._dispatcher = EventDispatcher(
                manager=self._adapter_mgr,
                scheduler=self._scheduler,
            )
            self._dispatch_task = asyncio.ensure_future(self._dispatch_loop())
            self._running = True
            logger.info("[BackendManager] EventDispatcher 已启动")

    def _init_http_api(self):
        """启动内嵌 HTTP API 服务器"""
        self._http_server = BackendHTTPServer(
            self,
            host=self.config.api_host,
            port=self.config.api_port,
        )
        asyncio.ensure_future(self._http_server.start())
        logger.info(f"[BackendManager] HTTP API 服务: http://{self.config.api_host}:{self.config.api_port}")

    async def _dispatch_loop(self):
        """包装 EventDispatcher.dispatch_loop() 以便异常捕获"""
        try:
            if self._dispatcher:
                await self._dispatcher.dispatch_loop()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[BackendManager] 事件分发循环异常: {e}")
