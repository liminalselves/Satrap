"""工具定义与执行兼容入口, 同步异步共用元数据和校验规则"""

from typing import Dict, Tuple, Any, Union, List, Callable, cast
import json
from satrap.core.utils.context import ContextManager
from satrap.core.type import LLMCallResponse, safe_getattr
from satrap.core.log import logger
from .utils import (
    _SENSITIVE_ARGUMENT_NAMES,
    _create_tool_error,
    _safe_json_dumps,
    _redact_argument_value,
    _summarize_arguments,
    _tool_execution_error,
    create_tool_defined,
)
from .tool import Tool
from .async_tool import AsyncTool
from .manager import ToolsManager
from .async_manager import AsyncToolsManager
