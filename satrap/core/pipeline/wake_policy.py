"""群聊唤醒规则与可解释决策, 不执行模型调用或内容回源"""
from collections.abc import Mapping
from dataclasses import dataclass
import sys

from satrap.core.config.platform_policy import normalize_wake_words
from satrap.core.platform.event import MessageEvent
from satrap.core.components import At, AtAll, Plain


@dataclass(frozen=True)
class WakeDecision:
    """本轮唤醒判断, 可用于界面解释和规则预览"""

    triggered: bool
    rule: str
    reason: str
    matched: str = ""
    score: float | None = None


NEVER_TRIGGER_THRESHOLD = sys.maxsize
"""wake_talk_value 为 0 时的映射结果: 任何窗口长度都达不到, 自动参与不触发"""

THRESHOLD_SOURCE_EXPLICIT = "explicit"
"""阈值来源: 显式 wake_message_threshold"""
THRESHOLD_SOURCE_TALK_VALUE = "talk_value"
"""阈值来源: wake_talk_value 映射"""
THRESHOLD_SOURCE_DEFAULT = "default"
"""阈值来源: 内置默认值"""

DEFAULT_MESSAGE_THRESHOLD = 3
"""未设置显式阈值与 wake_talk_value 时的条数阈值"""


@dataclass(frozen=True)
class MessageThreshold:
    """频率模式的有效阈值与来源, 显式阈值优先于 wake_talk_value 映射"""

    threshold: int
    source: str
    label: str
    talk_value: float | None = None
    talk_value_effective: bool = False
    """talk_value 是否为有效来源; 存在显式阈值时为 False, 即"被显式阈值覆盖" """

    @property
    def closed(self) -> bool:
        """有效来源是 talk_value=0: 自动参与被关闭, 到期补偿不得绕过"""
        return self.talk_value_effective and self.threshold == NEVER_TRIGGER_THRESHOLD


def resolve_message_threshold(settings: Mapping[str, object]) -> MessageThreshold:
    """
    解析频率模式的有效阈值与来源

    参数:
    - settings: 经平台, 时段, 群合并后的有效配置

    返回:
    - MessageThreshold: 阈值与来源; 显式 wake_message_threshold 优先,
      其次 wake_talk_value 映射, 皆无时为内置默认值
    """
    explicit = settings.get("wake_message_threshold")
    raw_talk = settings.get("wake_talk_value")
    talk_value = float(raw_talk) if isinstance(raw_talk, (int, float)) and not isinstance(raw_talk, bool) else None
    if explicit is not None:
        # 类型非法的显式阈值沿用严格校验语义在此暴露, 不静默改用映射或默认值
        value = int(str(explicit))
        return MessageThreshold(
            value, THRESHOLD_SOURCE_EXPLICIT, f"{value} (显式 wake_message_threshold)", talk_value, False,
        )
    if talk_value is not None:
        mapped = map_talk_value_threshold(talk_value)
        text = "不触发" if mapped == NEVER_TRIGGER_THRESHOLD else str(mapped)
        return MessageThreshold(
            mapped, THRESHOLD_SOURCE_TALK_VALUE, f"{text} (由 wake_talk_value={talk_value:g} 映射)", talk_value, True,
        )
    return MessageThreshold(DEFAULT_MESSAGE_THRESHOLD, THRESHOLD_SOURCE_DEFAULT, f"{DEFAULT_MESSAGE_THRESHOLD} (默认)", None, False)


TALK_VALUE_LADDER: tuple[tuple[float, int], ...] = (
    (1.0, 1),
    (0.75, 2),
    (0.5, 3),
    (0.35, 5),
    (0.2, 8),
    (0.1, 13),
)
"""发言频率偏好到消息条数阈值的固定阶梯, 0.5 档由映射时的 base 参数替换, 低于 0.1 固定为 21"""


def map_talk_value_threshold(talk_value: float, base: int = 3) -> int:
    """
    把发言频率偏好映射为频率模式的消息条数阈值

    参数:
    - talk_value: 0 到 1 的频率偏好, 越高越容易触发; 0 表示自动参与不触发
    - base: 0.5 档映射的阈值, 默认 3

    返回:
    - int: 阈值条数, 随 talk_value 单调不增
    """
    if talk_value <= 0:
        return NEVER_TRIGGER_THRESHOLD
    for cutoff, threshold in TALK_VALUE_LADDER:
        if talk_value >= cutoff:
            return base if cutoff == 0.5 else threshold
    return 21


def evaluate_wake(event: MessageEvent) -> WakeDecision:
    """
    按直接提及, 唤醒词, 别名的顺序评估顶层消息

    参数:
    - event: 已通过来源和权限检查的事件

    返回:
    - WakeDecision: 匹配原因, 不扫描引用或附件内容
    """
    if event.is_private_chat():
        return WakeDecision(True, "private", "私聊")
    if event.is_wake_up():
        return WakeDecision(True, "upstream", "上游明确唤醒")
    self_id = event.call_origin.self_id
    components = event.get_messages()
    if self_id and any(
        isinstance(component, At) and not isinstance(component, AtAll)
        and str(component.qq) != "all" and str(component.qq) == self_id
        for component in components
    ):
        return WakeDecision(True, "mention", "直接提及机器人", self_id)
    text_parts = [component.text for component in components if isinstance(component, Plain)]
    for key, rule, reason in [("wake_words", "wake_word", "命中唤醒词"), ("wake_aliases", "alias", "命中机器人别名")]:
        for word in normalize_wake_words(event.policy_settings.get(key, [])):
            if any(word in text for text in text_parts):
                return WakeDecision(True, rule, reason, word)
    return WakeDecision(False, "no_match", "未命中已启用的唤醒规则")
