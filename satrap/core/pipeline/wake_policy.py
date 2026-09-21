"""群聊唤醒规则与可解释决策, 不执行模型调用或内容回源"""
from dataclasses import dataclass

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
    if event.is_wake_up() or event.is_at_or_wake_command:
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
