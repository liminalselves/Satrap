"""
平台通知与请求事件的订阅, 去重和轻量分发

PlatformEventHub 作为适配器 emit_event 的统一处理器: 按事件类型分发给显式注册的处理器,
用平台实例, 账号, 事件类型及稳定载荷字段构造有界 TTL 去重键, 在独立的小任务中执行处理器,
不排在会话模型调用之后; 默认不把任何通知转成模型请求
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, cast
import asyncio

from satrap.core.platform import PlatformEvent
from satrap.core.components.message import File

from satrap.core.log import logger

NoticeHandler = Callable[[PlatformEvent], Awaitable[Any] | Any]



@dataclass(frozen=True)
class NoticePayload:
    """OneBot notice/request 的类型化载荷, 由适配器边界收窄"""

    category: str
    """notice 或 request"""
    kind: str
    """notice_type 或 request_type"""
    sub_type: str = ""
    self_id: str = ""
    group_id: str = ""
    user_id: str = ""
    operator_id: str = ""
    target_id: str = ""
    message_id: str = ""
    flag: str = ""
    comment: str = ""
    duration: int = 0
    time: int = 0
    file: dict[str, Any] = field(default_factory=dict[str, Any])

    def dedup_key(self, platform_id: str) -> str:
        """
        构造技术性去重键, 不使用随机内部 ID

        参数:
        - platform_id: 适配器实例 ID

        返回:
        - str: 由实例, 账号, 类型及稳定字段组成的键
        """
        parts = (platform_id, self.self_id, self.category, self.kind, self.sub_type, self.group_id,
                 self.user_id, self.operator_id, self.target_id, self.message_id, self.flag, str(self.time))
        return "|".join(parts)


class PlatformEventHub:
    """按事件类型分发平台事件的有界处理中心"""

    def __init__(self, dedup_capacity: int = 4096, dedup_ttl: float = 120.0, max_inflight: int = 64) -> None:
        """
        初始化处理中心

        参数:
        - dedup_capacity: 去重键容量
        - dedup_ttl: 去重保留秒数
        - max_inflight: 同时执行中的处理任务上限, 超出则丢弃事件并计数
        """
        self._handlers: dict[str, list[NoticeHandler]] = {}
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self.dedup_capacity = dedup_capacity
        self.dedup_ttl = dedup_ttl
        self.max_inflight = max_inflight
        self.stats = {"received": 0, "duplicate": 0, "dropped": 0, "dispatched": 0, "failed": 0, "unsubscribed": 0}
        self.closed = False

    def subscribe(self, event_type: str, handler: NoticeHandler) -> Callable[[], None]:
        """
        注册处理器, `*` 表示订阅全部类型

        参数:
        - event_type: 形如 notice.group_increase, request.friend 或 *
        - handler: 同步或异步处理器

        返回:
        - Callable: 注销函数, 重复调用无副作用
        """
        self._handlers.setdefault(event_type, []).append(handler)

        def unsubscribe() -> None:
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)
            if not handlers:
                self._handlers.pop(event_type, None)
        return unsubscribe

    def handlers_for(self, event_type: str) -> list[NoticeHandler]:
        """
        返回匹配事件类型的处理器列表

        参数:
        - event_type: 完整事件类型

        返回:
        - list: 精确匹配加通配订阅, 保持注册顺序
        """
        return list(self._handlers.get(event_type, [])) + list(self._handlers.get("*", []))

    def __call__(self, event: PlatformEvent) -> None:
        """
        作为适配器 event_handler 接收事件, 去重后在独立任务中分发

        参数:
        - event: 适配器构造的平台事件, extras["payload"] 为 NoticePayload 时参与去重
        """
        self.stats["received"] += 1
        if self.closed:
            self.stats["dropped"] += 1
            return
        payload = event.extras.get("payload")
        if isinstance(payload, NoticePayload) and not self._accept(payload.dedup_key(event.platform_id)):
            self.stats["duplicate"] += 1
            return
        handlers = self.handlers_for(event.event_type)
        if not handlers:
            self.stats["unsubscribed"] += 1
            return
        if len(self._tasks) >= self.max_inflight:
            self.stats["dropped"] += 1
            logger.warning(f"[PlatformEventHub] 处理任务已满, 丢弃 {event.platform_id}:{event.event_type}")
            return
        task = asyncio.get_running_loop().create_task(self._dispatch(event, handlers))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _accept(self, key: str) -> bool:
        """
        记录去重键, 已存在且未过期时返回 False

        参数:
        - key: 去重键

        返回:
        - bool: 是否首次出现
        """
        now = monotonic()
        while self._seen and next(iter(self._seen.values())) <= now:
            self._seen.popitem(last=False)
        if key in self._seen:
            return False
        self._seen[key] = now + self.dedup_ttl
        while len(self._seen) > self.dedup_capacity:
            self._seen.popitem(last=False)
        return True

    async def _dispatch(self, event: PlatformEvent, handlers: list[NoticeHandler]) -> None:
        """
        顺序调用处理器并隔离异常

        参数:
        - event: 平台事件
        - handlers: 匹配的处理器
        """
        for handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                    await result
                self.stats["dispatched"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.stats["failed"] += 1
                logger.error(f"[PlatformEventHub] 处理 {event.event_type} 失败: {type(error).__name__}")

    async def close(self) -> None:
        """拒绝新事件并等待执行中的处理任务取消完成"""
        self.closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._seen.clear()


def _text(value: object) -> str:
    """
    把 OneBot 数值/字符串字段收窄为字符串

    参数:
    - value: 原始字段

    返回:
    - str: 非 bool 的 int/str 转为字符串, 其他为空
    """
    return str(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else ""


def build_onebot_notice(raw: dict[str, Any], self_id: str) -> NoticePayload | None:
    """
    从 OneBot notice/request 原始事件构造类型化载荷

    参数:
    - raw: 原始事件字典
    - self_id: 当前实例已绑定账号

    返回:
    - NoticePayload | None: 未知类别或账号不匹配返回 None
    """
    post_type = _text(raw.get("post_type"))
    if post_type == "notice":
        kind = _text(raw.get("notice_type"))
    elif post_type == "request":
        kind = _text(raw.get("request_type"))
    else:
        return None
    if not kind:
        return None
    incoming = _text(raw.get("self_id"))
    if self_id and incoming and incoming != self_id:
        return None
    raw_file = raw.get("file")
    file: dict[str, Any] = {}
    if isinstance(raw_file, dict):
        for key, value in cast(dict[object, object], raw_file).items():
            if isinstance(key, str) and key in {"id", "name", "size", "busid", "url"}:
                file[key] = value
    raw_duration = raw.get("duration")
    raw_time = raw.get("time")
    return NoticePayload(
        category=post_type, kind=kind, sub_type=_text(raw.get("sub_type")), self_id=incoming or self_id,
        group_id=_text(raw.get("group_id")), user_id=_text(raw.get("user_id")),
        operator_id=_text(raw.get("operator_id")), target_id=_text(raw.get("target_id")),
        message_id=_text(raw.get("message_id")), flag=_text(raw.get("flag")), comment=_text(raw.get("comment")),
        duration=int(raw_duration) if isinstance(raw_duration, int) and not isinstance(raw_duration, bool) else 0,
        time=int(raw_time) if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool) else 0,
        file=file,
    )


def notice_attachment(payload: NoticePayload) -> File | None:
    """
    将群文件上传通知归一为 File 附件组件, 只携带远端元信息, 不触发下载

    参数:
    - payload: 归一化后的通知载荷

    返回:
    - File | None: 非群文件上传或缺少文件信息时返回 None; file 字段为远端文件 ID, url 可能为空
    """
    if payload.category != "notice" or payload.kind != "group_upload" or not payload.file:
        return None
    raw_name = payload.file.get("name")
    name = str(raw_name).strip() if isinstance(raw_name, str) else ""
    raw_id = payload.file.get("id")
    file_id = str(raw_id) if isinstance(raw_id, (int, str)) and not isinstance(raw_id, bool) else ""
    raw_url = payload.file.get("url")
    url = str(raw_url) if isinstance(raw_url, str) else ""
    if not file_id and not url:
        return None
    return File(name=name or "file", file=file_id, url=url)


_current_hub: PlatformEventHub | None = None


def set_current_hub(hub: PlatformEventHub | None) -> None:
    """
    登记后端当前使用的处理中心, 由 BackendManager 在启动/关闭时调用

    参数:
    - hub: 处理中心或 None
    """
    global _current_hub
    _current_hub = hub


def current_hub() -> PlatformEventHub | None:
    """
    供插件在 build_tools/build_handlers 中订阅平台事件

    返回:
    - PlatformEventHub | None: 后端未运行或未装配时为 None, 插件应据此跳过订阅
    """
    return _current_hub
