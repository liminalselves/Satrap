"""平台适配器协议, 注册表与运行时管理器"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import asyncio
import inspect
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Type, TypeVar
import time
import uuid
from abc import ABC, abstractmethod

from satrap.core.framework.providers.base import SESSION_CLASS_PROVIDER
from satrap.core.config.platform_policy import validate_event_limits
from satrap.core.type import Group, PlatformError, PlatformStatus, safe_getattr, safe_getattr_str

from satrap.core.log import logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from satrap.core.platform.event import (
        MessageChain,
        MessageEvent,
        MessageSession,
        PlatformMetadata,
    )
    from satrap.core.pipeline.scheduler import PipelineScheduler


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
    - enable: 是否启用
    - settings: 适配器专属配置
    """

    id: str = "default"
    type: str = ""
    enable: bool = True
    settings: Dict[str, Any] = field(default_factory=dict[str, Any])
    session_provider: str = SESSION_CLASS_PROVIDER
    session_type: str = ""


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

    def __init__(self, config: PlatformConfig, event_handler: EventHandler | None = None, event_queue: asyncio.Queue[Any] | None = None):
        """
        初始化 PlatformAdapter

        参数:
        - config: 配置信息
        - event_handler: 事件处理器
        - event_queue: 事件队列
        """
        self.config = config
        self.event_handler = event_handler
        self.started = False

        self.client_self_id = uuid.uuid4().hex
        validate_event_limits(config.settings)
        capacity = int(config.settings.get("event_queue_capacity", 256))
        if capacity <= 0:
            raise ValueError("event_queue_capacity 必须大于 0")
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

    async def send_message(self, session_id: str, message: MessageChain, *, request_id: str = "") -> Any:
        """
        发送完整的消息链, 默认实现: 提取 Plain 组件拼接文本后调用 send_text

        参数:
        - session_id: 会话 ID
        - message: 要发送的消息链
        - request_id: 可选的逻辑请求标识, 支持发送尝试记录的平台据此关联手动请求

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

