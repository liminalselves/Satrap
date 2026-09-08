from __future__ import annotations
from satrap.core.utils.TCBuilder import Tool
from satrap.edictum import SimpleSession
from .file_core import (
    _ReadFileToolCore,
    _ListDirToolCore,
    _GlobFilesToolCore,
    _GrepFilesToolCore,
    _TodoWriteToolCore,
)


class ReadFileTool(_ReadFileToolCore, Tool):
    def execute(self, path: str, offset: int = 0, limit: int = 200) -> str:
        """执行共用文件或待办业务"""
        return self._execute(path, offset, limit)

    def _bind(self, session: SimpleSession) -> None:
        self._session = session


class ListDirTool(_ListDirToolCore, Tool):
    def execute(self, path: str = "") -> str:
        """执行共用文件或待办业务"""
        return self._execute(path)

    def _bind(self, session: SimpleSession) -> None:
        self._session = session


class GlobFilesTool(_GlobFilesToolCore, Tool):
    def execute(self, pattern: str) -> str:
        """执行共用文件或待办业务"""
        return self._execute(pattern)

    def _bind(self, session: SimpleSession) -> None:
        self._session = session


class GrepFilesTool(_GrepFilesToolCore, Tool):
    def execute(self, pattern: str, path: str = "", glob: str = "") -> str:
        """执行共用文件或待办业务"""
        return self._execute(pattern, path, glob)

    def _bind(self, session: SimpleSession) -> None:
        self._session = session


class TodoWriteTool(_TodoWriteToolCore, Tool):
    def execute(self, operation: str, item: str = "", index: int = 0) -> str:
        """执行共用文件或待办业务"""
        return self._execute(operation, item, index)
