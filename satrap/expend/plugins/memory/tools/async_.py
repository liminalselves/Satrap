"""记忆工具的同步与异步入口"""
from __future__ import annotations
from typing import Any
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
    ) -> str:
        """执行共用记忆操作"""
        return self._execute(title, content, tags, importance)


class AsyncUpdateMemoryTool(_UpdateMemoryToolCore, _AsyncMemoryToolBase):
    async def execute(self, memory_id: str, content: str = "", title: str = "") -> str:
        """执行共用记忆操作"""
        return self._execute(memory_id, content, title)


class AsyncDeleteMemoryTool(_DeleteMemoryToolCore, _AsyncMemoryToolBase):
    async def execute(self, memory_id: str) -> str:
        """执行共用记忆操作"""
        return self._execute(memory_id)


class AsyncListMemoriesTool(_ListMemoriesToolCore, _AsyncMemoryToolBase):
    async def execute(self) -> str:
        """执行共用记忆操作"""
        return self._execute()


class AsyncGetMemoryTool(_GetMemoryToolCore, _AsyncMemoryToolBase):
    async def execute(self, memory_id: str) -> dict[str, Any]:
        """
        读取完整记忆详情

        参数:
        - memory_id: 当前范围内的记忆 ID

        返回:
        - 记忆详情或明确错误
        """
        return self._execute(memory_id)
