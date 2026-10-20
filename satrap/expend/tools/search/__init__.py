"""网页搜索与正文读取的同步和异步公开入口"""
from .async_ import AsyncSearchTool, AsyncFetchPageTool
from .sync import SearchTool, FetchPageTool

__all__ = ["SearchTool", "FetchPageTool", "AsyncSearchTool", "AsyncFetchPageTool"]
