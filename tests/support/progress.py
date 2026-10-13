"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

import asyncio
import pytest
from satrap.core.utils.application_progress import ApplicationOperations

async def _finished(registry: ApplicationOperations, key: str) -> dict:
    """只读等候已登记的任务, 不提交第二次动作"""
    for _attempt in range(100):
        result = registry.get(key)
        if result["status"] != "running":
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("已启动任务未结束")
