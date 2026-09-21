"""自动参与的有界文本窗口, 只在提交模型调用时消费快照"""
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic

from satrap.core.platform.event import MessageEvent
from satrap.core.pipeline.wake_policy import WakeDecision
from satrap.core.components import Plain


@dataclass(frozen=True)
class PendingText:
    """不保留附件或可变事件的轻量消息快照"""

    request_id: str
    actor_id: str
    message_id: str
    text: str
    received_at: float


class WakeWindow:
    """按来源路由隔离, 全局路由数与每路由文本均有界"""

    def __init__(self, max_routes: int = 512, max_messages: int = 32, max_chars: int = 8192, ttl: float = 120):
        """
        初始化轻量窗口

        参数:
        - max_routes: 最大路由数
        - max_messages: 每路由最大消息数
        - max_chars: 每路由最大正文字符数
        - ttl: 消息保留秒数
        """
        if min(max_routes, max_messages, max_chars) <= 0 or ttl <= 0:
            raise ValueError("窗口容量和 TTL 必须为正数")
        self.max_routes, self.max_messages, self.max_chars, self.ttl = max_routes, max_messages, max_chars, ttl
        self._last_sweep = float("-inf")
        self._pending: OrderedDict[tuple[str, ...], list[PendingText]] = OrderedDict()
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
                event.session_provider, event.session_type or "", "" if scope == "group" else origin.route_user_id or origin.actor_id)

    def observe(self, event: MessageEvent, now: float | None = None) -> tuple[PendingText, ...]:
        """
        接收顶层正文并返回未提交快照, 附件和纯提及不计数

        参数:
        - event: 当前事件
        - now: 单调时钟, 默认读取当前时间

        返回:
        - tuple[PendingText, ...]: 当前路由有效文本
        """
        now = monotonic() if now is None else now
        self._sweep(now)
        key = self.key(event)
        text = "".join(item.text for item in event.get_messages() if isinstance(item, Plain)).strip()
        if not text:
            return ()
        origin = event.call_origin
        self._expire_route(key, now)
        rows = self._pending.setdefault(key, [])
        if not any(item.request_id == origin.request_id or (origin.source_message_id and item.message_id == origin.source_message_id) for item in rows):
            rows.append(PendingText(origin.request_id, origin.actor_id, origin.source_message_id, text[:self.max_chars], now))
            self._record_activity(key, now, False)
        while len(rows) > self.max_messages or sum(len(item.text) for item in rows) > self.max_chars:
            rows.pop(0)
        self._pending.move_to_end(key)
        while len(self._pending) > self.max_routes:
            self._pending.popitem(last=False)
        return tuple(rows)

    def _expire_route(self, key: tuple[str, ...], now: float) -> list[PendingText]:
        """
        只清理当前路由的过期正文, 避免每事件全表重建

        参数:
        - key: 目标路由
        - now: 单调时钟

        返回:
        - list[PendingText]: 该路由仍有效的正文列表, 空列表时已从窗口移除
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

    def decide(self, event: MessageEvent, snapshot: tuple[PendingText, ...], now: float | None = None, *, deadline: bool = False) -> WakeDecision:
        """
        评估数量或本地必要性, 不额外调用模型

        参数:
        - event: 当前事件及策略
        - snapshot: 有效的未提交正文
        - now: 单调时钟, 默认读取当前时间
        - deadline: 是否为到期复查, 仍检查冷却和有效正文

        返回:
        - WakeDecision: 阈值, 冷却或评分解释
        """
        now = monotonic() if now is None else now
        if not snapshot:
            return WakeDecision(False, "no_pending", "没有可处理正文")
        settings = event.policy_settings
        mode = settings.get("wake_mode", "explicit")
        if mode not in {"frequency", "necessity"}:
            return WakeDecision(False, "explicit_only", "自动参与未启用")
        last = self._submitted.get(self.key(event))
        if last is not None and now - last < float(settings.get("wake_cooldown", 30)):
            return WakeDecision(False, "cooldown", "自动参与冷却中")
        max_wait = float(settings.get("wake_max_wait", 0))
        if deadline and max_wait > 0 and now - snapshot[0].received_at >= max_wait:
            return WakeDecision(True, "max_wait", "待处理正文达到最长等待时间")
        if mode == "frequency":
            triggered = len(snapshot) >= int(settings.get("wake_message_threshold", 3))
            return WakeDecision(triggered, "frequency", f"待处理正文 {len(snapshot)} 条")
        text = "\n".join(item.text for item in snapshot)
        question = float(any(mark in text for mark in ("?", "？", "请问", "怎么", "如何", "为什么", "能否", "是否")))
        addressed = float(any(mark in text for mark in ("你觉得", "你能", "帮我", "帮忙", "请教")))
        backlog = min(len(snapshot) / int(settings.get("wake_message_threshold", 3)), 1.0)
        history = [item for item in self._activity.get(self.key(event), []) if now - item[0] < self.ttl]
        ratio = sum(item[1] for item in history) / max(len(history), 1)
        score = max(0.0, min(1.0,
            question * float(settings.get("wake_question_weight", 0.55))
            + addressed * float(settings.get("wake_address_weight", 0.15))
            + backlog * float(settings.get("wake_backlog_weight", 0.30))
            - ratio * float(settings.get("wake_reply_penalty", 0.40))))
        threshold = float(settings.get("wake_score_threshold", 0.65))
        reason = f"问题={question:g}, 指向性={addressed:g}, 积压={backlog:.2f}, 近期提交占比={ratio:.2f}, 阈值={threshold:g}"
        return WakeDecision(score >= threshold, "necessity", reason, score=score)

    def ready(self, event: MessageEvent, count: int, now: float | None = None) -> bool:
        """
        判断频率阈值和自动回复冷却

        参数:
        - event: 当前事件
        - count: 当前有效消息数
        - now: 单调时钟, 默认读取当前时间

        返回:
        - bool: 是否达到自动触发条件
        """
        now = monotonic() if now is None else now
        last = self._submitted.get(self.key(event))
        return count >= int(event.policy_settings.get("wake_message_threshold", 3)) and (
            last is None or now - last >= float(event.policy_settings.get("wake_cooldown", 30))
        )

    def claim(self, event: MessageEvent, snapshot: tuple[PendingText, ...], automatic: bool, now: float | None = None, *, deadline: bool = False) -> tuple[PendingText, ...]:
        """
        提交前原子消费尚未提交且未过期的快照, 不自动重试已提交调用

        参数:
        - event: 当前事件
        - snapshot: 唤醒时冻结的候选消息
        - automatic: 是否重新核对自动阈值和冷却
        - now: 单调时钟, 默认读取当前时间
        - deadline: 是否按最长等待时间重新判断

        返回:
        - tuple[PendingText, ...]: 本轮可消费内容, 竞争失败时为空
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

    def peek(self, event: MessageEvent, now: float | None = None) -> tuple[PendingText, ...]:
        """
        读取未过期正文, 不增加计数或延长有效期

        参数:
        - event: 目标路由事件
        - now: 单调时钟, 默认读取当前时间

        返回:
        - tuple[PendingText, ...]: 当前有效窗口
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
