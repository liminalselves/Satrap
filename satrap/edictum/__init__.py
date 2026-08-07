"""edictum: 简易单工作流 Agent 系统

提供 SimpleSession / AsyncSimpleSession: 基于 Session 的高可扩展单 workflow
Agent 框架, 支持命令/工具/MCP/skill/处理器/插件六类能力的注入、删除、启停与查看:
- SessionHandler: 函数直注流程 (非 hook), 4 处理点, 优先级排序, before 可改写/拦截
- Plugin: 目录化组合包 (meta.yaml + tools.py/skills.py/mcp.py/handlers.py), 双层启停
- checkpoint 与多模态/流式能力完整。
"""
from satrap.edictum.plugin import Plugin
from satrap.edictum.simple_session import AsyncSimpleSession, SessionHandler, SimpleSession

__all__ = ["SimpleSession", "AsyncSimpleSession", "SessionHandler", "Plugin"]
