"""MCP 客户端与工具桥接兼容入口"""

from __future__ import annotations
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from contextlib import AsyncExitStack
from mcp.server import MCPServer
import threading
import asyncio
from asyncio import AbstractEventLoop
import inspect
from typing import Any, Awaitable, Dict, List, Literal, Optional, Protocol, Tuple, cast
from types import TracebackType
from mcp import ClientSession
from satrap.core.utils.TCBuilder import AsyncTool, AsyncToolsManager, Tool, ToolsManager
from satrap.core.type import safe_getattr, safe_getattr_str
from satrap.core.log import logger
from .utils import (
    TYPE_MAP,
    content_to_text,
    _input_schema_of,
    _params_from_schema,
    _mcp_error,
    _consume,
    MCPSessionProtocol,
)
from .adapters import MCPToolAdapter, SyncMCPToolAdapter
from .client import MCPClient
from .server import MCPServerExporter, export_tools_to_mcp

__all__ = [
    "MCPClient",
    "MCPToolAdapter",
    "SyncMCPToolAdapter",
    "MCPServerExporter",
    "export_tools_to_mcp",
    "content_to_text",
]
