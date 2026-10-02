"""独立于唤醒模式的有界群聊上下文窗口, 在模型调用前原子认领快照"""
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import Any

from satrap.core.platform.event import MessageEvent
from satrap.core.config.platform_policy import policy_default
from satrap.core.pipeline.wake_policy import WakeDecision, resolve_message_threshold
from satrap.core.components import Image, Plain
from satrap.core.type import safe_getattr_str


MEDIA_REFERENCE_LIMIT = 4096
"""窗口图片来源字段的最大长度, 超限来源不保留, 防止内联媒体占满内存"""


def display_label(value: str) -> str:
    """
    将显示字段限制为单行, 不允许其伪造消息标记

    参数:
    - value: 昵称或身份显示字段

    返回:
    - str: 至多 128 字符的单行显示值
    """
    return "".join(" " if char.isspace() else "(" if char == "[" else ")" if char == "]" else char
                   for char in value if char.isprintable() or char.isspace()).strip()[:128]


def sender_label(nickname: str, actor_id: str, message_id: str) -> str:
    """
    生成包含稳定身份的消息来源标记

    参数:
    - nickname: 消息接收时的昵称
    - actor_id: 真实发送者 ID, 只处理显示副本
    - message_id: 平台消息 ID

    返回:
    - str: 昵称缺失时回退到 ID 的来源标记
    """
    actor = display_label(actor_id) or "未知"
    name = display_label(nickname)
    user = f"{name} (ID {actor})" if name and name != actor else actor
    return f"[用户 {user}, 消息 {display_label(message_id)}]"


@dataclass(frozen=True)
class PendingImage:
    """图片原始标识和 URL 的不可变副本, 不保存下载文件或组件对象"""

    file: str = ""
    url: str = ""


@dataclass(frozen=True)
class PendingMessage:
    """正文, 图片位置及昵称的轻量消息快照, 不保留可变事件"""

    request_id: str
    actor_id: str
    message_id: str
    text: str
    received_at: float
    nickname: str = ""
    parts: tuple[str | PendingImage, ...] = ()
    omitted_images: int = 0

    @property
    def image_count(self) -> int:
        """返回快照保留的图片引用数量"""
        return sum(isinstance(part, PendingImage) for part in self.parts)

    @property
    def text_size(self) -> int:
        """返回实际保留的文字大小, 包含图片周围的空白"""
        return sum(len(part) for part in self.parts if isinstance(part, str)) if self.parts else len(self.text)

    @property
    def preview(self) -> str:
        """返回不包含图片来源地址的轻量预览"""
        return "".join(part if isinstance(part, str) else "[图片]" for part in self.parts).strip() if self.parts else self.text


class WakeWindow:
    """按来源路由隔离, 路由数, 正文及图片引用均有界"""

    def __init__(self, max_routes: int = 512, max_messages: int = 32, max_chars: int = 8192, ttl: float = 120,
                 max_images: int = 32, max_images_per_message: int = 8):
        """
        初始化轻量窗口

        参数:
        - max_routes: 最大路由数
        - max_messages: 每路由最大消息数
        - max_chars: 每路由最大正文字符数
        - ttl: 消息保留秒数
        - max_images: 每路由最大图片引用数
        - max_images_per_message: 每条消息最大图片引用数
        """
        if min(max_routes, max_messages, max_chars, max_images, max_images_per_message) <= 0 or ttl <= 0:
            raise ValueError("窗口容量和 TTL 必须为正数")
        self.max_routes, self.max_messages, self.max_chars, self.ttl = max_routes, max_messages, max_chars, ttl
        self.max_images = max_images
        self.max_images_per_message = min(max_images, max_images_per_message)
        self._last_sweep = float("-inf")
        self._pending: OrderedDict[tuple[str, ...], list[PendingMessage]] = OrderedDict()
        self._submitted: OrderedDict[tuple[str, ...], float] = OrderedDict()
        self._activity: OrderedDict[tuple[str, ...], list[tuple[float, bool]]] = OrderedDict()

    @staticmethod
    def key(event: MessageEvent) -> tuple[str, ...]:
        """
        获取成员可见范围对应的路由键

        参数:
        - event: 已通过权限检查的事件

        返回:
        - tuple[str, ...]: 只有显式 group 范围共享成员正文
        """
        origin = event.call_origin
        scope = str(event.policy_settings.get("context_scope", "legacy_user"))
        return (origin.adapter_id, origin.self_id, origin.chat_type, origin.chat_id, scope,
                event.session_provider, event.session_type or "", str(event.group_route_generation),
                str(event.agent_route_generation),
                "" if scope == "group" else origin.route_user_id or origin.actor_id)

    def observe(self, event: MessageEvent, now: float | None = None) -> tuple[PendingMessage, ...]:
        """
        接收顶层正文与图片引用, 无内容消息仍可读取已有快照

        参数:
        - event: 当前事件
        - now: 单调时钟, 默认读取当前时间

        返回:
        - tuple[PendingMessage, ...]: 当前路由有效消息, 入窗不触发下载
        """
        now = monotonic() if now is None else now
        self._sweep(now)
        key = self.key(event)
        self._expire_route(key, now)
        parts: list[str | PendingImage] = []
        remaining = self.max_chars
        image_count = 0
        omitted_images = 0
        for item in event.get_messages():
            if isinstance(item, Plain) and remaining:
                text_part = item.text[:remaining]
                if text_part:
                    parts.append(text_part)
                    remaining -= len(text_part)
            elif isinstance(item, Image):
                if image_count >= self.max_images_per_message:
                    omitted_images += 1
                    continue
                file = safe_getattr_str(item, "file")
                url = safe_getattr_str(item, "url")
                file = file if len(file) <= MEDIA_REFERENCE_LIMIT else ""
                url = url if len(url) <= MEDIA_REFERENCE_LIMIT else ""
                parts.append(PendingImage(file, url))
                image_count += 1
        text = "".join(part for part in parts if isinstance(part, str)).strip()
        if not text and not image_count:
            return self.peek(event, now)
        origin = event.call_origin
        rows = self._pending.setdefault(key, [])
        if not any(item.request_id == origin.request_id or (origin.source_message_id and item.message_id == origin.source_message_id) for item in rows):
            rows.append(PendingMessage(origin.request_id, origin.actor_id, origin.source_message_id, text, now,
                                       display_label(event.get_sender_name()), tuple(parts), omitted_images))
            self._record_activity(key, now, False)
        while (len(rows) > self.max_messages or sum(item.text_size for item in rows) > self.max_chars
               or sum(item.image_count for item in rows) > self.max_images):
            rows.pop(0)
        self._pending.move_to_end(key)
        while len(self._pending) > self.max_routes:
            self._pending.popitem(last=False)
        return tuple(rows)

    def _expire_route(self, key: tuple[str, ...], now: float) -> list[PendingMessage]:
        """
        只清理当前路由的过期消息, 避免每事件全表重建

        参数:
        - key: 目标路由
        - now: 单调时钟

        返回:
        - list[PendingMessage]: 该路由仍有效的消息列表, 空列表时已从窗口移除
        """
        rows = self._pending.get(key)
        if rows is None:
            return []
        if rows and now - rows[0].received_at >= self.ttl:
            rows[:] = [item for item in rows if now - item.received_at < self.ttl]
        if not rows:
            del self._pending[key]
        return rows

    def _sweep(self, now: float) -> None:
        """
        低频全表清扫, 每 ttl/4 至多一次, 释放长期没有新消息的路由

        参数:
        - now: 单调时钟
        """
        if now - self._last_sweep < self.ttl / 4:
            return
        self._last_sweep = now
        for key in list(self._pending):
            self._expire_route(key, now)

    def _record_activity(self, key: tuple[str, ...], now: float, submitted: bool) -> None:
        """
        记录有限的近期正文与机器人提交比例

        参数:
        - key: 来源路由
        - now: 单调时钟
        - submitted: 是否为模型调用提交
        """
        rows = self._activity.setdefault(key, [])
        rows[:] = [item for item in rows if now - item[0] < self.ttl][-63:]
        rows.append((now, submitted))
        self._activity.move_to_end(key)
        while len(self._activity) > self.max_routes:
            self._activity.popitem(last=False)

    def decide(self, event: MessageEvent, snapshot: tuple[PendingMessage, ...], now: float | None = None, *, deadline: bool = False) -> WakeDecision:
        """
        评估数量或本地必要性, 不额外调用模型

        参数:
        - event: 当前事件及策略
        - snapshot: 有效的未提交消息
        - now: 单调时钟, 默认读取当前时间
        - deadline: 是否为到期复查, 仍检查冷却和有效消息

        返回:
        - WakeDecision: 阈值, 冷却或评分解释
        """
        now = monotonic() if now is None else now
        if not snapshot:
            return WakeDecision(False, "no_pending", "没有可处理消息")
        settings = event.policy_settings
        mode = settings.get("wake_mode", policy_default("wake_mode"))
        if mode not in {"frequency", "necessity"}:
            return WakeDecision(False, "explicit_only", "自动参与未启用")
        resolution = resolve_message_threshold(settings) if mode == "frequency" else None
        if resolution is not None and resolution.closed:
            # 有效来源是 talk_value=0: 自动参与已关闭, 到期补偿不绕过关闭
            return WakeDecision(False, "frequency", f"{resolution.label}, 自动参与不触发")
        last = self._submitted.get(self.key(event))
        if last is not None and now - last < float(settings.get("wake_cooldown", policy_default("wake_cooldown"))):
            return WakeDecision(False, "cooldown", "自动参与冷却中")
        max_wait = float(settings.get("wake_max_wait", policy_default("wake_max_wait")))
        if deadline and max_wait > 0 and now - snapshot[0].received_at >= max_wait:
            return WakeDecision(True, "max_wait", "待处理消息达到最长等待时间")
        if resolution is not None:
            triggered = len(snapshot) >= resolution.threshold
            return WakeDecision(triggered, "frequency", f"待处理消息 {len(snapshot)} 条, 阈值 {resolution.label}")
        text = "\n".join(item.text for item in snapshot)
        question = float(any(mark in text for mark in ("?", "？", "请问", "怎么", "如何", "为什么", "能否", "是否")))
        addressed = float(any(mark in text for mark in ("你觉得", "你能", "帮我", "帮忙", "请教")))
        backlog = min(len(snapshot) / max(1, int(settings.get("wake_message_threshold", policy_default("wake_message_threshold")))), 1.0)
        history = [item for item in self._activity.get(self.key(event), []) if now - item[0] < self.ttl]
        ratio = sum(item[1] for item in history) / max(len(history), 1)
        score = max(0.0, min(1.0,
            question * float(settings.get("wake_question_weight", policy_default("wake_question_weight")))
            + addressed * float(settings.get("wake_address_weight", policy_default("wake_address_weight")))
            + backlog * float(settings.get("wake_backlog_weight", policy_default("wake_backlog_weight")))
            - ratio * float(settings.get("wake_reply_penalty", policy_default("wake_reply_penalty")))))
        threshold = float(settings.get("wake_score_threshold", policy_default("wake_score_threshold")))
        reason = f"问题={question:g}, 指向性={addressed:g}, 积压={backlog:.2f}, 近期提交占比={ratio:.2f}, 阈值={threshold:g}"
        return WakeDecision(score >= threshold, "necessity", reason, score=score)


    def claim(self, event: MessageEvent, snapshot: tuple[PendingMessage, ...], automatic: bool, now: float | None = None, *, deadline: bool = False) -> tuple[PendingMessage, ...]:
        """
        提交前原子消费尚未提交且未过期的快照, 不自动重试已提交调用

        参数:
        - event: 当前事件
        - snapshot: 唤醒时冻结的候选消息
        - automatic: 是否重新核对自动阈值和冷却
        - now: 单调时钟, 默认读取当前时间
        - deadline: 是否按最长等待时间重新判断

        返回:
        - tuple[PendingMessage, ...]: 本轮可消费内容, 竞争失败时为空
        """
        now = monotonic() if now is None else now
        key = self.key(event)
        ids = {item.request_id for item in snapshot}
        current = self._pending.get(key, [])
        selected = tuple(item for item in current if item.request_id in ids and now - item.received_at < self.ttl)
        if automatic and not self.decide(event, selected, now, deadline=deadline).triggered:
            return ()
        if selected:
            self._pending[key] = [item for item in current if item.request_id not in ids]
            if not self._pending[key]:
                del self._pending[key]
            self._submitted[key] = now
            self._record_activity(key, now, True)
            self._submitted.move_to_end(key)
            while len(self._submitted) > self.max_routes:
                self._submitted.popitem(last=False)
        return selected

    def cooldown_remaining(self, event: MessageEvent, now: float | None = None) -> float:
        """
        该路由距离自动参与冷却结束的剩余秒数

        参数:
        - event: 目标事件
        - now: 参考时刻

        返回:
        - float: 剩余秒数, 不在冷却中返回 0
        """
        now = monotonic() if now is None else now
        last = self._submitted.get(self.key(event))
        if last is None:
            return 0.0
        return max(0.0, float(event.policy_settings.get("wake_cooldown", policy_default("wake_cooldown"))) - (now - last))

    def peek(self, event: MessageEvent, now: float | None = None) -> tuple[PendingMessage, ...]:
        """
        读取未过期消息, 不增加计数或延长有效期

        参数:
        - event: 目标路由事件
        - now: 单调时钟, 默认读取当前时间

        返回:
        - tuple[PendingMessage, ...]: 当前有效窗口
        """
        now = monotonic() if now is None else now
        return tuple(item for item in self._pending.get(self.key(event), []) if now - item.received_at < self.ttl)

    def clear_adapter(self, adapter_id: str) -> None:
        """
        清除停用或重载平台的窗口与冷却

        参数:
        - adapter_id: 平台实例 ID
        """
        for mapping in (self._pending, self._submitted, self._activity):
            for key in list(mapping):
                if key[0] == adapter_id:
                    del mapping[key]
