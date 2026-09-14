"""
satrap_coding 处理器: 持续目标注入模型输入

注入点: before_user_send (用户消息进入模型前), 把目标块拼接到消息头部;
每轮读取内容并比较缓存, 内容变化时重新拼接注入文本

注: 长期记忆注入已移交 base_take 插件 (priority=0), 本处理器只注入目标 (priority=1)
"""
from __future__ import annotations

from satrap.expend.plugins.satrap_coding.core.goal_state import GoalState
from satrap.expend.plugins.satrap_coding.state import get_plugin_state
from satrap.edictum import (
    AsyncSimpleSession,
    HandlerContext,
    SessionHandler,
    SimpleSession,
)

_HEADER = "【持续目标】\n"


SessionType = SimpleSession | AsyncSimpleSession
"""插件支持的会话类型"""


class _GoalInjector:
    """目标注入器 (带缓存失效)"""

    def __init__(self, goals: GoalState, session: SessionType) -> None:
        """
        初始化 _GoalInjector

        参数:
        - goals: 目标列表
        - session: 会话
        """
        self.goals = goals
        self.session = session
        self._cache: tuple[str, str] = ("", "")
        """缓存: (目标块, 拼接结果)"""

    def inject(self, text: str) -> str:
        """
        返回注入后的文本 (无内容时透传)

        参数:
        - text: 待处理文本

        返回:
        - str: 注入后的文本 (无内容时透传)
        """
        goal_block = self.goals.to_context_block(self.session.session_id)
        if not goal_block:
            return text
        if goal_block != self._cache[0]:
            self._cache = (goal_block, _HEADER + goal_block + "\n")
        return self._cache[1] + text

def build_handlers(session: SessionType) -> list[SessionHandler]:
    """
    构建处理器: 注入目标到模型输入

    参数:
    - session: 会话

    返回:
    - list[SessionHandler]: 构建处理器: 注入目标到模型输入
    """
    state = get_plugin_state(session)
    goals = state["goals"]
    assert isinstance(goals, GoalState)
    injector = _GoalInjector(goals, session)

    def before_user_send(text: str, ctx: HandlerContext) -> str:
        return injector.inject(text)

    return [SessionHandler(name="satrap_coding.inject", priority=1, before_user_send=before_user_send)]
    # priority=1: 错开 base_take 的记忆注入 (priority=0), 记忆在前目标在后
