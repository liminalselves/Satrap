"""base_take 异步工具入口, 复用文档读取及记忆操作核心"""
from __future__ import annotations
from typing import Any
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
    description = "读取 xlsx/docx/pdf/txt/md 等文档; PDF 支持分页, auto 随模型能力返回文字或页面图像, text 只读文字, visual 查看图表和扫描页"
    params_dict = {
        "path": ("string", "文档路径 (绝对或相对工作区)"),
        "max_length": ("number", "返回文本最大长度, 默认 131072, 超出截断"),
        "mode": ("string", "auto/text/visual, 默认 auto"),
        "start_page": ("number", "PDF 起始页码, 从 1 开始"),
        "page_count": ("number", "本次读取 1 至 5 页, 默认 5"),
    }

    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session

    async def execute(self, path: str, max_length: int = 131072, mode: str = "auto", start_page: int = 1, page_count: int = 5) -> str | dict[str, Any]:
        """
        读取文档文字及可选 PDF 页面图片

        参数:
        - path: 工作区内文档路径
        - max_length: 文本字符上限, 默认 131072
        - mode: auto 随模型能力选择, text 只读文字, visual 返回页面图像
        - start_page: PDF 起始页码, 默认 1
        - page_count: PDF 读取页数, 默认 5, 最多 5

        返回:
        - 文本或可持久化的媒体结果, 读取失败时返回错误文本
        """
        return await asyncio.to_thread(self._read_document, path, max_length, mode, start_page, page_count)


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
