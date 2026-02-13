from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap import logger

class ModelWorkflowFramework:
    """
    模型工作流框架, 负责管理模型的调用和工作流的执行
    这是 Satrap 的核心组件之一, 负责协调单个模型实例调用, 以实现复杂的任务处理和自动化流程;

    任何工作流都应该继承这个类, 并实现自己的工作流逻辑, 以便被调用和执行
    """
    def __init__(self, model: LLM):

        self.model = model