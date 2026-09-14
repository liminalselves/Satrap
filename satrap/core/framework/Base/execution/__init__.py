"""
工作流执行与恢复公共类型

普通和可恢复执行使用同一 Agent 循环, 本包提供失败类型及执行记录访问,
驱动器由工作流入口调用, 不提供插件执行阶段 Hook
"""

from .errors import AgentExecutionError, ModelCallError, ModelProtocolError
from .store import RunStore, RunConflictError, RunNeedsAttention

__all__ = ["RunStore", "RunConflictError", "RunNeedsAttention", "AgentExecutionError", "ModelCallError", "ModelProtocolError"]
