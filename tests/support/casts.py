"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

from typing import Any, cast

from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.utils.TCBuilder import AsyncToolsManager, ToolsManager


def _as_llm(fake: Any) -> LLM:
    """LLM 替身类型边界: 替身实现 LLM.call 调用面, cast 集中在此工厂"""
    return cast(LLM, fake)


def _as_async_llm(fake: Any) -> AsyncLLM:
    """AsyncLLM 替身类型边界: 替身实现 AsyncLLM.call 调用面, cast 集中在此工厂"""
    return cast(AsyncLLM, fake)


def _as_tools_manager(fake: Any) -> ToolsManager:
    """ToolsManager 替身类型边界: 替身实现工具查询调用面, cast 集中在此工厂"""
    return cast(ToolsManager, fake)


def _as_async_tools_manager(fake: Any) -> AsyncToolsManager:
    """AsyncToolsManager 替身类型边界: 替身实现工具查询调用面, cast 集中在此工厂"""
    return cast(AsyncToolsManager, fake)
