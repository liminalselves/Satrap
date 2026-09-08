from typing import Any
from .tool_base import _ToolBase


class Tool(_ToolBase):
    """
    工具基类; 所有工具都应当继承自该类

    定义工具时, 应当在子类中通过类属性指定元数据:
        - tool_name: 工具名称
        - description: 工具描述
        - params_dict: 参数字典, 格式如 {"param1": ("类型", "描述"), ...}
    然后实现 execute 方法

    当然仍然支持使用`super().__init__(tool_name, description, params_dict)`初始化工具

    {
        "param1": ("string", "这是第一个参数"),
        "param2": ("number", "这是第二个参数"),
        ...
    }

    使用示例:
    ``` python
    # 写法 1. 直接在类属性中指定元数据
    class sum_numbers(Tool):
        tool_name = 'sum'
        description = '计算两个数的和'
        params_dict = {
            "a": ("number", "第一个数"),
            "b": ("number", "第二个数")
        }

        def execute(self, a: float, b: float) -> float:
            return a + b

    # 写法 2. 也可以在 __init__ 中指定元数据
    class sum_numbers(Tool):
        def __init__(
        self,
        tool_name = 'sum',
        description = '计算两个数的和',
        params_dict = {
            "a": ("number", "第一个数"),
            "b": ("number", "第二个数")
        }):
        super().__init__(tool_name, description, params_dict)

        def execute(self, a: float, b: float) -> float:
            '''执行工具; 计算两个数的和'''
            return a + b

    # 执行
    tool = sum_numbers()
    result = tool(1, 2)   # 返回 3
    ```
    """

    def execute(self, *input: Any, **kwargs: Any) -> Any:
        """
        执行工具

        参数:
        - input: 输入
        - kwargs: 额外关键字参数

        返回:
        - Any: 执行工具
        """
        return None

    def __call__(self, *input: Any, **kwargs: Any):
        """
        使工具实例可被调用
        使用方式: result = tool_instance(...)

        参数:
        - input: 输入
        - kwargs: 额外关键字参数

        返回:
        - 使工具实例可被调用
        """
        result = self.execute(*input, **kwargs)
        return result
