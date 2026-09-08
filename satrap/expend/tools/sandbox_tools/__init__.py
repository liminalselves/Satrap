"""沙箱工具兼容入口, 执行逻辑由同步异步共同复用"""

from collections.abc import Awaitable, Callable
import asyncio, re
from typing import Dict, Any, Optional
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.core.utils.sandbox import CodeSandbox
from satrap.core.log import logger
from .utils import extract_code, SyncExecutionAuthorizer, AsyncExecutionAuthorizer
from .sync import CodeSandboxTool
from .async_ import AsyncCodeSandboxTool
