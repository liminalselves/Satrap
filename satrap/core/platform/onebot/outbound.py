"""
OneBot 普通消息发送计划与执行串行化

按文本预算拆分组件, 保留非文本组件顺序,
按平台会话限制并发发送并在关闭时撤销尚未完成的逻辑回复
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
import asyncio

from satrap.core.components import BaseMessageComponent, PlatformComponentType, Plain
from satrap.core.type import safe_getattr_str


def split_components(components: list[BaseMessageComponent], limit: int) -> list[list[BaseMessageComponent]]:
    """
    按段落和换行优先拆分文本, 非文本组件不可切开

    参数:
    - components: 原始组件, 不原地修改
    - limit: 每块文本字符上限, 必须为正整数

    返回:
    - list: 按原顺序排列的非空组件块, 拼接文本不丢失字符
    """
    if limit <= 0:
        raise ValueError("文本上限必须为正数")
    chunks: list[list[BaseMessageComponent]] = []
    current: list[BaseMessageComponent] = []
    used = 0
    for component in components:
        if component.type != PlatformComponentType.Plain:
            current.append(component)
            continue
        text = safe_getattr_str(component, "text")
        while text:
            remaining = limit - used
            if remaining == 0:
                chunks.append(current)
                current, used = [], 0
                remaining = limit
            end = min(len(text), remaining)
            if len(text) > remaining:
                paragraph = text.rfind("\n\n", 0, remaining)
                newline = text.rfind("\n", 0, remaining)
                if paragraph >= 0:
                    end = paragraph + 2
                elif newline >= 0:
                    end = newline + 1
            current.append(Plain(text[:end]))
            used += end
            text = text[end:]
            if text:
                chunks.append(current)
                current, used = [], 0
    if current:
        chunks.append(current)
    return chunks


class OutboundTurns:
    """实例级有界逻辑回复队列, 同一目标保持整轮发送顺序"""

    def __init__(self) -> None:
        """初始化至多 64 个等待或执行中的逻辑回复"""
        self.locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self.tasks: set[asyncio.Task[object]] = set()
        self.closed = False

    @asynccontextmanager
    async def turn(self, target: str) -> AsyncIterator[None]:
        """
        获取一个平台目标的有界发送机会

        参数:
        - target: 平台原生会话 ID

        返回:
        - AsyncIterator: 等待至多 30 秒的互斥发送作用域, 关闭或满载时拒绝
        """
        task = asyncio.current_task()
        if self.closed or len(self.tasks) >= 64 or task is None:
            raise RuntimeError("发送队列不可用")
        lock, users = self.locks.get(target, (asyncio.Lock(), 0))
        self.locks[target] = (lock, users + 1)
        self.tasks.add(task)
        acquired = False
        try:
            await asyncio.wait_for(lock.acquire(), 30)
            acquired = True
            if self.closed:
                raise RuntimeError("发送队列已关闭")
            yield
        finally:
            if acquired:
                lock.release()
            self.tasks.discard(task)
            remaining = self.locks[target][1] - 1
            if remaining:
                self.locks[target] = (lock, remaining)
            else:
                self.locks.pop(target)

    async def close(self) -> None:
        """拒绝新回复并等待已有发送任务取消完成"""
        self.closed = True
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
