"""同步文件读取, 目录查询和待办入口, 共享宿主边界并保留各自操作范围"""
from __future__ import annotations
from functools import partial

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
        """
        同步分页读取固定环境中的文件

        参数:
        - path: 文件路径, 相对路径基于当前工作区
        - offset: 起始行偏移, 默认 0
        - limit: 最大返回行数, 默认 200

        返回:
        - 分页读取结果或错误说明; 审批与目录状态变化时取消原操作
        """
        return self._execute(path, offset, limit)

    def _bind(self, session: SimpleSession) -> None:
        """
        绑定文件操作所属会话

        参数:
        - session: 当前宿主会话
        """
        self._session = session


class ListDirTool(_ListDirToolCore, Tool):
    def execute(self, path: str = "") -> str:
        """
        同步列出固定环境中的目录

        参数:
        - path: 目录路径, 默认空字符串使用工作区

        返回:
        - 搜索或读取结果; 目录改绑时拒绝返回旧请求内容
        """
        return self._read_in_environment(partial(self._execute, path))

    def _bind(self, session: SimpleSession) -> None:
        """
        绑定文件操作所属会话

        参数:
        - session: 当前宿主会话
        """
        self._session = session


class GlobFilesTool(_GlobFilesToolCore, Tool):
    def execute(self, pattern: str) -> str:
        """
        同步匹配固定环境中的文件路径

        参数:
        - pattern: 相对工作区的文件名模式

        返回:
        - 搜索或读取结果; 目录改绑时拒绝返回旧请求内容
        """
        return self._read_in_environment(partial(self._execute, pattern))

    def _bind(self, session: SimpleSession) -> None:
        """
        绑定文件操作所属会话

        参数:
        - session: 当前宿主会话
        """
        self._session = session


class GrepFilesTool(_GrepFilesToolCore, Tool):
    def execute(self, pattern: str, path: str = "", glob: str = "") -> str:
        """
        同步搜索固定环境中的文本

        参数:
        - pattern: 文本搜索模式
        - path: 目录路径, 默认空字符串使用工作区
        - glob: 文件过滤模式, 默认空字符串不额外过滤

        返回:
        - 搜索或读取结果; 目录改绑时拒绝返回旧请求内容
        """
        return self._read_in_environment(partial(self._execute, pattern, path, glob))

    def _bind(self, session: SimpleSession) -> None:
        """
        绑定文件操作所属会话

        参数:
        - session: 当前宿主会话
        """
        self._session = session


class TodoWriteTool(_TodoWriteToolCore, Tool):
    def execute(self, operation: str, item: str = "", index: int = 0) -> str:
        """执行共用文件或待办业务"""
        return self._execute(operation, item, index)
