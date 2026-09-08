from __future__ import annotations
import asyncio
from satrap.core.utils.TCBuilder import AsyncTool
from satrap.edictum import AsyncSimpleSession
from .file_core import (
    _ReadFileToolCore,
    _ListDirToolCore,
    _GlobFilesToolCore,
    _GrepFilesToolCore,
    _TodoWriteToolCore,
)


class AsyncReadFileTool(_ReadFileToolCore, AsyncTool):
    async def execute(self, path: str, offset: int = 0, limit: int = 200) -> str:
        """执行共用文件或待办业务"""
        return await asyncio.to_thread(self._execute, path, offset, limit)

    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session


class AsyncListDirTool(_ListDirToolCore, AsyncTool):
    async def execute(self, path: str = "") -> str:
        """执行共用文件或待办业务"""
        return await asyncio.to_thread(self._execute, path)

    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session


class AsyncGlobFilesTool(_GlobFilesToolCore, AsyncTool):
    async def execute(self, pattern: str) -> str:
        """执行共用文件或待办业务"""
        return await asyncio.to_thread(self._execute, pattern)

    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session


class AsyncGrepFilesTool(_GrepFilesToolCore, AsyncTool):
    async def execute(self, pattern: str, path: str = "", glob: str = "") -> str:
        """执行共用文件或待办业务"""
        return await asyncio.to_thread(self._execute, pattern, path, glob)

    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session


class AsyncTodoWriteTool(_TodoWriteToolCore, AsyncTool):
    async def execute(self, operation: str, item: str = "", index: int = 0) -> str:
        """执行共用文件或待办业务"""
        return self._execute(operation, item, index)
