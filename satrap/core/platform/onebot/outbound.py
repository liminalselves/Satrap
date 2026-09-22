"""
OneBot 普通消息发送计划与执行串行化

按文本预算拆分组件, 保留非文本组件顺序,
按平台会话限制并发发送并在关闭时撤销尚未完成的逻辑回复
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
import asyncio

from satrap.core.components import BaseMessageComponent, Forward, Node, Nodes, PlatformComponentType, Plain
from satrap.core.type import safe_getattr_str

T = TypeVar("T")


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


def split_forward_turns(components: list[BaseMessageComponent]) -> list[tuple[str, list[BaseMessageComponent]]]:
    """
    在 Node/Nodes 边界把消息链拆为普通段与转发段, 相邻同类合并

    参数:
    - components: 原始组件, 不原地修改

    返回:
    - list[tuple[str, list]]: 按原顺序排列的 ("normal", 组件) 与 ("forward", Node 列表),
      不隐式把整条链包装成转发
    """
    turns: list[tuple[str, list[BaseMessageComponent]]] = []
    normal: list[BaseMessageComponent] = []
    nodes: list[BaseMessageComponent] = []
    for component in components:
        if isinstance(component, (Node, Nodes)):
            if normal:
                turns.append(("normal", normal))
                normal = []
            nodes.extend(component.nodes if isinstance(component, Nodes) else [component])
        else:
            if nodes:
                turns.append(("forward", nodes))
                nodes = []
            normal.append(component)
    if nodes:
        turns.append(("forward", nodes))
    if normal:
        turns.append(("normal", normal))
    return turns


def flatten_forward_nodes(nodes: list[Node]) -> list[BaseMessageComponent]:
    """
    将转发节点展开为普通组件序列, 供不支持转发接口的实现降级分段发送

    参数:
    - nodes: 待展开的 Node 组件

    返回:
    - list: 节点正文按原顺序拼接, 嵌套转发组件替换为占位文本
    """
    flat: list[BaseMessageComponent] = []
    for node in nodes:
        for component in node.content:
            if isinstance(component, (Node, Nodes, Forward)):
                flat.append(Plain("[转发]"))
            else:
                flat.append(component)
    return flat


class OutboundTurns:
    """实例级有界逻辑回复队列, 同一目标保持整轮发送顺序"""

    def __init__(self) -> None:
        """初始化至多 64 个等待或执行中的逻辑回复"""
        self.locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.closed = False

    async def run(self, target: str, operation: Callable[[], Awaitable[T]]) -> T:
        """
        在一个平台目标的有界发送机会内执行发送

        参数:
        - target: 平台原生会话 ID
        - operation: 已获取执行权后运行的发送协程工厂

        返回:
        - T: 发送结果; 关闭或满载时抛出 RuntimeError, 等待超过 30 秒抛出 TimeoutError

        发送在独立子任务中执行, 关闭时只取消子任务而不影响调用方所在的事件处理任务
        """
        if self.closed or len(self.tasks) >= 64:
            raise RuntimeError("发送队列不可用")
        lock, users = self.locks.get(target, (asyncio.Lock(), 0))
        self.locks[target] = (lock, users + 1)
        task: asyncio.Task[T] = asyncio.create_task(self._guarded(lock, operation))
        self.tasks.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if self.closed and task.cancelled():
                raise RuntimeError("发送队列已关闭") from None
            if task.done() and not task.cancelled() and task.exception() is not None:
                raise RuntimeError("发送子任务失败") from task.exception()
            # 调用方自身被取消: 连带取消子任务并保留取消语义
            task.cancel()
            raise
        finally:
            self.tasks.discard(task)
            remaining = self.locks[target][1] - 1
            if remaining:
                self.locks[target] = (lock, remaining)
            else:
                self.locks.pop(target)

    async def _guarded(self, lock: asyncio.Lock, operation: Callable[[], Awaitable[T]]) -> T:
        """等待目标锁后执行发送"""
        await asyncio.wait_for(lock.acquire(), 30)
        try:
            if self.closed:
                raise RuntimeError("发送队列已关闭")
            return await operation()
        finally:
            lock.release()

    async def close(self) -> None:
        """拒绝新回复并等待已有发送子任务取消完成"""
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
