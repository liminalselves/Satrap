from __future__ import annotations
from satrap.core.utils.TCBuilder import Tool
from satrap.edictum import SimpleSession
from .base import (
    _DocumentCore,
    _MemoryBinding,
    _AddMemoryToolCore,
    _UpdateMemoryToolCore,
    _DeleteMemoryToolCore,
    _ListMemoriesToolCore,
)


class ReadDocumentTool(_DocumentCore, Tool):
    """读取文档 (xlsx/docx/pdf/txt/md 等) 并解析为纯文本"""

    tool_name = "read_document"
    description = "读取文档并解析为纯文本, 支持 xlsx/docx/pdf/txt/md 等; 适合读取表格、文档、PDF 内容"
    params_dict = {
        "path": ("string", "文档路径 (绝对或相对工作区)"),
        "max_length": ("number", "返回文本最大长度, 默认 131072, 超出截断"),
    }

    def _bind(self, session: SimpleSession) -> None:
        self._session = session

    def execute(self, path: str, max_length: int = 131072) -> str:
        """执行共用文档读取逻辑"""
        return self._read_document(path, max_length)


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
