"""edictum: 简易单工作流 Agent 系统

提供 SimpleSession / AsyncSimpleSession: 基于 Session 的高可扩展单 workflow
Agent 框架, 支持命令/工具/MCP/skill/插件五类能力的注入、删除、启停与查看,
插件采用函数直注流程 (非 hook), checkpoint 与多模态/流式能力完整。
"""
from satrap.edictum.simple_session import AsyncSimpleSession, SessionPlugin, SimpleSession

__all__ = ["SimpleSession", "AsyncSimpleSession", "SessionPlugin"]
