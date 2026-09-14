"""
工作流执行异常

普通与可恢复执行使用相同的失败契约, 任务状态由执行记录层另行保存
"""


class AgentExecutionError(RuntimeError):
    """Agent 执行未能产生可提交的完整回答"""


class ModelCallError(AgentExecutionError):
    """模型调用失败或未返回有效响应"""


class ModelProtocolError(AgentExecutionError):
    """模型响应违反当前执行阶段的协议"""
