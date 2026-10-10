"""记忆工具工厂, 同步与异步入口共享宿主业务"""
from __future__ import annotations

from typing import Any

from satrap.edictum import AsyncSimpleSession, SimpleSession
from satrap.edictum.plugin_resources import PluginResources
from satrap.expend.plugins.memory.state import get_plugin_state
from .sync import AddMemoryTool, UpdateMemoryTool, DeleteMemoryTool, ListMemoriesTool, GetMemoryTool
from .async_ import AsyncAddMemoryTool, AsyncUpdateMemoryTool, AsyncDeleteMemoryTool, AsyncListMemoriesTool, AsyncGetMemoryTool


def get_tools(session: SimpleSession | AsyncSimpleSession, config: dict[str, Any] | None = None,
              resources: PluginResources | None = None) -> list[Any]:
    """
    构建独立记忆插件的工具

    参数:
    - session: 工具所属的主会话
    - config: 已解析的插件配置
    - resources: 插件资源对象, 本插件从会话读取记忆存储

    返回:
    - 与会话同步形态一致的记忆工具列表
    """
    state = get_plugin_state(session, config)
    classes = ([AsyncAddMemoryTool, AsyncUpdateMemoryTool, AsyncDeleteMemoryTool, AsyncListMemoriesTool, AsyncGetMemoryTool]
               if isinstance(session, AsyncSimpleSession)
               else [AddMemoryTool, UpdateMemoryTool, DeleteMemoryTool, ListMemoriesTool, GetMemoryTool])
    result = [tool(state["store"], service=state["service"]) for tool in classes]
    for tool in result:
        tool.config = dict(config or {})
    return result
