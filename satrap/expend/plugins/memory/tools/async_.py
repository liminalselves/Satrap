"""记忆工具的同步与异步入口"""
from __future__ import annotations
from typing import Any
import asyncio
from satrap.expend.plugins.memory.runtime import group_call
from satrap.core.utils.TCBuilder import AsyncTool
from .base import (_MemoryBinding, _AddMemoryToolCore, _UpdateMemoryToolCore, _DeleteMemoryToolCore, _ListMemoriesToolCore, _GetMemoryToolCore)


class _AsyncMemoryToolBase(_MemoryBinding, AsyncTool):
    """异步记忆工具基类"""


class AsyncAddMemoryTool(_AddMemoryToolCore, _AsyncMemoryToolBase):
    async def execute(
        self,
        title: str,
        content: str,
        tags: list[str] | None = None,
        importance: int = 1,
        **group_values: Any,
    ) -> str | dict[str, Any]:
        """执行共用记忆操作"""
        if group_call():
            return await self._group("create", self._group_values(title, content, group_values))
        return await asyncio.to_thread(self._execute, title, content, tags, importance, **group_values)


class AsyncUpdateMemoryTool(_UpdateMemoryToolCore, _AsyncMemoryToolBase):
    async def execute(self, memory_id: str, content: str = "", title: str = "", **group_values: Any) -> str | dict[str, Any]:
        """执行共用记忆操作"""
        if group_call():
            return await self._group("update", self._group_values(memory_id, content, title, group_values))
        return await asyncio.to_thread(self._execute, memory_id, content, title, **group_values)


class AsyncDeleteMemoryTool(_DeleteMemoryToolCore, _AsyncMemoryToolBase):
    async def execute(self, memory_id: str, **group_values: Any) -> str | dict[str, Any]:
        """执行共用记忆操作"""
        if group_call():
            return await self._group("delete", self._group_values(memory_id, group_values))
        return await asyncio.to_thread(self._execute, memory_id, **group_values)


class AsyncListMemoriesTool(_ListMemoriesToolCore, _AsyncMemoryToolBase):
    async def execute(self, **filters: Any) -> str | dict[str, Any]:
        """执行共用记忆操作"""
        if group_call():
            return await self._group("list", filters)
        return await asyncio.to_thread(self._execute, **filters)


class AsyncGetMemoryTool(_GetMemoryToolCore, _AsyncMemoryToolBase):
    async def execute(self, memory_id: str) -> dict[str, Any]:
        """
        读取完整记忆详情

        参数:
        - memory_id: 当前范围内的记忆 ID

        返回:
        - 记忆详情或明确错误
        """
        if group_call():
            return await self._group("get", self._group_values(memory_id))
        return await asyncio.to_thread(self._execute, memory_id)
