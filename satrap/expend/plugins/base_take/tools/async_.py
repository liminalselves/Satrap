from __future__ import annotations
import asyncio
from satrap.core.utils.TCBuilder import AsyncTool
from satrap.edictum import AsyncSimpleSession
from .base import (
    _DocumentCore,
    _MemoryBinding,
    _AddMemoryToolCore,
    _UpdateMemoryToolCore,
    _DeleteMemoryToolCore,
    _ListMemoriesToolCore,
)


class AsyncReadDocumentTool(_DocumentCore, AsyncTool):
    """读取文档并解析为纯文本 (异步)"""

    tool_name = "read_document"
    description = "读取文档并解析为纯文本, 支持 xlsx/docx/pdf/txt/md 等; 适合读取表格、文档、PDF 内容"
    params_dict = {
        "path": ("string", "文档路径 (绝对或相对工作区)"),
        "max_length": ("number", "返回文本最大长度, 默认 131072, 超出截断"),
    }

    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session

    async def execute(self, path: str, max_length: int = 131072) -> str:
        """执行共用文档读取逻辑"""
        return await asyncio.to_thread(self._read_document, path, max_length)


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
