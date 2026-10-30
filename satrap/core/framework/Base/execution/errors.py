"""
工作流执行异常

普通与可恢复执行使用相同的失败契约, 任务状态由执行记录层另行保存
"""

from satrap.core.type import LLMErrorInfo


class AgentExecutionError(RuntimeError):
    """Agent 执行未能产生可提交的完整回答"""


class ModelCallError(AgentExecutionError):
    """模型调用失败或未返回有效响应"""

    def __init__(self, error: str | LLMErrorInfo) -> None:
        """
        保留模型错误资料供宿主回传, 不保存原始请求

        参数:
        - error: 兼容旧调用的文本或已分类的错误
        """
        self.info = error if isinstance(error, LLMErrorInfo) else LLMErrorInfo("model_call_failed", error)
        super().__init__(error if isinstance(error, str) else error.text)


class ModelProtocolError(AgentExecutionError):
    """模型响应违反当前执行阶段的协议"""
