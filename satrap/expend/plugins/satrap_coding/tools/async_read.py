"""
异步文件读取与内存待办工具

复用公共业务规则, 根据操作性质协调会话交互与文件执行
"""

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
        """
        异步执行共用文件读取或待办操作

        参数:
        - path: 文件路径, 绝对路径或相对工作区路径
        - offset: 起始行偏移, 默认 0
        - limit: 读取行数上限, 默认 200

        返回:
        - 操作结果或输入错误说明, 文件操作还可能返回访问失败说明
        """
        return await asyncio.to_thread(self._execute, path, offset, limit)

    def _bind(self, session: AsyncSimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class AsyncListDirTool(_ListDirToolCore, AsyncTool):
    async def execute(self, path: str = "") -> str:
        """
        异步执行共用文件读取或待办操作

        参数:
        - path: 文件路径, 绝对路径或相对工作区路径

        返回:
        - 操作结果或输入错误说明, 文件操作还可能返回访问失败说明
        """
        return await asyncio.to_thread(self._execute, path)

    def _bind(self, session: AsyncSimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class AsyncGlobFilesTool(_GlobFilesToolCore, AsyncTool):
    async def execute(self, pattern: str) -> str:
        """
        异步执行共用文件读取或待办操作

        参数:
        - pattern: 需要匹配的文件名或文本模式

        返回:
        - 操作结果或输入错误说明, 文件操作还可能返回访问失败说明
        """
        return await asyncio.to_thread(self._execute, pattern)

    def _bind(self, session: AsyncSimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class AsyncGrepFilesTool(_GrepFilesToolCore, AsyncTool):
    async def execute(self, pattern: str, path: str = "", glob: str = "") -> str:
        """
        异步执行共用文件读取或待办操作

        参数:
        - pattern: 需要匹配的文件名或文本模式
        - path: 文件路径, 绝对路径或相对工作区路径
        - glob: 文件过滤模式, 默认空字符串表示不额外过滤

        返回:
        - 操作结果或输入错误说明, 文件操作还可能返回访问失败说明
        """
        return await asyncio.to_thread(self._execute, pattern, path, glob)

    def _bind(self, session: AsyncSimpleSession) -> None:
        """
        在工具注册前绑定所属会话

        参数:
        - session: 工具后续执行使用的会话
        """
        self._session = session


class AsyncTodoWriteTool(_TodoWriteToolCore, AsyncTool):
    async def execute(self, operation: str, item: str = "", index: int = 0) -> str:
        """
        异步执行共用文件读取或待办操作

        参数:
        - operation: 待办操作, 支持 add, done, list, clear
        - item: 待办内容, 默认空字符串, add 时必填
        - index: 待办序号, 从 1 开始, 默认 0, done 时必填

        返回:
        - 操作结果或输入错误说明, 文件操作还可能返回访问失败说明
        """
        return self._execute(operation, item, index)
        # 当前仅修改内存待办, 无阻塞 I/O, 保持事件循环内执行
