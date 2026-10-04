"""记忆工具的同步与异步入口"""
from __future__ import annotations
from typing import Any
from satrap.core.utils.TCBuilder import Tool
from .base import (_MemoryBinding, _AddMemoryToolCore, _UpdateMemoryToolCore, _DeleteMemoryToolCore, _ListMemoriesToolCore, _GetMemoryToolCore)


class _MemoryToolBase(_MemoryBinding, Tool):
    """记忆工具基类: store 绑定"""


class AddMemoryTool(_AddMemoryToolCore, _MemoryToolBase):
    def execute(
        self,
        title: str,
        content: str,
        tags: list[str] | None = None,
        importance: int = 1,
    ) -> str:
        """执行共用记忆操作"""
        return self._execute(title, content, tags, importance)


class UpdateMemoryTool(_UpdateMemoryToolCore, _MemoryToolBase):
    def execute(self, memory_id: str, content: str = "", title: str = "") -> str:
        """执行共用记忆操作"""
        return self._execute(memory_id, content, title)


class DeleteMemoryTool(_DeleteMemoryToolCore, _MemoryToolBase):
    def execute(self, memory_id: str) -> str:
        """执行共用记忆操作"""
        return self._execute(memory_id)


class ListMemoriesTool(_ListMemoriesToolCore, _MemoryToolBase):
    def execute(self) -> str:
        """执行共用记忆操作"""
        return self._execute()


class GetMemoryTool(_GetMemoryToolCore, _MemoryToolBase):
    def execute(self, memory_id: str) -> dict[str, Any]:
        """
        读取完整记忆详情

        参数:
        - memory_id: 当前范围内的记忆 ID

        返回:
        - 记忆详情或明确错误
        """
        return self._execute(memory_id)
