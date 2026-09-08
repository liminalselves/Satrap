"""子代理工具兼容入口, 共用输入规则并保留各自调度策略"""

from multiprocessing.process import BaseProcess
import multiprocessing
from dataclasses import dataclass
import threading
import asyncio
import pickle
from typing import Any, Protocol, cast
import queue
import json
import time
from uuid import uuid4
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.core.utils.TCBuilder import ToolsManager, AsyncToolsManager
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.core.framework.Base import (
    ModelWorkflowFramework,
    AsyncModelWorkflowFramework,
)
from satrap.core.log import logger
from .utils import (
    SUB_AGENT_SYSTEM_PROMPT,
    MAX_SUB_AGENT_TASKS,
    DEFAULT_SUB_AGENT_TIMEOUT,
    DEFAULT_SUB_AGENT_WORKERS,
    _ProcessConnection,
    _PendingToolCall,
    _RunningSubTask,
    _serialize_llm,
    _restore_llm,
    _ProxyToolsManager,
    _execute_parent_tool,
    _run_sub_task_process,
)
from .sync import SubAgent, SubAgentModel
from .async_ import AsyncSubAgent, AsyncSubAgentModel
