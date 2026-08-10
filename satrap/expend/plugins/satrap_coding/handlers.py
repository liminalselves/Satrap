"""satrap_coding 处理器: 长期记忆 + 持续目标注入模型输入

注入点: before_user_send (用户消息进入模型前), 把记忆块与目标块拼接到消息头部;
版本号缓存避免每轮重复读库, 内容变化 (写操作后) 自动失效。
"""
from __future__ import annotations

from typing import Any

from satrap.edictum import AsyncSimpleSession, SessionHandler, SimpleSession
from satrap.expend.plugins.satrap_coding.core.goal_state import GoalState
from satrap.expend.plugins.satrap_coding.core.memory_store import MemoryStore

_HEADER = "【长期记忆与目标】\n"


SessionType = SimpleSession | AsyncSimpleSession
"""插件支持的会话类型"""


class _Injector:
    """记忆/目标注入器 (带缓存失效)"""

    def __init__(self, store: MemoryStore, goals: GoalState, session: SessionType) -> None:
        self.store = store
        self.goals = goals
        self.session = session
        self._cache: tuple[str, str, str] = ("", "", "")
        """缓存: (记忆块, 目标块, 拼接结果)"""

    def _memory_block(self) -> str:
        return self.store.to_context_block()

    def _goal_block(self) -> str:
        return self.goals.to_context_block(self.session.session_id)

    def inject(self, text: str) -> str:
        """返回注入后的文本 (无内容时透传)"""
        memory_block = self._memory_block()
        goal_block = self._goal_block()
        if not memory_block and not goal_block:
            return text
        if (memory_block, goal_block) != self._cache[:2]:
            combined = "\n".join(block for block in (memory_block, goal_block) if block)
            self._cache = (memory_block, goal_block, combined)
        return _HEADER + self._cache[2] + "\n" + text

    def invalidate(self) -> None:
        """主动失效缓存 (记忆/目标被修改后调用)"""
        self._cache = ("", "", "")


def build_handlers(session: SessionType) -> list[SessionHandler]:
    """构建处理器: 注入记忆与目标到模型输入"""
    from satrap.edictum import HandlerContext
    from satrap.expend.plugins.satrap_coding.state import get_plugin_state

    state = get_plugin_state(session)
    store = state["store"]
    goals = state["goals"]
    assert isinstance(store, MemoryStore) and isinstance(goals, GoalState)
    injector = _Injector(store, goals, session)
    state["_injector"] = injector

    def before_user_send(text: str, ctx: HandlerContext) -> str:
        return injector.inject(text)

    return [SessionHandler(name="satrap_coding.inject", priority=0, before_user_send=before_user_send)]
