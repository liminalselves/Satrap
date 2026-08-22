"""
base_take 处理器: 长期记忆注入模型输入 (从 coding 插件迁入, 仅记忆部分)

注入点: before_user_send (用户消息进入模型前), 把记忆块拼接到消息头部;
版本号缓存避免每轮重复读库, 内容变化 (写操作后) 自动失效
"""
from __future__ import annotations

from typing import Any

from satrap.edictum import AsyncSimpleSession, HandlerContext, SessionHandler, SimpleSession
from satrap.expend.plugins.base_take.state import get_plugin_state
from satrap.expend.tools.memory_store import MemoryStore

_HEADER = "【长期记忆】\n"

SessionType = SimpleSession | AsyncSimpleSession
"""插件支持的会话类型"""


class _MemoryInjector:
    """记忆注入器 (带缓存失效)"""

    def __init__(self, store: MemoryStore) -> None:
        """
        初始化 _MemoryInjector

        参数:
        - store: 存储实例
        """
        self.store = store
        self._cache: tuple[str, str] = ("", "")
        """缓存: (记忆块, 拼接结果)"""

    def inject(self, text: str) -> str:
        """
        返回注入后的文本 (无内容时透传)

        参数:
        - text: 待处理文本

        返回:
        - str: 注入后的文本 (无内容时透传)
        """
        memory_block = self.store.to_context_block()
        if not memory_block:
            return text
        if memory_block != self._cache[0]:
            self._cache = (memory_block, _HEADER + memory_block + "\n")
        return self._cache[1] + text

    def invalidate(self) -> None:
        """主动失效缓存 (记忆被修改后调用)"""
        self._cache = ("", "")


def build_handlers(session: SessionType) -> list[SessionHandler]:
    """
    构建处理器: 注入记忆到模型输入

    参数:
    - session: 会话

    返回:
    - list[SessionHandler]: 构建处理器: 注入记忆到模型输入
    """
    state = get_plugin_state(session)
    store = state["store"]
    assert isinstance(store, MemoryStore)
    injector = _MemoryInjector(store)
    state["_injector"] = injector

    def before_user_send(text: str, ctx: HandlerContext) -> str:
        return injector.inject(text)

    return [SessionHandler(name="base_take.memory_inject", priority=0, before_user_send=before_user_send)]
