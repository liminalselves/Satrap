from satrap import logger

class ModelWorkflowFramework:
    """模型工作流框架"""
    def __init__(self):
        """
        模型工作流框架, 负责管理模型的调用和工作流的执行
        这是 Satrap 的核心组件之一, 负责协调单个模型实例调用, 以实现复杂的任务处理和自动化流程;

        任何工作流都应该继承这个类, 并实现自己的工作流逻辑, 以便被调用和执行

        工作流应当覆写 `forward` 方法, 并在其中实现模型的调用和工作流的逻辑
        """

    def forward(self):
        """执行工作流; 调用模型并返回结果"""
        return None

    def __call__(self, *input, **kwargs):
        result = self.forward(*input, **kwargs)
        return result

