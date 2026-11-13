"""搜索工具共享结果契约"""
from __future__ import annotations

from typing import TypedDict


class SearchResult(TypedDict):
    """保留搜索工具原有的成功结果字段"""

    title: str
    """搜索结果链接对应的页面标题, 取自结果锚点文本"""
    url: str
    """搜索结果指向的页面地址, 取自结果链接"""
    snippet: str
    """搜索服务展示的结果摘要, 未提供时为空字符串"""
