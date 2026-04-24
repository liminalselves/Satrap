from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Type, TypeVar

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from satrap.core.platform.event import (
        MessageChain,
        MessageEvent,
        MessageSession,
        PlatformMetadata,
    )

from satrap.core.log import logger
from satrap.core.type import Group, PlatformError, PlatformStatus


# 统一事件回调签名:
# - 输入: PlatformEvent
# - 输出: 可等待对象或 None (均可)
EventHandler = Callable[["PlatformEvent"], Awaitable[Any] | Any]


@dataclass
class PlatformConfig:
    """平台适配器配置
    参考 AstrBot 的适配器配置思想, 保留统一字段:
    - id: 适配器实例唯一标识
    - type: 适配器类型 (如 misskey / aiocqhttp / telegram)
    - enable: 是否启用
    - settings: 适配器专属配置
    """

    id: str = "default"
    type: str = ""
    enable: bool = True
    settings: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlatformEvent:
    """统一平台事件模型

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
    extras: Dict[str, Any] = field(default_factory=dict)


class PlatformAdapter(ABC):
    """平台适配器基类
    平台实现建议:
    1. 继承此类并实现 `start/stop`
    2. 收到平台消息后构造 PlatformEvent 并调用 `emit_event`
    3. 需要发送消息时实现 `send_text`
    """

    adapter_type: str = ""

    def __init__(self, config: PlatformConfig, event_handler: EventHandler | None = None, event_queue: asyncio.Queue | None = None):
        self.config = config
        self.event_handler = event_handler
        self.started = False

        self.client_self_id = uuid.uuid4().hex
        self._event_queue = event_queue or asyncio.Queue()
        self._status: PlatformStatus = PlatformStatus.PENDING
        self._errors: list[PlatformError] = []
        self._started_at: datetime | None = None
        self._run_task: asyncio.Task | None = None

    def set_event_handler(self, handler: EventHandler | None):
        """设置/替换事件回调函数

        参数:
        - handler: 事件处理函数, 输入为 PlatformEvent, 输出为可等待对象或 None
        """
        self.event_handler = handler

    async def emit_event(self, event: PlatformEvent):
        """向上层派发事件

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

    # ── Status management ──

    @property
    def status(self) -> PlatformStatus:
        """获取当前适配器状态"""
        return self._status

    @status.setter
    def status(self, value: PlatformStatus) -> None:
        """设置适配器状态

        参数:
        - value: 新状态
        """
        self._status = value

    @property
    def errors(self) -> list[PlatformError]:
        """获取适配器最近记录的错误列表"""
        return list(self._errors)

    @property
    def last_error(self) -> PlatformError | None:
        """获取适配器最近记录的错误"""
        return self._errors[-1] if self._errors else None

    def record_error(self, message: str, traceback_str: str | None = None) -> None:
        """记录适配器错误

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

    # ── Meta ──

    @abstractmethod
    def meta(self) -> PlatformMetadata:
        """获取适配器元数据"""
        ...

    # ── Lifecycle ──

    @abstractmethod
    async def run(self) -> None:
        """返回一个协程, 作为平台的主循环"""
        ...

    async def start(self) -> None:
        """启动平台, 创建 run 任务并设置状态"""
        self._run_task = asyncio.create_task(self.run())
        self.started = True
        self._status = PlatformStatus.RUNNING
        self._started_at = datetime.now()
        logger.info(
            f"[PlatformAdapter] 平台已启动: {self.config.id} "
            f"(task={id(self._run_task)})"
        )

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
        self._errors.clear()

    # ── Event queue ──

    def commit_event(self, event: MessageEvent) -> None:
        """提交 MessageEvent 到事件队列"""
        self._event_queue.put_nowait(event)

    # ── Client access ──

    def get_client(self) -> object:
        """获取平台客户端对象, 默认返回 None"""
        return None

    # ── Webhook ──

    async def webhook_callback(self, request: Any) -> Any:
        """Webhook 回调处理, 默认无操作"""
        return None

    def unified_webhook(self) -> bool:
        """是否统一 Webhook 模式, 默认 False"""
        return False

    # ── Stats ──

    def get_stats(self) -> dict:
        """获取平台运行统计信息"""
        return {
            "status": self._status.value,
            "started": self.started,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "error_count": len(self._errors),
            "last_error": str(self._errors[-1]) if self._errors else None,
            "client_self_id": self.client_self_id,
            "config_id": self.config.id,
            "config_type": self.config.type,
        }

    # ── Sending ──

    async def send_by_session(self, session: MessageSession, message_chain: MessageChain) -> None:
        """通过会话对象发送消息, 无需 event 引用

        参数:
        - session: 会话对象
        - message_chain: 要发送的消息链
        """
        await self.send_message(session.session_id, message_chain)

    async def send_text(self, session_id: str, text: str) -> Any:
        """发送文本消息

        默认不支持, 具体平台可覆写

        参数:
        - session_id: 会话 ID
        - text: 要发送的文本消息
        """
        logger.error(
            f"[PlatformAdapter] {self.__class__.__name__} 未实现 send_text()"
        )

    async def send_message(self, session_id: str, message: MessageChain) -> Any:
        """发送完整的消息链, 默认实现: 提取 Plain 组件拼接文本后调用 send_text

        参数:
        - session_id: 会话 ID
        - message: 要发送的消息链
        """
        parts = []
        for c in message:
            t = getattr(c, 'type', None)
            if t is not None and hasattr(t, 'value') and t.value.lower() == 'plain':
                parts.append(getattr(c, 'text', '') or '')
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
        """流式发送消息链

        默认实现: 迭代 generator 并逐条调用 send_message
        子类可覆写以支持真正的流式推送, 此时可借助 use_fallback 决定降级策略

        参数:
        - session_id: 会话 ID
        - generator: 消息链异步生成器
        - use_fallback: 是否使用降级策略
        """
        async for chunk in generator:
            await self.send_message(session_id, chunk)

    async def send_typing(self, session_id: str) -> None:
        """发送"输入中"状态, 默认无操作"""

    async def stop_typing(self, session_id: str) -> None:
        """停止"输入中"状态, 默认无操作"""

    async def react(self, session_id: str, emoji: str) -> None:
        """对消息添加表情回应, 默认无操作"""
        await self.send_text(session_id, emoji)

    async def get_group(self, group_id: str | None = None) -> Group | None:
        """获取群聊信息, 默认返回 None"""
        return None


TAdapter = TypeVar("TAdapter", bound=PlatformAdapter)


class PlatformAdapterRegistry:
    """平台适配器注册表"""

    def __init__(self):
        self._mapping: Dict[str, Type[PlatformAdapter]] = {}

    def register(self, adapter_type: str, adapter_cls: Type[TAdapter]):
        """注册适配器类型

        参数:
        - adapter_type: 适配器类型字符串
        - adapter_cls: 适配器类
        """
        key = (adapter_type or "").strip().lower()
        if not key:
            logger.error("[PlatformAdapterRegistry] 注册失败: adapter_type 不能为空")
            return
        if not issubclass(adapter_cls, PlatformAdapter):
            logger.error(
                f"[PlatformAdapterRegistry] 注册失败: {adapter_cls.__name__} 必须继承 PlatformAdapter"
            )
            return

        self._mapping[key] = adapter_cls
        logger.info(f"[PlatformAdapterRegistry] 已注册平台适配器: {key} -> {adapter_cls.__name__}")

    def unregister(self, adapter_type: str):
        """注销适配器类型

        参数:
        - adapter_type: 适配器类型字符串
        """
        key = (adapter_type or "").strip().lower()
        self._mapping.pop(key, None)

    def get(self, adapter_type: str) -> Optional[Type[PlatformAdapter]]:
        """获取适配器类

        参数:
        - adapter_type: 适配器类型字符串
        """
        key = (adapter_type or "").strip().lower()
        return self._mapping.get((adapter_type or "").strip().lower())

    def list_types(self) -> List[str]:
        """列出所有已注册适配器类型"""
        return sorted(self._mapping.keys())

    def create(self, config: PlatformConfig, event_handler: EventHandler | None = None) -> Optional[PlatformAdapter]:
        """根据配置实例化适配器

        参数:
        - config: 平台配置
        - event_handler: 事件处理函数
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
    """平台适配器管理器

    管理多个平台实例的生命周期 (创建, 启动, 停止, 删除)
    """

    def __init__(self, registry: PlatformAdapterRegistry | None = None):
        self.registry = registry or PlatformAdapterRegistry()
        self._adapters: Dict[str, PlatformAdapter] = {}

    def add_adapter(self, config: PlatformConfig, event_handler: EventHandler | None = None) -> Optional[PlatformAdapter]:
        """添加一个适配器实例 (按 config.id 唯一)

        参数:
        - config: 平台配置
        - event_handler: 事件处理函数
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
        """获取适配器实例

        参数:
        - adapter_id: 适配器实例 id
        """
        return self._adapters.get((adapter_id or "").strip())

    def remove_adapter(self, adapter_id: str) -> Optional[PlatformAdapter]:
        """移除适配器实例 (仅移除, 不自动 stop)

        参数:
        - adapter_id: 适配器实例 id
        """
        return self._adapters.pop((adapter_id or "").strip(), None)

    def list_adapters(self) -> List[str]:
        """列出当前适配器实例 id"""
        return sorted(self._adapters.keys())

    async def start_adapter(self, adapter_id: str):
        """启动指定适配器

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
        """停止指定适配器

        参数:
        - adapter_id: 适配器实例 id
        """
        adapter = self.get_adapter(adapter_id)
        if adapter is None:
            return
        if not adapter.started:
            return
        await adapter.stop()

    async def start_all(self):
        """启动所有启用的适配器"""
        for adapter in self._adapters.values():
            if not adapter.started and adapter.config.enable:
                await adapter.start()

    async def stop_all(self):
        """停止所有已启动适配器"""
        for adapter in self._adapters.values():
            if adapter.started:
                await adapter.stop()


class EventDispatcher:
    """事件分发器

    轮询所有适配器的事件队列, 将 MessageEvent 分发给对应的处理器
    """

    def __init__(self, manager: PlatformAdapterManager):
        self.manager = manager

    async def dispatch_loop(self) -> None:
        """主循环: 从所有适配器队列中拉取事件并处理"""
        while True:
            for adapter in self.manager._adapters.values():
                while not adapter._event_queue.empty():
                    event = adapter._event_queue.get_nowait()
                    await self._process_event(event)
            await asyncio.sleep(0.01)

    async def _process_event(self, event: MessageEvent) -> None:
        """处理单个 MessageEvent (占位实现, 后续接入插件管道/LLM 工作流)

        参数:
        - event: 要处理的事件
        """
        logger.debug(
            f"[EventDispatcher] 收到事件: session={event.unified_msg_origin}, "
            f"message={event.get_message_str()!r}"
        )


# 全局注册表与装饰器, 便于平台实现快速注册
registry = PlatformAdapterRegistry()


def register_platform_adapter(adapter_type: str):
    """平台适配器注册装饰器

    示例:
    ```python
    @register_platform_adapter("misskey")
    class MisskeyAdapter(PlatformAdapter):
        ...
    ```
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

